import json
from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from pydantic import BaseModel

from core import (
    iso_now, new_id, short_id, require_user, get_current_user,
    cart_key, validate_coupon, get_settings, get_wallet, credit_points,
    public_product, recompute_product_rating, _parse_jsonb, notify, utcnow,
)
from storage import put_object, compress_image
import db

router = APIRouter(tags=["shop"])


# ---------- Cart ----------

async def get_cart_doc(request: Request, user=None) -> dict:
    key = cart_key(request, user)
    cart = await db.fetch_one("SELECT * FROM carts WHERE key=$1", key)
    if not cart:
        await db.execute(
            "INSERT INTO carts (key, items, coupon_code, updated_at) VALUES ($1,'[]',null,$2)",
            key, iso_now()
        )
        cart = {"key": key, "items": [], "coupon_code": None, "updated_at": iso_now()}
    return dict(cart)


async def enrich_cart(cart: dict) -> dict:
    items_raw = _parse_jsonb(cart.get("items"), [])
    items, subtotal = [], 0
    for it in items_raw:
        p = await db.fetch_one("SELECT * FROM products WHERE id=$1 AND active=true", it["product_id"])
        if not p:
            continue
        variants = _parse_jsonb(p["variants"], [])
        v = next((x for x in variants if x["id"] == it["variant_id"]), None)
        if not v:
            continue
        img = (v.get("images") or _parse_jsonb(p.get("images"), []) or [""])[0]
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
    p = await db.fetch_one("SELECT * FROM products WHERE id=$1 AND active=true", body.product_id)
    if not p:
        raise HTTPException(404, "Product not found")
    variants = _parse_jsonb(p["variants"], [])
    v = next((x for x in variants if x["id"] == body.variant_id), None)
    if not v:
        raise HTTPException(404, "Variant not found")
    qty = max(1, min(body.qty, 10))
    if p.get("out_of_stock") or v.get("out_of_stock"):
        raise HTTPException(400, "This item is currently out of stock")
    if v.get("stock", 0) < qty:
        raise HTTPException(400, "Not enough stock available")

    cart = await get_cart_doc(request, user)
    items = _parse_jsonb(cart.get("items"), [])
    for it in items:
        if it["product_id"] == body.product_id and it["variant_id"] == body.variant_id:
            it["qty"] = min(it["qty"] + qty, 10)
            break
    else:
        items.append({"product_id": body.product_id, "variant_id": body.variant_id, "qty": qty})

    await db.execute(
        "UPDATE carts SET items=$1, updated_at=$2 WHERE key=$3",
        json.dumps(items), iso_now(), cart["key"]
    )
    return await enrich_cart({**cart, "items": items})


class CartQtyBody(BaseModel):
    qty: int


@router.put("/cart/items/{product_id}/{variant_id}")
async def update_cart_item(product_id: str, variant_id: str, body: CartQtyBody, request: Request):
    user = await get_current_user(request)
    cart = await get_cart_doc(request, user)
    items = [i for i in _parse_jsonb(cart.get("items"), [])
             if not (i["product_id"] == product_id and i["variant_id"] == variant_id)]
    if body.qty > 0:
        items.append({"product_id": product_id, "variant_id": variant_id, "qty": min(body.qty, 10)})
    await db.execute("UPDATE carts SET items=$1, updated_at=$2 WHERE key=$3",
                     json.dumps(items), iso_now(), cart["key"])
    return await enrich_cart({**cart, "items": items})


@router.delete("/cart/items/{product_id}/{variant_id}")
async def remove_cart_item(product_id: str, variant_id: str, request: Request):
    user = await get_current_user(request)
    cart = await get_cart_doc(request, user)
    items = [i for i in _parse_jsonb(cart.get("items"), [])
             if not (i["product_id"] == product_id and i["variant_id"] == variant_id)]
    await db.execute("UPDATE carts SET items=$1, updated_at=$2 WHERE key=$3",
                     json.dumps(items), iso_now(), cart["key"])
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
    await db.execute("UPDATE carts SET coupon_code=$1, updated_at=$2 WHERE key=$3",
                     info["code"], iso_now(), cart["key"])
    return {**enriched, "coupon_code": info["code"], "discount": discount, "coupon": info}


@router.delete("/cart/coupon")
async def remove_cart_coupon(request: Request):
    user = await get_current_user(request)
    cart = await get_cart_doc(request, user)
    await db.execute("UPDATE carts SET coupon_code=null, updated_at=$1 WHERE key=$2",
                     iso_now(), cart["key"])
    return await enrich_cart({**cart, "coupon_code": None})


# ---------- Wishlist ----------

@router.get("/wishlist")
async def get_wishlist(request: Request):
    user = await require_user(request)
    wl = await db.fetch_one("SELECT product_ids FROM wishlists WHERE user_id=$1", user["id"])
    pids = list(wl["product_ids"] or []) if wl else []
    if not pids:
        return {"items": []}
    prods = await db.fetch_all("SELECT * FROM products WHERE id=ANY($1) AND active=true", pids)
    return {"items": [public_product(dict(p)) for p in prods]}


class WishlistBody(BaseModel):
    product_id: str


@router.post("/wishlist")
async def add_wishlist(body: WishlistBody, request: Request):
    user = await require_user(request)
    await db.execute(
        """INSERT INTO wishlists (id, user_id, product_ids) VALUES ($1,$2,ARRAY[$3]::TEXT[])
           ON CONFLICT (user_id) DO UPDATE SET product_ids = array_append(
               array_remove(wishlists.product_ids, $3), $3
           )""",
        new_id(), user["id"], body.product_id
    )
    return {"ok": True}


@router.delete("/wishlist/{product_id}")
async def remove_wishlist(product_id: str, request: Request):
    user = await require_user(request)
    await db.execute(
        "UPDATE wishlists SET product_ids=array_remove(product_ids,$1) WHERE user_id=$2",
        product_id, user["id"]
    )
    return {"ok": True}


# ---------- Addresses ----------

@router.get("/addresses")
async def get_addresses(request: Request):
    user = await require_user(request)
    items = await db.fetch_all(
        "SELECT * FROM addresses WHERE user_id=$1 ORDER BY created_at ASC LIMIT 20", user["id"]
    )
    return {"items": [dict(a) for a in items]}


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
    if body.is_default:
        await db.execute("UPDATE addresses SET is_default=false WHERE user_id=$1", user["id"])
    count = await db.fetch_val("SELECT COUNT(*) FROM addresses WHERE user_id=$1", user["id"])
    is_default = body.is_default or (count == 0)
    aid = new_id()
    await db.execute(
        """INSERT INTO addresses (id, user_id, name, phone, line1, line2, landmark, city, pincode, is_default, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
        aid, user["id"], body.name, body.phone, body.line1, body.line2,
        body.landmark, body.city, body.pincode, is_default, iso_now()
    )
    doc = await db.fetch_one("SELECT * FROM addresses WHERE id=$1", aid)
    return {"address": dict(doc)}


@router.delete("/addresses/{address_id}")
async def delete_address(address_id: str, request: Request):
    user = await require_user(request)
    await db.execute("DELETE FROM addresses WHERE id=$1 AND user_id=$2", address_id, user["id"])
    return {"ok": True}


# ---------- Rewards & Spin ----------

@router.get("/rewards")
async def rewards(request: Request):
    user = await require_user(request)
    wallet = await get_wallet(user["id"])
    txns = await db.fetch_all(
        "SELECT * FROM reward_transactions WHERE user_id=$1 ORDER BY created_at DESC LIMIT 50",
        user["id"]
    )
    spins = await db.fetch_all(
        "SELECT * FROM spin_transactions WHERE user_id=$1 ORDER BY created_at DESC LIMIT 20",
        user["id"]
    )
    coupons = await db.fetch_all(
        "SELECT * FROM coupons WHERE user_id=$1 AND active=true", user["id"]
    )
    settings = await get_settings()
    return {
        "wallet": wallet,
        "transactions": [dict(t) for t in txns],
        "spins": [dict(s) for s in spins],
        "coupons": [dict(c) for c in coupons],
        "points_per_spin": settings.get("points_per_spin", 50),
    }


@router.get("/spin")
async def spin_info(request: Request):
    user = await require_user(request)
    settings = await get_settings()
    wallet = await get_wallet(user["id"])
    spin_rewards = await db.fetch_all(
        "SELECT id, label, type, value, expiry_days, active FROM spin_rewards WHERE active=true LIMIT 20"
    )
    cost = settings.get("points_per_spin", 50)
    return {
        "rewards": [dict(r) for r in spin_rewards],
        "cost": cost,
        "balance": wallet.get("balance", 0),
        "can_spin": wallet.get("balance", 0) >= cost and settings.get("spin_enabled", True),
        "enabled": settings.get("spin_enabled", True),
    }


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

    spin_rewards = await db.fetch_all("SELECT * FROM spin_rewards WHERE active=true LIMIT 50")
    if not spin_rewards:
        raise HTTPException(400, "No rewards configured")
    rewards_list = [dict(r) for r in spin_rewards]
    weights = [max(r.get("probability", 1), 0) for r in rewards_list]
    if sum(weights) == 0:
        weights = [1] * len(rewards_list)
    reward = rnd.choices(rewards_list, weights=weights, k=1)[0]

    await credit_points(user["id"], -cost, "spin_cost", "Spin & Win", "")
    result = {"id": reward.get("id"), "label": reward.get("label", ""), "type": reward.get("type", "none"), "value": reward.get("value", 0)}

    if reward.get("type") == "points":
        await credit_points(user["id"], int(reward.get("value", 0)), "spin_reward", reward.get("label", "Spin reward"), "")
    elif reward.get("type") in ("coupon_percent", "coupon_flat", "free_delivery"):
        from datetime import timedelta as td
        code = "SPIN" + short_id()
        ctype = {"coupon_percent": "percent", "coupon_flat": "flat", "free_delivery": "free_delivery"}[reward["type"]]
        expires = (utcnow() + td(days=reward.get("expiry_days", 7))).isoformat()
        await db.execute(
            """INSERT INTO coupons (id, code, label, type, value, min_order, max_discount, usage_limit,
               per_user_limit, used_count, user_id, expires_at, active, first_order_only, created_at)
               VALUES ($1,$2,$3,$4,$5,0,null,1,1,0,$6,$7,true,false,$8)""",
            new_id(), code, reward.get("label", code), ctype,
            reward.get("value", 0), user["id"], expires, iso_now()
        )
        result["coupon_code"] = code

    await db.execute(
        """INSERT INTO spin_transactions (id, user_id, reward_label, reward_type, reward_value, coupon_code, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7)""",
        new_id(), user["id"], result["label"], result["type"],
        result.get("value", 0), result.get("coupon_code"), iso_now()
    )
    msg = result["label"] if result["type"] != "none" else "Better luck next time!"
    await notify(user["id"], "spin", "Spin & Win", f"You played Spin & Win: {msg}")

    wallet = await get_wallet(user["id"])
    return {"result": result, "balance": wallet.get("balance", 0)}


# ---------- Customer uploads ----------

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
    await db.execute(
        """INSERT INTO files (id, storage_path, original_filename, content_type, size, kind, public, is_deleted, created_at)
           VALUES ($1,$2,$3,$4,$5,'image',true,false,$6)""",
        new_id(), result["path"], file.filename, ctype, result.get("size", len(data)), iso_now()
    )
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
    bought = await db.fetch_one(
        """SELECT id FROM orders WHERE user_id=$1 AND status='delivered'
           AND items::text LIKE $2""",
        user["id"], f"%{product_id}%"
    )
    if not bought:
        raise HTTPException(403, "Only verified buyers can review this product")
    existing = await db.fetch_one(
        "SELECT id FROM reviews WHERE product_id=$1 AND user_id=$2", product_id, user["id"]
    )
    if existing:
        raise HTTPException(400, "You have already reviewed this product")
    await db.execute(
        """INSERT INTO reviews (id, product_id, user_id, user_name, rating, comment, images, approved, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,true,$8)""",
        new_id(), product_id, user["id"],
        user.get("name") or "StyleNow Customer",
        body.rating, body.comment.strip(), body.images[:4], iso_now()
    )
    await recompute_product_rating(product_id)
    return {"ok": True}


@router.delete("/products/{product_id}/reviews/{review_id}")
async def delete_own_review(product_id: str, review_id: str, request: Request):
    user = await require_user(request)
    result = await db.execute(
        "DELETE FROM reviews WHERE id=$1 AND product_id=$2 AND user_id=$3",
        review_id, product_id, user["id"]
    )
    # asyncpg returns "DELETE N"
    if result == "DELETE 0":
        raise HTTPException(404, "Review not found")
    await recompute_product_rating(product_id)
    return {"ok": True}


# ---------- Notifications ----------

@router.get("/notifications")
async def notifications(request: Request):
    user = await require_user(request)
    items = await db.fetch_all(
        "SELECT * FROM notifications WHERE user_id=$1 ORDER BY created_at DESC LIMIT 50",
        user["id"]
    )
    items = [dict(i) for i in items]
    for i in items:
        if isinstance(i.get("data"), str):
            try:
                i["data"] = json.loads(i["data"])
            except Exception:
                i["data"] = {}
    unread = sum(1 for i in items if not i.get("read"))
    return {"items": items, "unread": unread}


@router.post("/notifications/read")
async def mark_notifications_read(request: Request):
    user = await require_user(request)
    await db.execute("UPDATE notifications SET read=true WHERE user_id=$1", user["id"])
    return {"ok": True}


# ---------- Recently Viewed ----------

@router.get("/recently-viewed")
async def recently_viewed(request: Request):
    user = await require_user(request)
    rv = await db.fetch_all(
        "SELECT product_id FROM recently_viewed WHERE user_id=$1 ORDER BY viewed_at DESC LIMIT 10",
        user["id"]
    )
    ids = [r["product_id"] for r in rv]
    if not ids:
        return {"items": []}
    prods = await db.fetch_all("SELECT * FROM products WHERE id=ANY($1) AND active=true", ids)
    order = {p["id"]: dict(p) for p in prods}
    return {"items": [public_product(order[i]) for i in ids if i in order]}
