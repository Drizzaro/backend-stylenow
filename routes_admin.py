import json
import asyncio
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core import (
    db, iso_now, utcnow, new_id, require_admin, audit, notify, hub,
    credit_points, get_settings, public_product, hash_password, recompute_product_rating,
)
from storage import put_object, compress_image

router = APIRouter(prefix="/admin", tags=["admin"])

CATALOG_ROLES = ["product_manager"]
ORDER_ROLES = ["order_manager", "customer_support"]
MARKETING_ROLES = ["marketing_manager"]


# ---------- SSE realtime ----------

@router.get("/stream")
async def admin_stream(request: Request):
    await require_admin(request)
    q = hub.subscribe()

    async def gen():
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- Overview / KPIs ----------

@router.get("/overview")
async def overview(request: Request):
    await require_admin(request)
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    orders_today = await db.orders.find({"created_at": {"$gte": today}}, {"_id": 0}).to_list(5000)
    paid = [o for o in orders_today if o.get("payment_status") in ("paid", "cod")]
    revenue = sum(o["total"] for o in paid if o["status"] != "cancelled")
    status_counts = {}
    for o in orders_today:
        status_counts[o["status"]] = status_counts.get(o["status"], 0) + 1
    new_customers = await db.users.count_documents({"created_at": {"$gte": today}})
    refunds_today = await db.returns.count_documents({"status": "refunded", "updated_at": {"$gte": today}})
    settings = await get_settings()
    threshold = settings.get("low_stock_threshold", 5)
    products = await db.products.find({"active": True}, {"_id": 0, "id": 1, "name": 1, "variants": 1, "images": 1}).to_list(5000)
    low_stock, out_stock = [], []
    for p in products:
        p_oos = p.get("out_of_stock", False)
        for v in p.get("variants", []):
            v_oos = p_oos or v.get("out_of_stock", False)
            if v_oos or v.get("stock", 0) <= 0:
                out_stock.append({"product_id": p["id"], "name": p["name"], "variant": f"{v.get('color','')} {v.get('size','')}".strip(), "stock": v.get("stock", 0), "flagged": v_oos})
            elif v.get("stock", 0) <= threshold:
                low_stock.append({"product_id": p["id"], "name": p["name"], "variant": f"{v.get('color','')} {v.get('size','')}".strip(), "stock": v["stock"]})
    since = (utcnow() - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    chart_orders = await db.orders.find({"created_at": {"$gte": since}, "payment_status": "paid"}, {"_id": 0, "created_at": 1, "total": 1, "status": 1}).to_list(20000)
    by_day = {}
    for i in range(14):
        d = (utcnow() - timedelta(days=13 - i)).strftime("%Y-%m-%d")
        by_day[d] = {"date": d[5:], "orders": 0, "revenue": 0}
    for o in chart_orders:
        d = o["created_at"][:10]
        if d in by_day and o["status"] != "cancelled":
            by_day[d]["orders"] += 1
            by_day[d]["revenue"] += o["total"]
    top_products = await db.products.find({"active": True, "order_count": {"$gt": 0}}, {"_id": 0, "name": 1, "order_count": 1, "images": 1}).sort("order_count", -1).to_list(5)
    return {
        "today": {
            "orders": len(orders_today), "revenue": revenue,
            "aov": round(revenue / len(paid)) if paid else 0,
            "new_customers": new_customers,
            "placed": status_counts.get("placed", 0),
            "out_for_delivery": status_counts.get("out_for_delivery", 0),
            "delivered": status_counts.get("delivered", 0),
            "cancelled": status_counts.get("cancelled", 0),
            "refunds": refunds_today,
        },
        "low_stock": low_stock[:20], "out_of_stock": out_stock[:20],
        "chart": list(by_day.values()), "top_products": top_products,
        "totals": {
            "orders": await db.orders.count_documents({}),
            "customers": await db.users.count_documents({}),
            "products": await db.products.count_documents({"active": True}),
        },
    }


# ---------- Orders ----------

@router.get("/orders")
async def admin_orders(request: Request, status: str = "", q: str = "", page: int = 1, limit: int = 30):
    await require_admin(request)
    filt = {}
    if status:
        filt["status"] = status
    if q:
        filt["$or"] = [{"id": {"$regex": q, "$options": "i"}},
                       {"customer.name": {"$regex": q, "$options": "i"}},
                       {"customer.phone": {"$regex": q, "$options": "i"}}]
    total = await db.orders.count_documents(filt)
    items = await db.orders.find(filt, {"_id": 0}).sort("created_at", -1).skip((page - 1) * limit).limit(limit).to_list(limit)
    counts = {}
    for st in ["placed", "confirmed", "preparing", "packed", "out_for_delivery", "delivered", "cancelled", "returned", "refunded"]:
        counts[st] = await db.orders.count_documents({"status": st})
    return {"items": items, "total": total, "counts": counts}


@router.get("/orders/{order_id}")
async def admin_order_detail(order_id: str, request: Request):
    await require_admin(request)
    order = await db.orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(404, "Order not found")
    return {"order": order}


STATUS_LABELS = {
    "confirmed": "Order confirmed", "preparing": "Order being prepared", "packed": "Order packed",
    "out_for_delivery": "Out for delivery", "delivered": "Delivered", "cancelled": "Order cancelled",
    "returned": "Order returned", "refunded": "Order refunded",
}


class StatusBody(BaseModel):
    status: str
    note: str = ""


@router.put("/orders/{order_id}/status")
async def admin_update_status(order_id: str, body: StatusBody, request: Request):
    admin = await require_admin(request, ORDER_ROLES)
    order = await db.orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(404, "Order not found")
    valid = ["placed", "confirmed", "preparing", "packed", "out_for_delivery", "delivered", "cancelled", "returned", "refunded"]
    if body.status not in valid:
        raise HTTPException(400, "Invalid status")
    prev = order["status"]
    upd = {"status": body.status, "updated_at": iso_now()}
    if body.status == "refunded":
        upd["payment_status"] = "refunded"
        upd["refund_status"] = "processed"
    await db.orders.update_one({"id": order_id}, {
        "$set": upd,
        "$push": {"timeline": {"status": body.status, "at": iso_now(), "note": body.note or STATUS_LABELS.get(body.status, ""), "by": admin.get("email")}},
    })
    if body.status == "cancelled" and prev != "cancelled":
        for it in order["items"]:
            await db.products.update_one({"id": it["product_id"], "variants.id": it["variant_id"]},
                                         {"$inc": {"variants.$.stock": it["qty"]}})
        if order.get("points_redeemed"):
            await credit_points(order["user_id"], order["points_redeemed"], "refund", f"Order {order_id} cancelled", order_id)
    if body.status == "delivered" and not order.get("reward_points_awarded"):
        settings = await get_settings()
        pts = int(order["total"] * settings.get("points_per_rupee", 0.05))
        if pts > 0:
            await credit_points(order["user_id"], pts, "order_reward", f"Order {order_id} delivered", order_id)
            await db.orders.update_one({"id": order_id}, {"$set": {"reward_points_awarded": pts}})
            await notify(order["user_id"], "reward", "StylePoints earned",
                         f"You earned {pts} StylePoints from order {order_id}.", {"order_id": order_id})
    await notify(order["user_id"], "order", STATUS_LABELS.get(body.status, "Order update"),
                 f"Order {order_id}: {STATUS_LABELS.get(body.status, body.status)}.", {"order_id": order_id})
    hub.publish("order_update", {"order_id": order_id, "status": body.status})
    await audit(admin, "order_status", "order", order_id, prev=prev, new=body.status, request=request)
    return {"ok": True}


class RiderBody(BaseModel):
    name: str
    phone: str


@router.post("/orders/{order_id}/rider")
async def assign_rider(order_id: str, body: RiderBody, request: Request):
    admin = await require_admin(request, ORDER_ROLES)
    order = await db.orders.find_one({"id": order_id})
    if not order:
        raise HTTPException(404, "Order not found")
    rider = {"name": body.name.strip(), "phone": body.phone.strip()}
    await db.orders.update_one({"id": order_id}, {"$set": {"rider": rider, "updated_at": iso_now()},
        "$push": {"timeline": {"status": "rider_assigned", "at": iso_now(), "note": f"Rider {rider['name']} assigned"}}})
    await notify(order["user_id"], "order", "Rider assigned",
                 f"{rider['name']} is bringing your order {order_id}.", {"order_id": order_id})
    await audit(admin, "assign_rider", "order", order_id, new=rider, request=request)
    return {"ok": True}


class NoteBody(BaseModel):
    note: str


@router.post("/orders/{order_id}/notes")
async def add_order_note(order_id: str, body: NoteBody, request: Request):
    admin = await require_admin(request, ORDER_ROLES)
    note = {"note": body.note.strip(), "by": admin.get("email"), "at": iso_now()}
    res = await db.orders.update_one({"id": order_id}, {"$push": {"internal_notes": note}})
    if res.matched_count == 0:
        raise HTTPException(404, "Order not found")
    return {"ok": True}


# ---------- Products ----------

@router.get("/products")
async def admin_products(request: Request, q: str = "", page: int = 1, limit: int = 30):
    await require_admin(request)
    filt = {}
    if q:
        filt["$or"] = [{"name": {"$regex": q, "$options": "i"}}, {"tags": {"$regex": q, "$options": "i"}},
                       {"brand": {"$regex": q, "$options": "i"}}]
    total = await db.products.count_documents(filt)
    items = await db.products.find(filt, {"_id": 0}).sort("created_at", -1).skip((page - 1) * limit).limit(limit).to_list(limit)
    return {"items": items, "total": total}


class VariantIn(BaseModel):
    id: str = ""
    sku: str = ""
    color: str = ""
    size: str = ""
    price: float = 0
    mrp: float = 0
    stock: int = 0
    images: list[str] = []
    barcode: str = ""
    out_of_stock: bool = False


class ProductIn(BaseModel):
    name: str
    description: str = ""
    category_id: str = ""
    subcategory: str = ""
    brand: str = ""
    gender: str = ""
    material: str = ""
    fabric: str = ""
    tags: list[str] = []
    images: list[str] = []
    variants: list[VariantIn] = []
    seo_title: str = ""
    seo_description: str = ""
    attributes: dict = {}
    featured: bool = False
    active: bool = True
    out_of_stock: bool = False


def product_doc(body: ProductIn, existing: dict = None) -> dict:
    variants = []
    for v in body.variants:
        d = v.model_dump()
        d["id"] = d["id"] or new_id()
        if not d["mrp"]:
            d["mrp"] = d["price"]
        d["images"] = (d.get("images") or [])[:12]
        variants.append(d)
    doc = body.model_dump()
    doc["variants"] = variants
    doc["images"] = (doc.get("images") or [])[:12]
    doc["tags"] = [t.strip().lower() for t in body.tags if t.strip()]
    doc["updated_at"] = iso_now()
    return doc


@router.post("/products")
async def create_product(body: ProductIn, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    cat = await db.categories.find_one({"id": body.category_id}, {"_id": 0})
    doc = product_doc(body)
    doc.update({
        "id": new_id(), "category_name": cat["name"] if cat else "",
        "category_slug": cat.get("slug", "") if cat else "",
        "order_count": 0, "rating_avg": 0, "rating_count": 0, "created_at": iso_now(),
    })
    await db.products.insert_one(doc)
    await audit(admin, "create", "product", doc["id"], new={"name": doc["name"]}, request=request)
    doc.pop("_id", None)
    return {"product": doc}


@router.put("/products/{product_id}")
async def update_product(product_id: str, body: ProductIn, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    existing = await db.products.find_one({"id": product_id})
    if not existing:
        raise HTTPException(404, "Product not found")
    cat = await db.categories.find_one({"id": body.category_id}, {"_id": 0})
    doc = product_doc(body, existing)
    doc["category_name"] = cat["name"] if cat else ""
    doc["category_slug"] = cat.get("slug", "") if cat else ""
    prev_price = min((v.get("price", 0) for v in existing.get("variants", [])), default=0)
    new_price = min((v.price for v in body.variants), default=0)
    await db.products.update_one({"id": product_id}, {"$set": doc})
    await audit(admin, "update", "product", product_id,
                prev={"price": prev_price}, new={"price": new_price}, request=request)
    return {"ok": True}


@router.delete("/products/{product_id}")
async def delete_product(product_id: str, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    await db.products.update_one({"id": product_id}, {"$set": {"active": False, "updated_at": iso_now()}})
    await audit(admin, "deactivate", "product", product_id, request=request)
    return {"ok": True}


class StockBody(BaseModel):
    variant_id: str
    change: int
    reason: str = "manual adjustment"


class AvailabilityBody(BaseModel):
    out_of_stock: bool
    variant_id: str = ""  # empty string = whole product


@router.put("/products/{product_id}/availability")
async def set_availability(product_id: str, body: AvailabilityBody, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    if body.variant_id:
        res = await db.products.update_one(
            {"id": product_id, "variants.id": body.variant_id},
            {"$set": {"variants.$.out_of_stock": body.out_of_stock, "updated_at": iso_now()}})
    else:
        res = await db.products.update_one({"id": product_id},
            {"$set": {"out_of_stock": body.out_of_stock, "updated_at": iso_now()}})
    if res.matched_count == 0:
        raise HTTPException(404, "Product or variant not found")
    await audit(admin, "mark_out_of_stock" if body.out_of_stock else "mark_in_stock", "product", product_id,
                new={"scope": body.variant_id or "whole_product"}, request=request)
    p = await db.products.find_one({"id": product_id}, {"_id": 0})
    return {"product": p}


@router.post("/products/{product_id}/stock")
async def adjust_stock(product_id: str, body: StockBody, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    if body.change < 0:
        res = await db.products.find_one_and_update(
            {"id": product_id, "variants.id": body.variant_id, "variants.stock": {"$gte": -body.change}},
            {"$inc": {"variants.$.stock": body.change}})
        if not res:
            raise HTTPException(400, "Stock cannot go below zero")
    else:
        await db.products.update_one({"id": product_id, "variants.id": body.variant_id},
                                     {"$inc": {"variants.$.stock": body.change}})
    await db.inventory_transactions.insert_one({
        "id": new_id(), "product_id": product_id, "variant_id": body.variant_id,
        "change": body.change, "reason": body.reason, "by": admin.get("email"), "created_at": iso_now()})
    await audit(admin, "stock_adjust", "product", product_id,
                new={"variant": body.variant_id, "change": body.change}, request=request)
    p = await db.products.find_one({"id": product_id}, {"_id": 0})
    return {"product": p}


@router.get("/inventory/transactions")
async def inventory_transactions(request: Request):
    await require_admin(request)
    items = await db.inventory_transactions.find({}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return {"items": items}


# ---------- Categories ----------

@router.get("/categories")
async def admin_categories(request: Request):
    await require_admin(request)
    items = await db.categories.find({}, {"_id": 0}).sort("sort", 1).to_list(200)
    return {"items": items}


class CategoryIn(BaseModel):
    name: str
    image: str = ""
    active: bool = True
    sort: int = 0


@router.post("/categories")
async def create_category(body: CategoryIn, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    slug = re_slug(body.name)
    if await db.categories.find_one({"slug": slug}):
        raise HTTPException(400, "A category with this name already exists")
    doc = {"id": new_id(), "name": body.name.strip(), "slug": slug, "image": body.image,
           "active": body.active, "sort": body.sort, "created_at": iso_now()}
    await db.categories.insert_one(doc)
    await audit(admin, "create", "category", doc["id"], new={"name": doc["name"]}, request=request)
    doc.pop("_id", None)
    return {"category": doc}


@router.put("/categories/{cat_id}")
async def update_category(cat_id: str, body: CategoryIn, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    res = await db.categories.update_one({"id": cat_id}, {"$set": {
        "name": body.name.strip(), "slug": re_slug(body.name), "image": body.image,
        "active": body.active, "sort": body.sort}})
    if res.matched_count == 0:
        raise HTTPException(404, "Category not found")
    await db.products.update_many({"category_id": cat_id}, {"$set": {"category_name": body.name.strip()}})
    await audit(admin, "update", "category", cat_id, request=request)
    return {"ok": True}


@router.delete("/categories/{cat_id}")
async def delete_category(cat_id: str, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    await db.categories.delete_one({"id": cat_id})
    await audit(admin, "delete", "category", cat_id, request=request)
    return {"ok": True}


def re_slug(name: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


# ---------- Coupons ----------

@router.get("/coupons")
async def admin_coupons(request: Request):
    await require_admin(request)
    items = await db.coupons.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return {"items": items}


class CouponIn(BaseModel):
    code: str
    label: str = ""
    type: str = "percent"
    value: float = 0
    min_order: float = 0
    max_discount: float | None = None
    usage_limit: int | None = None
    per_user_limit: int | None = 1
    expires_at: str = ""
    active: bool = True
    first_order_only: bool = False


@router.post("/coupons")
async def create_coupon(body: CouponIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    code = body.code.strip().upper()
    if await db.coupons.find_one({"code": code}):
        raise HTTPException(400, "Coupon code already exists")
    doc = {"id": new_id(), **body.model_dump(), "code": code, "used_count": 0,
           "user_id": None, "created_at": iso_now()}
    await db.coupons.insert_one(doc)
    await audit(admin, "create", "coupon", code, new=body.model_dump(), request=request)
    doc.pop("_id", None)
    return {"coupon": doc}


@router.put("/coupons/{coupon_id}")
async def update_coupon(coupon_id: str, body: CouponIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    res = await db.coupons.update_one({"id": coupon_id}, {"$set": {**body.model_dump(), "code": body.code.strip().upper()}})
    if res.matched_count == 0:
        raise HTTPException(404, "Coupon not found")
    await audit(admin, "update", "coupon", coupon_id, new=body.model_dump(), request=request)
    return {"ok": True}


@router.post("/coupons/generate")
async def generate_coupon_code(request: Request):
    await require_admin(request, MARKETING_ROLES)
    import random, string
    for _ in range(10):
        code = "STYLE" + "".join(random.choices(string.digits, k=3))
        if not await db.coupons.find_one({"code": code}):
            return {"code": code}
    return {"code": "STYLE" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))}


# ---------- Deals & Homepage ----------

class DealIn(BaseModel):
    title: str
    discount_pct: float = 0
    product_ids: list[str] = []
    category_id: str = ""
    start_at: str = ""
    end_at: str = ""
    active: bool = True


@router.get("/deals")
async def admin_deals(request: Request):
    await require_admin(request, MARKETING_ROLES)
    items = await db.deals.find({}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return {"items": items}


@router.post("/deals")
async def create_deal(body: DealIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    doc = {"id": new_id(), **body.model_dump(), "created_at": iso_now()}
    await db.deals.insert_one(doc)
    await audit(admin, "create", "deal", doc["id"], new={"title": doc["title"]}, request=request)
    doc.pop("_id", None)
    return {"deal": doc}


@router.put("/deals/{deal_id}")
async def update_deal(deal_id: str, body: DealIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    res = await db.deals.update_one({"id": deal_id}, {"$set": body.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Deal not found")
    await audit(admin, "update", "deal", deal_id, request=request)
    return {"ok": True}


@router.delete("/deals/{deal_id}")
async def delete_deal(deal_id: str, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    await db.deals.delete_one({"id": deal_id})
    return {"ok": True}


class TickerIn(BaseModel):
    text: str
    icon: str = ""
    link: str = ""
    active: bool = True
    sort: int = 0
    start_at: str = ""
    end_at: str = ""


@router.get("/homepage")
async def admin_homepage(request: Request):
    await require_admin(request)
    ticker = await db.homepage_deals.find({}, {"_id": 0}).sort("sort", 1).to_list(50)
    banners = await db.banners.find({}, {"_id": 0}).sort("sort", 1).to_list(20)
    hp = await db.homepage.find_one({"id": "homepage"}, {"_id": 0}) or {"sections": []}
    return {"ticker": ticker, "banners": banners, "sections": hp.get("sections", [])}


@router.post("/homepage/ticker")
async def create_ticker(body: TickerIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    doc = {"id": new_id(), **body.model_dump(), "created_at": iso_now()}
    await db.homepage_deals.insert_one(doc)
    await audit(admin, "create", "ticker_deal", doc["id"], new={"text": doc["text"]}, request=request)
    doc.pop("_id", None)
    return {"deal": doc}


@router.put("/homepage/ticker/{deal_id}")
async def update_ticker(deal_id: str, body: TickerIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    res = await db.homepage_deals.update_one({"id": deal_id}, {"$set": body.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Ticker deal not found")
    return {"ok": True}


@router.delete("/homepage/ticker/{deal_id}")
async def delete_ticker(deal_id: str, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    await db.homepage_deals.delete_one({"id": deal_id})
    return {"ok": True}


class BannerIn(BaseModel):
    title: str
    subtitle: str = ""
    image: str
    link: str = ""
    active: bool = True
    sort: int = 0


@router.post("/homepage/banners")
async def create_banner(body: BannerIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    doc = {"id": new_id(), **body.model_dump(), "created_at": iso_now()}
    await db.banners.insert_one(doc)
    doc.pop("_id", None)
    return {"banner": doc}


@router.put("/homepage/banners/{banner_id}")
async def update_banner(banner_id: str, body: BannerIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    res = await db.banners.update_one({"id": banner_id}, {"$set": body.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Banner not found")
    return {"ok": True}


@router.delete("/homepage/banners/{banner_id}")
async def delete_banner(banner_id: str, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    await db.banners.delete_one({"id": banner_id})
    return {"ok": True}


class SectionsIn(BaseModel):
    sections: list[dict]


@router.put("/homepage/sections")
async def update_sections(body: SectionsIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    await db.homepage.update_one({"id": "homepage"}, {"$set": {"sections": body.sections}}, upsert=True)
    await audit(admin, "update", "homepage", "sections", request=request)
    return {"ok": True}


# ---------- Spin Wheel ----------

class SpinRewardIn(BaseModel):
    label: str
    type: str = "none"
    value: float = 0
    probability: float = 1
    expiry_days: int = 7
    active: bool = True


@router.get("/spin/rewards")
async def admin_spin_rewards(request: Request):
    await require_admin(request, MARKETING_ROLES)
    items = await db.spin_rewards.find({}, {"_id": 0}).to_list(50)
    spins = await db.spin_transactions.find({}, {"_id": 0}).sort("created_at", -1).to_list(50)
    return {"items": items, "recent_spins": spins}


@router.post("/spin/rewards")
async def create_spin_reward(body: SpinRewardIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    if body.type not in ("coupon_percent", "coupon_flat", "points", "free_delivery", "none"):
        raise HTTPException(400, "Invalid reward type")
    doc = {"id": new_id(), **body.model_dump(), "created_at": iso_now()}
    await db.spin_rewards.insert_one(doc)
    await audit(admin, "create", "spin_reward", doc["id"], new=body.model_dump(), request=request)
    doc.pop("_id", None)
    return {"reward": doc}


@router.put("/spin/rewards/{reward_id}")
async def update_spin_reward(reward_id: str, body: SpinRewardIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    res = await db.spin_rewards.update_one({"id": reward_id}, {"$set": body.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Reward not found")
    return {"ok": True}


@router.delete("/spin/rewards/{reward_id}")
async def delete_spin_reward(reward_id: str, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    await db.spin_rewards.delete_one({"id": reward_id})
    return {"ok": True}


# ---------- Customers ----------

@router.get("/customers")
async def admin_customers(request: Request, q: str = "", page: int = 1, limit: int = 30):
    await require_admin(request)
    filt = {}
    if q:
        filt["$or"] = [{"name": {"$regex": q, "$options": "i"}}, {"phone": {"$regex": q}},
                       {"email": {"$regex": q, "$options": "i"}}]
    total = await db.users.count_documents(filt)
    users = await db.users.find(filt, {"_id": 0}).sort("created_at", -1).skip((page - 1) * limit).limit(limit).to_list(limit)
    out = []
    for u in users:
        orders = await db.orders.find({"user_id": u["id"], "payment_status": "paid"}, {"_id": 0, "total": 1, "created_at": 1}).to_list(1000)
        wallet = await db.reward_accounts.find_one({"user_id": u["id"]}, {"_id": 0}) or {}
        out.append({
            "id": u["id"], "name": u.get("name", ""), "phone": u.get("phone", ""),
            "email": u.get("email", ""), "created_at": u.get("created_at"),
            "disabled": u.get("disabled", False), "total_orders": len(orders),
            "total_spent": sum(o["total"] for o in orders if o.get("total")),
            "last_order": max((o["created_at"] for o in orders), default=None),
            "points": wallet.get("balance", 0),
        })
    return {"items": out, "total": total}


class CustomerStatusBody(BaseModel):
    disabled: bool


@router.put("/customers/{user_id}/status")
async def set_customer_status(user_id: str, body: CustomerStatusBody, request: Request):
    admin = await require_admin(request, ["customer_support"])
    res = await db.users.update_one({"id": user_id}, {"$set": {"disabled": body.disabled}})
    if res.matched_count == 0:
        raise HTTPException(404, "Customer not found")
    await audit(admin, "disable" if body.disabled else "enable", "user", user_id, request=request)
    return {"ok": True}


class PointsBody(BaseModel):
    points: int
    note: str = ""


@router.post("/customers/{user_id}/points")
async def adjust_points(user_id: str, body: PointsBody, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    await credit_points(user_id, body.points, "admin_adjust", body.note or "Manual adjustment", "")
    await audit(admin, "points_adjust", "user", user_id, new={"points": body.points}, request=request)
    return {"ok": True}


# ---------- Reviews ----------

@router.get("/reviews")
async def admin_reviews(request: Request):
    await require_admin(request, ["customer_support"])
    items = await db.reviews.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return {"items": items}


class ReviewModBody(BaseModel):
    approved: bool


@router.delete("/reviews/{review_id}")
async def admin_delete_review(review_id: str, request: Request):
    admin = await require_admin(request, ["customer_support"])
    review = await db.reviews.find_one({"id": review_id}, {"_id": 0})
    if not review:
        raise HTTPException(404, "Review not found")
    await db.reviews.delete_one({"id": review_id})
    await recompute_product_rating(review["product_id"])
    await audit(admin, "delete_review", "review", review_id, request=request)
    return {"ok": True}


@router.put("/reviews/{review_id}")
async def moderate_review(review_id: str, body: ReviewModBody, request: Request):
    admin = await require_admin(request, ["customer_support"])
    res = await db.reviews.update_one({"id": review_id}, {"$set": {"approved": body.approved}})
    if res.matched_count == 0:
        raise HTTPException(404, "Review not found")
    await audit(admin, "moderate_review", "review", review_id, new={"approved": body.approved}, request=request)
    return {"ok": True}


# ---------- Returns ----------

@router.get("/returns")
async def admin_returns(request: Request):
    await require_admin(request, ORDER_ROLES)
    items = await db.returns.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return {"items": items}


class ReturnActionBody(BaseModel):
    action: str


@router.put("/returns/{return_id}")
async def action_return(return_id: str, body: ReturnActionBody, request: Request):
    admin = await require_admin(request, ORDER_ROLES)
    ret = await db.returns.find_one({"id": return_id}, {"_id": 0})
    if not ret:
        raise HTTPException(404, "Return not found")
    if body.action not in ("approved", "rejected", "refunded"):
        raise HTTPException(400, "Invalid action")
    await db.returns.update_one({"id": return_id}, {"$set": {"status": body.action, "updated_at": iso_now()}})
    if body.action == "refunded":
        await db.orders.update_one({"id": ret["order_id"]}, {"$set": {"status": "refunded", "payment_status": "refunded", "updated_at": iso_now()},
            "$push": {"timeline": {"status": "refunded", "at": iso_now(), "note": f"Return {return_id} refunded"}}})
        order = await db.orders.find_one({"id": ret["order_id"]}, {"_id": 0})
        if order:
            for it in order.get("items", []):
                await db.products.update_one({"id": it["product_id"], "variants.id": it["variant_id"]},
                                             {"$inc": {"variants.$.stock": it["qty"]}})
        await notify(ret["user_id"], "refund", "Refund processed",
                     f"Refund for order {ret['order_id']} has been processed.", {"order_id": ret["order_id"]})
    await audit(admin, f"return_{body.action}", "return", return_id, request=request)
    return {"ok": True}


# ---------- Delivery partners ----------

class PartnerIn(BaseModel):
    name: str
    phone: str
    zone: str = ""
    active: bool = True


@router.get("/delivery/partners")
async def list_partners(request: Request):
    await require_admin(request, ORDER_ROLES)
    items = await db.delivery_partners.find({}, {"_id": 0}).to_list(100)
    return {"items": items}


@router.post("/delivery/partners")
async def create_partner(body: PartnerIn, request: Request):
    admin = await require_admin(request, ORDER_ROLES)
    doc = {"id": new_id(), **body.model_dump(), "created_at": iso_now()}
    await db.delivery_partners.insert_one(doc)
    doc.pop("_id", None)
    return {"partner": doc}


@router.delete("/delivery/partners/{partner_id}")
async def delete_partner(partner_id: str, request: Request):
    admin = await require_admin(request, ORDER_ROLES)
    await db.delivery_partners.delete_one({"id": partner_id})
    return {"ok": True}


# ---------- Synonyms ----------

class SynonymIn(BaseModel):
    keyword: str
    synonyms: list[str] = []


@router.get("/synonyms")
async def list_synonyms(request: Request):
    await require_admin(request, CATALOG_ROLES)
    items = await db.search_synonyms.find({}, {"_id": 0}).to_list(200)
    return {"items": items}


@router.post("/synonyms")
async def create_synonym(body: SynonymIn, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    kw = body.keyword.strip().lower()
    if await db.search_synonyms.find_one({"keyword": kw}):
        raise HTTPException(400, "Synonym group already exists")
    doc = {"id": new_id(), "keyword": kw, "synonyms": [s.strip().lower() for s in body.synonyms if s.strip()]}
    await db.search_synonyms.insert_one(doc)
    await audit(admin, "create", "synonym", kw, request=request)
    doc.pop("_id", None)
    return {"synonym": doc}


@router.delete("/synonyms/{syn_id}")
async def delete_synonym(syn_id: str, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    await db.search_synonyms.delete_one({"id": syn_id})
    return {"ok": True}


# ---------- Settings ----------

class SettingsIn(BaseModel):
    delivery_fee: float | None = None
    delivery_eta_min: int | None = None
    delivery_eta_max: int | None = None
    points_per_spin: int | None = None
    points_per_rupee: float | None = None
    points_value_rupee: float | None = None
    low_stock_threshold: int | None = None
    spin_enabled: bool | None = None
    city: str | None = None
    brand_accent: str | None = None
    social_links: dict | None = None
    contact_phones: list | None = None
    try_at_doorstep_threshold: float | None = None
    try_at_doorstep_fee: float | None = None
    try_at_doorstep_enabled: bool | None = None


@router.get("/settings")
async def admin_settings(request: Request):
    await require_admin(request)
    return {"settings": await get_settings()}


@router.put("/settings")
async def update_settings(body: SettingsIn, request: Request):
    admin = await require_admin(request)
    upd = {k: v for k, v in body.model_dump().items() if v is not None}
    await db.settings.update_one({"id": "global"}, {"$set": upd}, upsert=True)
    await audit(admin, "update", "settings", "global", new=upd, request=request)
    return {"settings": await get_settings()}


# ---------- File uploads (object storage) ----------

ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
ALLOWED_VIDEO_EXT = {"mp4", "webm", "mov", "m4v"}


@router.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    await require_admin(request)
    ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""
    if ext in ALLOWED_IMAGE_EXT:
        kind = "image"
        max_size = 8 * 1024 * 1024
    elif ext in ALLOWED_VIDEO_EXT:
        kind = "video"
        max_size = 80 * 1024 * 1024
    else:
        raise HTTPException(400, "Unsupported file type. Use images (jpg/png/webp) or videos (mp4/webm).")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if kind == "image":
        data, ctype, ext = compress_image(data)
    elif len(data) > max_size:
        raise HTTPException(400, f"File too large (max {max_size // (1024 * 1024)}MB for {kind}s)")
    path = f"stylenow/{kind}s/{new_id()}.{ext}"
    result = put_object(path, data, "image/jpeg" if kind == "image" else (file.content_type or "application/octet-stream"))
    await db.files.insert_one({
        "id": new_id(), "storage_path": result["path"], "original_filename": file.filename,
        "content_type": "image/jpeg" if kind == "image" else file.content_type, "size": result.get("size", len(data)),
        "kind": kind, "public": True, "is_deleted": False, "created_at": iso_now(),
    })
    return {"path": result["path"], "url": f"/api/files/{result['path']}", "kind": kind}


# ---------- Media library ----------

@router.get("/files")
async def admin_files(request: Request, kind: str = ""):
    await require_admin(request)
    filt = {"is_deleted": False}
    if kind in ("image", "video"):
        filt["kind"] = kind
    items = await db.files.find(filt, {"_id": 0}).sort("created_at", -1).to_list(500)
    for f in items:
        f["url"] = f"/api/files/{f['storage_path']}"
    return {"items": items}


@router.delete("/files/{file_id}")
async def delete_file(file_id: str, request: Request):
    admin = await require_admin(request)
    res = await db.files.update_one({"id": file_id}, {"$set": {"is_deleted": True}})
    if res.matched_count == 0:
        raise HTTPException(404, "File not found")
    await audit(admin, "delete", "file", file_id, request=request)
    return {"ok": True}


# ---------- Videos (admin-managed ads / reviews) ----------

class VideoIn(BaseModel):
    username: str
    caption: str = ""
    product_id: str = ""
    video: str
    poster: str = ""
    kind: str = "review"
    active: bool = True
    sort: int = 0


@router.get("/videos")
async def admin_videos(request: Request):
    await require_admin(request)
    items = await db.videos.find({}, {"_id": 0}).sort("sort", 1).to_list(200)
    for v in items:
        p = await db.products.find_one({"id": v.get("product_id")}, {"_id": 0, "name": 1})
        v["product_name"] = p["name"] if p else None
    return {"items": items}


@router.post("/videos")
async def create_video(body: VideoIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    if body.kind not in ("review", "ad"):
        raise HTTPException(400, "Kind must be review or ad")
    if body.product_id and not await db.products.find_one({"id": body.product_id}):
        raise HTTPException(400, "Attached product not found")
    doc = {"id": new_id(), **body.model_dump(), "created_at": iso_now()}
    await db.videos.insert_one(doc)
    await audit(admin, "create", "video", doc["id"], new={"username": doc["username"], "kind": doc["kind"]}, request=request)
    doc.pop("_id", None)
    return {"video": doc}


@router.put("/videos/{video_id}")
async def update_video(video_id: str, body: VideoIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    res = await db.videos.update_one({"id": video_id}, {"$set": body.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Video not found")
    await audit(admin, "update", "video", video_id, request=request)
    return {"ok": True}


@router.delete("/videos/{video_id}")
async def delete_video(video_id: str, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    v = await db.videos.find_one({"id": video_id})
    if v:
        await db.files.update_many({"storage_path": v.get("video")}, {"$set": {"is_deleted": True}})
    await db.videos.delete_one({"id": video_id})
    await audit(admin, "delete", "video", video_id, request=request)
    return {"ok": True}


# ---------- Analytics & audit ----------

@router.get("/analytics/search")
async def search_analytics(request: Request):
    await require_admin(request)
    popular = await db.search_logs.aggregate([
        {"$group": {"_id": "$query", "searches": {"$sum": 1}, "avg_results": {"$avg": "$results"},
                    "clicks": {"$sum": {"$cond": [{"$ne": ["$clicked_product", None]}, 1, 0]}}}},
        {"$sort": {"searches": -1}}, {"$limit": 20},
    ]).to_list(20)
    zero = await db.search_logs.aggregate([
        {"$match": {"results": 0}},
        {"$group": {"_id": "$query", "searches": {"$sum": 1}}},
        {"$sort": {"searches": -1}}, {"$limit": 20},
    ]).to_list(20)
    return {"popular": popular, "zero_results": zero}


@router.get("/audit-logs")
async def audit_logs(request: Request, page: int = 1, limit: int = 50):
    await require_admin(request)
    total = await db.audit_logs.count_documents({})
    items = await db.audit_logs.find({}, {"_id": 0}).sort("created_at", -1).skip((page - 1) * limit).limit(limit).to_list(limit)
    return {"items": items, "total": total}
