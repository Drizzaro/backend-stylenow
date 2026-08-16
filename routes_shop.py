from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from core import (
    db, iso_now, new_id, short_id, require_user, get_current_user,
    cart_key, validate_coupon, get_settings, get_wallet, credit_points, public_product,
    recompute_product_rating,
)
from storage import put_object, compress_image
from fastapi import UploadFile, File

router = APIRouter(tags=["shop"])


# ---------- Cart ----------

async def get_cart_doc(request: Request, user=None) -> dict:
    key = cart_key(request, user)
    cart = await db.carts.find_one({"key": key}, {"_id": 0})
    if not cart:
        cart = {"key": key, "items": [], "coupon_code": None, "updated_at": iso_now()}
        await db.carts.insert_one(cart)
    return cart


async def enrich_cart(cart: dict) -> dict:
    items, subtotal = [], 0
    for it in cart.get("items", []):
        p = await db.products.find_one({"id": it["product_id"], "active": True}, {"_id": 0})
        if not p:
            continue
        v = next((x for x in p.get("variants", []) if x["id"] == it["variant_id"]), None)
        if not v:
            continue
        img = (v.get("images") or p.get("images") or [""])[0]
        line = v.get("price", 0) * it["qty"]
        subtotal += line
        item_oos = bool(p.get("out_of_stock") or v.get("out_of_stock"))
        items.append({
            "product_id": p["id"], "variant_id": v["id"], "qty": it["qty"],
            "name": p.get("name", ""), "image": img, "price": v.get("price", 0),
            "mrp": v.get("mrp", v.get("price", 0)), "color": v.get("color", ""),
            "size": v.get("size", ""), "stock": v.get("stock", 0),
            "out_of_stock": item_oos,
            "in_stock": (not item_oos) and v.get("stock", 0) >= it["qty"], "line_total": line,
        })
    return {"items": items, "subtotal": subtotal, "coupon_code": cart.get("coupon_code")}


@router.get("/cart")
async def get_cart(request: Request):
    user = await get_current_user(request)
    cart = await get_cart_doc(request, user)
    return await enrich_cart(cart)


class CartItemBody(BaseModel):
    product_id: str
    variant_id: str
    qty: int = 1


@router.post("/cart/items")
async def add_cart_item(body: CartItemBody, request: Request):
    user = await get_current_user(request)
    p = await db.products.find_one({"id": body.product_id, "active": True}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Product not found")
    v = next((x for x in p.get("variants", []) if x["id"] == body.variant_id), None)
    if not v:
        raise HTTPException(404, "Variant not found")
    qty = max(1, min(body.qty, 10))
    if p.get("out_of_stock") or v.get("out_of_stock"):
        raise HTTPException(400, "This item is currently out of stock")
    if v.get("stock", 0) < qty:
        raise HTTPException(400, "Not enough stock available")
    cart = await get_cart_doc(request, user)
    items = cart.get("items", [])
    for it in items:
        if it["product_id"] == body.product_id and it["variant_id"] == body.variant_id:
            it["qty"] = min(it["qty"] + qty, 10)
            break
    else:
        items.append({"product_id": body.product_id, "variant_id": body.variant_id, "qty": qty})
    await db.carts.update_one({"key": cart["key"]}, {"$set": {"items": items, "updated_at": iso_now()}})
    return await enrich_cart({**cart, "items": items})


class CartQtyBody(BaseModel):
    qty: int


@router.put("/cart/items/{product_id}/{variant_id}")
async def update_cart_item(product_id: str, variant_id: str, body: CartQtyBody, request: Request):
    user = await get_current_user(request)
    cart = await get_cart_doc(request, user)
    items = [i for i in cart.get("items", []) if not (i["product_id"] == product_id and i["variant_id"] == variant_id)]
    if body.qty > 0:
        items.append({"product_id": product_id, "variant_id": variant_id, "qty": min(body.qty, 10)})
    await db.carts.update_one({"key": cart["key"]}, {"$set": {"items": items, "updated_at": iso_now()}})
    return await enrich_cart({**cart, "items": items})


@router.delete("/cart/items/{product_id}/{variant_id}")
async def remove_cart_item(product_id: str, variant_id: str, request: Request):
    user = await get_current_user(request)
    cart = await get_cart_doc(request, user)
    items = [i for i in cart.get("items", []) if not (i["product_id"] == product_id and i["variant_id"] == variant_id)]
    await db.carts.update_one({"key": cart["key"]}, {"$set": {"items": items, "updated_at": iso_now()}})
    return await enrich_cart({**cart, "items": items})


class CouponBody(BaseModel):
    code: str


@router.post("/cart/coupon")
async def apply_cart_coupon(body: CouponBody, request: Request):
    user = await get_current_user(request)
    cart = await get_cart_doc(request, user)
    enriched = await enrich_cart(cart)
    discount, info = await validate_coupon(body.code, enriched["subtotal"], user["id"] if user else None)
    if not info or info.get("error"):
        raise HTTPException(400, (info or {}).get("error", "Invalid coupon"))
    await db.carts.update_one({"key": cart["key"]}, {"$set": {"coupon_code": info["code"], "updated_at": iso_now()}})
    return {**enriched, "coupon_code": info["code"], "discount": discount, "coupon": info}


@router.delete("/cart/coupon")
async def remove_cart_coupon(request: Request):
    user = await get_current_user(request)
    cart = await get_cart_doc(request, user)
    await db.carts.update_one({"key": cart["key"]}, {"$set": {"coupon_code": None, "updated_at": iso_now()}})
    return await enrich_cart({**cart, "coupon_code": None})


# ---------- Wishlist ----------

@router.get("/wishlist")
async def get_wishlist(request: Request):
    user = await require_user(request)
    wl = await db.wishlists.find_one({"user_id": user["id"]}, {"_id": 0}) or {"product_ids": []}
    prods = await db.products.find({"id": {"$in": wl.get("product_ids", [])}, "active": True}, {"_id": 0}).to_list(100)
    return {"items": [public_product(p) for p in prods]}


class WishlistBody(BaseModel):
    product_id: str


@router.post("/wishlist")
async def add_wishlist(body: WishlistBody, request: Request):
    user = await require_user(request)
    await db.wishlists.update_one(
        {"user_id": user["id"]},
        {"$addToSet": {"product_ids": body.product_id}, "$setOnInsert": {"id": new_id()}},
        upsert=True)
    return {"ok": True}


@router.delete("/wishlist/{product_id}")
async def remove_wishlist(product_id: str, request: Request):
    user = await require_user(request)
    await db.wishlists.update_one({"user_id": user["id"]}, {"$pull": {"product_ids": product_id}})
    return {"ok": True}


# ---------- Addresses ----------

@router.get("/addresses")
async def get_addresses(request: Request):
    user = await require_user(request)
    items = await db.addresses.find({"user_id": user["id"]}, {"_id": 0}).to_list(20)
    return {"items": items}


class AddressBody(BaseModel):
    name: str
    phone: str
    line1: str
    line2: str = ""
    landmark: str = ""
    city: str = "Bahraich"
    pincode: str
    is_default: bool = False


@router.post("/addresses")
async def add_address(body: AddressBody, request: Request):
    user = await require_user(request)
    doc = {"id": new_id(), "user_id": user["id"], **body.model_dump(), "created_at": iso_now()}
    if body.is_default:
        await db.addresses.update_many({"user_id": user["id"]}, {"$set": {"is_default": False}})
    elif await db.addresses.count_documents({"user_id": user["id"]}) == 0:
        doc["is_default"] = True
    await db.addresses.insert_one(doc)
    doc.pop("_id", None)
    return {"address": doc}


@router.delete("/addresses/{address_id}")
async def delete_address(address_id: str, request: Request):
    user = await require_user(request)
    await db.addresses.delete_one({"id": address_id, "user_id": user["id"]})
    return {"ok": True}


# ---------- Rewards & Spin ----------

@router.get("/rewards")
async def rewards(request: Request):
    user = await require_user(request)
    wallet = await get_wallet(user["id"])
    txns = await db.reward_transactions.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(50)
    spins = await db.spin_transactions.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(20)
    coupons = await db.coupons.find({"user_id": user["id"], "active": True}, {"_id": 0}).to_list(20)
    settings = await get_settings()
    return {"wallet": wallet, "transactions": txns, "spins": spins, "coupons": coupons,
            "points_per_spin": settings.get("points_per_spin", 50)}


@router.get("/spin")
async def spin_info(request: Request):
    user = await require_user(request)
    settings = await get_settings()
    wallet = await get_wallet(user["id"])
    rewards = await db.spin_rewards.find({"active": True}, {"_id": 0, "probability": 0}).to_list(20)
    cost = settings.get("points_per_spin", 50)
    return {"rewards": rewards, "cost": cost, "balance": wallet.get("balance", 0),
            "can_spin": wallet.get("balance", 0) >= cost and settings.get("spin_enabled", True),
            "enabled": settings.get("spin_enabled", True)}


@router.post("/spin")
async def do_spin(request: Request):
    import random as rnd
    user = await require_user(request)
    settings = await get_settings()
    if not settings.get("spin_enabled", True):
        raise HTTPException(400, "Spin & Win is currently unavailable")
    cost = settings.get("points_per_spin", 50)
    wallet = await get_wallet(user["id"])
    if wallet.get("balance", 0) < cost:
        raise HTTPException(400, f"You need {cost} StylePoints to spin")
    rewards = await db.spin_rewards.find({"active": True}, {"_id": 0}).to_list(50)
    if not rewards:
        raise HTTPException(400, "No rewards configured")
    weights = [max(r.get("probability", 1), 0) for r in rewards]
    if sum(weights) == 0:
        weights = [1] * len(rewards)
    reward = rnd.choices(rewards, weights=weights, k=1)[0]
    await credit_points(user["id"], -cost, "spin_cost", "Spin & Win", "")
    result = {"id": reward.get("id"), "label": reward.get("label", ""), "type": reward.get("type", "none"), "value": reward.get("value", 0)}
    if reward.get("type") == "points":
        await credit_points(user["id"], int(reward.get("value", 0)), "spin_reward", reward.get("label", "Spin reward"), "")
    elif reward.get("type") in ("coupon_percent", "coupon_flat", "free_delivery"):
        from datetime import timedelta as td
        from core import utcnow
        code = "SPIN" + short_id()
        ctype = {"coupon_percent": "percent", "coupon_flat": "flat", "free_delivery": "free_delivery"}[reward["type"]]
        await db.coupons.insert_one({
            "id": new_id(), "code": code, "label": reward.get("label", code), "type": ctype,
            "value": reward.get("value", 0), "min_order": 0, "max_discount": None,
            "usage_limit": 1, "per_user_limit": 1, "used_count": 0, "user_id": user["id"],
            "expires_at": (utcnow() + td(days=reward.get("expiry_days", 7))).isoformat(),
            "active": True, "first_order_only": False, "created_at": iso_now(),
        })
        result["coupon_code"] = code
    await db.spin_transactions.insert_one({
        "id": new_id(), "user_id": user["id"], "reward_label": result["label"],
        "reward_type": result["type"], "reward_value": result.get("value", 0),
        "coupon_code": result.get("coupon_code"), "created_at": iso_now(),
    })
    await notify_spin(user["id"], result)
    wallet = await get_wallet(user["id"])
    return {"result": result, "balance": wallet.get("balance", 0)}


async def notify_spin(user_id, result):
    from core import notify
    msg = result["label"] if result["type"] != "none" else "Better luck next time!"
    await notify(user_id, "spin", "Spin & Win", f"You played Spin & Win: {msg}")


# ---------- Customer uploads (review photos — compressed to <=1MB) ----------

@router.post("/uploads")
async def customer_upload(request: Request, file: UploadFile = File(...)):
    user = await require_user(request)
    ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""
    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
        raise HTTPException(400, "Only image files are allowed")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 20MB before compression)")
    data, ctype, ext = compress_image(data)
    path = f"stylenow/reviews/{user['id']}/{new_id()}.{ext}"
    result = put_object(path, data, ctype)
    await db.files.insert_one({
        "id": new_id(), "storage_path": result["path"], "original_filename": file.filename,
        "content_type": ctype, "size": result.get("size", len(data)), "kind": "image",
        "public": True, "is_deleted": False, "created_at": iso_now(),
    })
    return {"path": result["path"], "url": f"/api/files/{result['path']}"}


# ---------- Reviews ----------

class ReviewBody(BaseModel):
    rating: int
    comment: str = ""
    images: list[str] = []


@router.post("/products/{product_id}/reviews")
async def add_review(product_id: str, body: ReviewBody, request: Request):
    user = await require_user(request)
    if not 1 <= body.rating <= 5:
        raise HTTPException(400, "Rating must be between 1 and 5")
    bought = await db.orders.find_one({
        "user_id": user["id"], "status": "delivered", "items.product_id": product_id})
    if not bought:
        raise HTTPException(403, "Only verified buyers can review this product")
    existing = await db.reviews.find_one({"product_id": product_id, "user_id": user["id"]})
    if existing:
        raise HTTPException(400, "You have already reviewed this product")
    await db.reviews.insert_one({
        "id": new_id(), "product_id": product_id, "user_id": user["id"],
        "user_name": user.get("name") or "StyleNow Customer", "rating": body.rating,
        "comment": body.comment.strip(), "images": body.images[:4], "approved": True,
        "created_at": iso_now(),
    })
    agg = await db.reviews.aggregate([
        {"$match": {"product_id": product_id, "approved": True}},
        {"$group": {"_id": None, "avg": {"$avg": "$rating"}, "n": {"$sum": 1}}},
    ]).to_list(1)
    if agg:
        await db.products.update_one({"id": product_id},
            {"$set": {"rating_avg": round(agg[0]["avg"], 1), "rating_count": agg[0]["n"]}})
    return {"ok": True}


@router.delete("/products/{product_id}/reviews/{review_id}")
async def delete_own_review(product_id: str, review_id: str, request: Request):
    user = await require_user(request)
    res = await db.reviews.delete_one({"id": review_id, "product_id": product_id, "user_id": user["id"]})
    if res.deleted_count == 0:
        raise HTTPException(404, "Review not found")
    await recompute_product_rating(product_id)
    return {"ok": True}


# ---------- Notifications ----------

@router.get("/notifications")
async def notifications(request: Request):
    user = await require_user(request)
    items = await db.notifications.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(50)
    unread = sum(1 for i in items if not i.get("read"))
    return {"items": items, "unread": unread}


@router.post("/notifications/read")
async def mark_notifications_read(request: Request):
    user = await require_user(request)
    await db.notifications.update_many({"user_id": user["id"]}, {"$set": {"read": True}})
    return {"ok": True}


@router.get("/recently-viewed")
async def recently_viewed(request: Request):
    user = await require_user(request)
    rv = await db.recently_viewed.find({"user_id": user["id"]}, {"_id": 0}).sort("viewed_at", -1).to_list(10)
    ids = [r["product_id"] for r in rv]
    prods = await db.products.find({"id": {"$in": ids}, "active": True}, {"_id": 0}).to_list(10)
    order = {p["id"]: p for p in prods}
    return {"items": [public_product(order[i]) for i in ids if i in order]}
