import json
import asyncio
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core import (
    iso_now, utcnow, new_id, require_admin, audit, notify, hub,
    credit_points, get_settings, public_product, hash_password,
    recompute_product_rating, _parse_jsonb,
)
from storage import put_object, compress_image
import db

router = APIRouter(prefix="/admin", tags=["admin"])

CATALOG_ROLES = ["product_manager"]
ORDER_ROLES = ["order_manager", "customer_support"]
MARKETING_ROLES = ["marketing_manager"]


def _j(val, default=None):
    """Parse a JSONB field that might be a string."""
    if val is None:
        return default
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return val


def _order_dict(row) -> dict:
    if not row:
        return {}
    d = dict(row)
    for f in ("customer", "items", "address", "try_at_doorstep", "timeline",
              "internal_notes", "rider", "refund_details"):
        d[f] = _j(d.get(f), [] if f in ("items", "timeline", "internal_notes") else {})
    return d


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

    orders_today = await db.fetch_all(
        "SELECT * FROM orders WHERE created_at >= $1", today
    )
    orders_today = [_order_dict(o) for o in orders_today]
    paid = [o for o in orders_today if o.get("payment_status") in ("paid", "cod")]
    revenue = sum(o["total"] for o in paid if o["status"] != "cancelled")
    status_counts = {}
    for o in orders_today:
        status_counts[o["status"]] = status_counts.get(o["status"], 0) + 1

    new_customers = await db.fetch_val("SELECT COUNT(*) FROM users WHERE created_at >= $1", today)
    refunds_today = await db.fetch_val(
        "SELECT COUNT(*) FROM returns WHERE status='refunded' AND updated_at >= $1", today
    )
    settings = await get_settings()
    threshold = settings.get("low_stock_threshold", 5)

    products = await db.fetch_all(
        "SELECT id, name, variants, images FROM products WHERE active=true"
    )
    low_stock, out_stock = [], []
    for p in products:
        p_oos = p.get("out_of_stock", False)
        for v in _parse_jsonb(p["variants"], []):
            v_oos = p_oos or v.get("out_of_stock", False)
            variant_str = f"{v.get('color', '')} {v.get('size', '')}".strip()
            if v_oos or v.get("stock", 0) <= 0:
                out_stock.append({"product_id": p["id"], "name": p["name"],
                                  "variant": variant_str, "stock": v.get("stock", 0), "flagged": v_oos})
            elif v.get("stock", 0) <= threshold:
                low_stock.append({"product_id": p["id"], "name": p["name"],
                                  "variant": variant_str, "stock": v["stock"]})

    since = (utcnow() - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    # Revenue chart using SQL GROUP BY
    chart_rows = await db.fetch_all(
        """SELECT LEFT(created_at, 10) as date, COUNT(*) as orders, SUM(total) as revenue
           FROM orders WHERE created_at >= $1 AND payment_status='paid' AND status != 'cancelled'
           GROUP BY 1 ORDER BY 1""",
        since
    )
    by_day = {}
    for i in range(14):
        d = (utcnow() - timedelta(days=13 - i)).strftime("%Y-%m-%d")
        by_day[d] = {"date": d[5:], "orders": 0, "revenue": 0}
    for row in chart_rows:
        d = row["date"]
        if d in by_day:
            by_day[d]["orders"] = int(row["orders"])
            by_day[d]["revenue"] = float(row["revenue"] or 0)

    top_products = await db.fetch_all(
        "SELECT name, order_count, images FROM products WHERE active=true AND order_count > 0 ORDER BY order_count DESC LIMIT 5"
    )

    totals_orders = await db.fetch_val("SELECT COUNT(*) FROM orders")
    totals_customers = await db.fetch_val("SELECT COUNT(*) FROM users")
    totals_products = await db.fetch_val("SELECT COUNT(*) FROM products WHERE active=true")

    return {
        "today": {
            "orders": len(orders_today), "revenue": revenue,
            "aov": round(revenue / len(paid)) if paid else 0,
            "new_customers": new_customers or 0,
            "placed": status_counts.get("placed", 0),
            "out_for_delivery": status_counts.get("out_for_delivery", 0),
            "delivered": status_counts.get("delivered", 0),
            "cancelled": status_counts.get("cancelled", 0),
            "refunds": refunds_today or 0,
        },
        "low_stock": low_stock[:20], "out_of_stock": out_stock[:20],
        "chart": list(by_day.values()),
        "top_products": [dict(p) for p in top_products],
        "totals": {"orders": totals_orders or 0, "customers": totals_customers or 0, "products": totals_products or 0},
    }


# ---------- Orders ----------

@router.get("/orders")
async def admin_orders(request: Request, status: str = "", q: str = "", page: int = 1, limit: int = 30):
    await require_admin(request)
    conditions, args = [], []

    def add(cond, val):
        args.append(val)
        conditions.append(f"{cond}=${len(args)}")

    if status:
        add("status", status)
    if q:
        args.append(f"%{q}%")
        conditions.append(
            f"(id ILIKE ${len(args)} OR customer->>'name' ILIKE ${len(args)} OR customer->>'phone' ILIKE ${len(args)})"
        )

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    total = await db.fetch_val(f"SELECT COUNT(*) FROM orders {where}", *args)
    items = await db.fetch_all(
        f"SELECT * FROM orders {where} ORDER BY created_at DESC OFFSET {(page-1)*limit} LIMIT {limit}",
        *args
    )

    # Status counts in a single query
    count_rows = await db.fetch_all(
        "SELECT status, COUNT(*) as n FROM orders GROUP BY status"
    )
    counts = {r["status"]: int(r["n"]) for r in count_rows}

    return {"items": [_order_dict(o) for o in items], "total": total or 0, "counts": counts}


@router.get("/orders/{order_id}")
async def admin_order_detail(order_id: str, request: Request):
    await require_admin(request)
    order = await db.fetch_one("SELECT * FROM orders WHERE id=$1", order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    return {"order": _order_dict(order)}


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
    order = await db.fetch_one("SELECT * FROM orders WHERE id=$1", order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    valid = ["placed", "confirmed", "preparing", "packed", "out_for_delivery", "delivered",
             "cancelled", "returned", "refunded"]
    if body.status not in valid:
        raise HTTPException(400, "Invalid status")
    order = _order_dict(order)
    prev = order["status"]

    timeline = list(order.get("timeline") or [])
    timeline.append({"status": body.status, "at": iso_now(),
                     "note": body.note or STATUS_LABELS.get(body.status, ""), "by": admin.get("email")})

    payment_status_upd = "refunded" if body.status == "refunded" else order.get("payment_status")
    refund_status_upd = "processed" if body.status == "refunded" else order.get("refund_status")

    await db.execute(
        """UPDATE orders SET status=$1, updated_at=$2, timeline=$3,
           payment_status=$4, refund_status=$5 WHERE id=$6""",
        body.status, iso_now(), json.dumps(timeline),
        payment_status_upd, refund_status_upd, order_id
    )

    if body.status == "cancelled" and prev != "cancelled":
        for it in _parse_jsonb(order.get("items"), []):
            await db.execute("SELECT increment_variant_stock($1,$2,$3)",
                             it["product_id"], it["variant_id"], it["qty"])
        if order.get("points_redeemed"):
            await credit_points(order["user_id"], order["points_redeemed"], "refund",
                                f"Order {order_id} cancelled", order_id)

    if body.status == "delivered" and not order.get("reward_points_awarded"):
        settings = await get_settings()
        pts = int(order["total"] * settings.get("points_per_rupee", 0.05))
        if pts > 0:
            await credit_points(order["user_id"], pts, "order_reward",
                                f"Order {order_id} delivered", order_id)
            await db.execute("UPDATE orders SET reward_points_awarded=$1 WHERE id=$2", pts, order_id)
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
    order = await db.fetch_one("SELECT user_id FROM orders WHERE id=$1", order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    rider = {"name": body.name.strip(), "phone": body.phone.strip()}
    order_full = _order_dict(await db.fetch_one("SELECT * FROM orders WHERE id=$1", order_id))
    timeline = list(order_full.get("timeline") or [])
    timeline.append({"status": "rider_assigned", "at": iso_now(),
                     "note": f"Rider {rider['name']} assigned"})
    await db.execute(
        "UPDATE orders SET rider=$1, updated_at=$2, timeline=$3 WHERE id=$4",
        json.dumps(rider), iso_now(), json.dumps(timeline), order_id
    )
    await notify(order["user_id"], "order", "Rider assigned",
                 f"{rider['name']} is bringing your order {order_id}.", {"order_id": order_id})
    await audit(admin, "assign_rider", "order", order_id, new=rider, request=request)
    return {"ok": True}


class NoteBody(BaseModel):
    note: str


@router.post("/orders/{order_id}/notes")
async def add_order_note(order_id: str, body: NoteBody, request: Request):
    admin = await require_admin(request, ORDER_ROLES)
    order = await db.fetch_one("SELECT internal_notes FROM orders WHERE id=$1", order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    notes = _j(order["internal_notes"], [])
    notes.append({"note": body.note.strip(), "by": admin.get("email"), "at": iso_now()})
    await db.execute("UPDATE orders SET internal_notes=$1 WHERE id=$2", json.dumps(notes), order_id)
    return {"ok": True}


# ---------- Products ----------

@router.get("/products")
async def admin_products(request: Request, q: str = "", page: int = 1, limit: int = 30):
    await require_admin(request)
    if q:
        total = await db.fetch_val(
            "SELECT COUNT(*) FROM products WHERE name ILIKE $1 OR brand ILIKE $1 OR tags::text ILIKE $1",
            f"%{q}%"
        )
        items = await db.fetch_all(
            "SELECT * FROM products WHERE name ILIKE $1 OR brand ILIKE $1 OR tags::text ILIKE $1 ORDER BY created_at DESC OFFSET $2 LIMIT $3",
            f"%{q}%", (page - 1) * limit, limit
        )
    else:
        total = await db.fetch_val("SELECT COUNT(*) FROM products")
        items = await db.fetch_all(
            "SELECT * FROM products ORDER BY created_at DESC OFFSET $1 LIMIT $2",
            (page - 1) * limit, limit
        )
    return {"items": [dict(p) for p in items], "total": total or 0}


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


def _build_variants(variant_list):
    variants = []
    for v in variant_list:
        d = v.model_dump()
        d["id"] = d["id"] or new_id()
        if not d["mrp"]:
            d["mrp"] = d["price"]
        d["images"] = (d.get("images") or [])[:12]
        variants.append(d)
    return variants


@router.post("/products")
async def create_product(body: ProductIn, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    cat = await db.fetch_one("SELECT name, slug FROM categories WHERE id=$1", body.category_id)
    pid = new_id()
    variants = _build_variants(body.variants)
    tags = [t.strip().lower() for t in body.tags if t.strip()]
    await db.execute(
        """INSERT INTO products (
            id, name, description, category_id, category_name, category_slug,
            subcategory, brand, gender, material, fabric, tags, images, variants,
            seo_title, seo_description, attributes, featured, active, out_of_stock,
            order_count, rating_avg, rating_count, created_at, updated_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,0,0,0,$21,$21)""",
        pid, body.name, body.description, body.category_id,
        cat["name"] if cat else "", cat["slug"] if cat else "",
        body.subcategory, body.brand, body.gender, body.material, body.fabric,
        tags, body.images[:12], json.dumps(variants),
        body.seo_title, body.seo_description, json.dumps(body.attributes),
        body.featured, body.active, body.out_of_stock, iso_now()
    )
    await audit(admin, "create", "product", pid, new={"name": body.name}, request=request)
    product = await db.fetch_one("SELECT * FROM products WHERE id=$1", pid)
    return {"product": dict(product)}


@router.put("/products/{product_id}")
async def update_product(product_id: str, body: ProductIn, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    existing = await db.fetch_one("SELECT * FROM products WHERE id=$1", product_id)
    if not existing:
        raise HTTPException(404, "Product not found")
    cat = await db.fetch_one("SELECT name, slug FROM categories WHERE id=$1", body.category_id)
    variants = _build_variants(body.variants)
    tags = [t.strip().lower() for t in body.tags if t.strip()]
    prev_price = min((v.get("price", 0) for v in _parse_jsonb(existing["variants"], [])), default=0)
    new_price = min((v.price for v in body.variants), default=0)
    await db.execute(
        """UPDATE products SET
            name=$1, description=$2, category_id=$3, category_name=$4, category_slug=$5,
            subcategory=$6, brand=$7, gender=$8, material=$9, fabric=$10,
            tags=$11, images=$12, variants=$13, seo_title=$14, seo_description=$15,
            attributes=$16, featured=$17, active=$18, out_of_stock=$19, updated_at=$20
           WHERE id=$21""",
        body.name, body.description, body.category_id,
        cat["name"] if cat else "", cat["slug"] if cat else "",
        body.subcategory, body.brand, body.gender, body.material, body.fabric,
        tags, body.images[:12], json.dumps(variants),
        body.seo_title, body.seo_description, json.dumps(body.attributes),
        body.featured, body.active, body.out_of_stock, iso_now(), product_id
    )
    await audit(admin, "update", "product", product_id,
                prev={"price": prev_price}, new={"price": new_price}, request=request)
    return {"ok": True}


@router.delete("/products/{product_id}")
async def delete_product(product_id: str, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    await db.execute("UPDATE products SET active=false, updated_at=$1 WHERE id=$2", iso_now(), product_id)
    await audit(admin, "deactivate", "product", product_id, request=request)
    return {"ok": True}


class StockBody(BaseModel):
    variant_id: str
    change: int
    reason: str = "manual adjustment"


class AvailabilityBody(BaseModel):
    out_of_stock: bool
    variant_id: str = ""


@router.put("/products/{product_id}/availability")
async def set_availability(product_id: str, body: AvailabilityBody, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    product = await db.fetch_one("SELECT variants FROM products WHERE id=$1", product_id)
    if not product:
        raise HTTPException(404, "Product or variant not found")
    if body.variant_id:
        variants = _parse_jsonb(product["variants"], [])
        updated = False
        for v in variants:
            if v["id"] == body.variant_id:
                v["out_of_stock"] = body.out_of_stock
                updated = True
                break
        if not updated:
            raise HTTPException(404, "Variant not found")
        await db.execute("UPDATE products SET variants=$1, updated_at=$2 WHERE id=$3",
                         json.dumps(variants), iso_now(), product_id)
    else:
        await db.execute("UPDATE products SET out_of_stock=$1, updated_at=$2 WHERE id=$3",
                         body.out_of_stock, iso_now(), product_id)
    await audit(admin, "mark_out_of_stock" if body.out_of_stock else "mark_in_stock",
                "product", product_id, new={"scope": body.variant_id or "whole_product"}, request=request)
    p = await db.fetch_one("SELECT * FROM products WHERE id=$1", product_id)
    return {"product": dict(p)}


@router.post("/products/{product_id}/stock")
async def adjust_stock(product_id: str, body: StockBody, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    if body.change < 0:
        result = await db.fetch_val(
            "SELECT decrement_variant_stock($1,$2,$3)", product_id, body.variant_id, -body.change
        )
        if result is None:
            raise HTTPException(400, "Stock cannot go below zero")
    else:
        await db.execute("SELECT increment_variant_stock($1,$2,$3)", product_id, body.variant_id, body.change)
    await db.execute(
        "INSERT INTO inventory_transactions (id, product_id, variant_id, change, reason, by, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        new_id(), product_id, body.variant_id, body.change, body.reason, admin.get("email"), iso_now()
    )
    await audit(admin, "stock_adjust", "product", product_id,
                new={"variant": body.variant_id, "change": body.change}, request=request)
    p = await db.fetch_one("SELECT * FROM products WHERE id=$1", product_id)
    return {"product": dict(p)}


@router.get("/inventory/transactions")
async def inventory_transactions(request: Request):
    await require_admin(request)
    items = await db.fetch_all("SELECT * FROM inventory_transactions ORDER BY created_at DESC LIMIT 100")
    return {"items": [dict(i) for i in items]}


# ---------- Categories ----------

def re_slug(name: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


@router.get("/categories")
async def admin_categories(request: Request):
    await require_admin(request)
    items = await db.fetch_all("SELECT * FROM categories ORDER BY sort ASC")
    return {"items": [dict(c) for c in items]}


class CategoryIn(BaseModel):
    name: str
    image: str = ""
    active: bool = True
    sort: int = 0


@router.post("/categories")
async def create_category(body: CategoryIn, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    slug = re_slug(body.name)
    existing = await db.fetch_one("SELECT id FROM categories WHERE slug=$1", slug)
    if existing:
        raise HTTPException(400, "A category with this name already exists")
    cid = new_id()
    await db.execute(
        "INSERT INTO categories (id, name, slug, image, active, sort, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        cid, body.name.strip(), slug, body.image, body.active, body.sort, iso_now()
    )
    await audit(admin, "create", "category", cid, new={"name": body.name}, request=request)
    cat = await db.fetch_one("SELECT * FROM categories WHERE id=$1", cid)
    return {"category": dict(cat)}


@router.put("/categories/{cat_id}")
async def update_category(cat_id: str, body: CategoryIn, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    result = await db.execute(
        "UPDATE categories SET name=$1, slug=$2, image=$3, active=$4, sort=$5 WHERE id=$6",
        body.name.strip(), re_slug(body.name), body.image, body.active, body.sort, cat_id
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Category not found")
    await db.execute("UPDATE products SET category_name=$1 WHERE category_id=$2",
                     body.name.strip(), cat_id)
    await audit(admin, "update", "category", cat_id, request=request)
    return {"ok": True}


@router.delete("/categories/{cat_id}")
async def delete_category(cat_id: str, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    await db.execute("DELETE FROM categories WHERE id=$1", cat_id)
    await audit(admin, "delete", "category", cat_id, request=request)
    return {"ok": True}


# ---------- Coupons ----------

@router.get("/coupons")
async def admin_coupons(request: Request):
    await require_admin(request)
    items = await db.fetch_all("SELECT * FROM coupons ORDER BY created_at DESC LIMIT 200")
    return {"items": [dict(c) for c in items]}


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
    existing = await db.fetch_one("SELECT id FROM coupons WHERE code=$1", code)
    if existing:
        raise HTTPException(400, "Coupon code already exists")
    cid = new_id()
    await db.execute(
        """INSERT INTO coupons (id, code, label, type, value, min_order, max_discount, usage_limit,
           per_user_limit, used_count, expires_at, active, first_order_only, user_id, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,0,$10,$11,$12,null,$13)""",
        cid, code, body.label, body.type, body.value, body.min_order,
        body.max_discount, body.usage_limit, body.per_user_limit,
        body.expires_at, body.active, body.first_order_only, iso_now()
    )
    await audit(admin, "create", "coupon", code, new=body.model_dump(), request=request)
    coupon = await db.fetch_one("SELECT * FROM coupons WHERE id=$1", cid)
    return {"coupon": dict(coupon)}


@router.put("/coupons/{coupon_id}")
async def update_coupon(coupon_id: str, body: CouponIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    result = await db.execute(
        """UPDATE coupons SET code=$1, label=$2, type=$3, value=$4, min_order=$5,
           max_discount=$6, usage_limit=$7, per_user_limit=$8, expires_at=$9,
           active=$10, first_order_only=$11 WHERE id=$12""",
        body.code.strip().upper(), body.label, body.type, body.value, body.min_order,
        body.max_discount, body.usage_limit, body.per_user_limit, body.expires_at,
        body.active, body.first_order_only, coupon_id
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Coupon not found")
    await audit(admin, "update", "coupon", coupon_id, new=body.model_dump(), request=request)
    return {"ok": True}


@router.post("/coupons/generate")
async def generate_coupon_code(request: Request):
    await require_admin(request, MARKETING_ROLES)
    import random, string
    for _ in range(10):
        code = "STYLE" + "".join(random.choices(string.digits, k=3))
        existing = await db.fetch_one("SELECT id FROM coupons WHERE code=$1", code)
        if not existing:
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
    items = await db.fetch_all("SELECT * FROM deals ORDER BY created_at DESC LIMIT 100")
    return {"items": [dict(d) for d in items]}


@router.post("/deals")
async def create_deal(body: DealIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    did = new_id()
    await db.execute(
        """INSERT INTO deals (id, title, discount_pct, product_ids, category_id, start_at, end_at, active, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
        did, body.title, body.discount_pct, body.product_ids,
        body.category_id, body.start_at, body.end_at, body.active, iso_now()
    )
    await audit(admin, "create", "deal", did, new={"title": body.title}, request=request)
    deal = await db.fetch_one("SELECT * FROM deals WHERE id=$1", did)
    return {"deal": dict(deal)}


@router.put("/deals/{deal_id}")
async def update_deal(deal_id: str, body: DealIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    result = await db.execute(
        """UPDATE deals SET title=$1, discount_pct=$2, product_ids=$3, category_id=$4,
           start_at=$5, end_at=$6, active=$7 WHERE id=$8""",
        body.title, body.discount_pct, body.product_ids, body.category_id,
        body.start_at, body.end_at, body.active, deal_id
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Deal not found")
    await audit(admin, "update", "deal", deal_id, request=request)
    return {"ok": True}


@router.delete("/deals/{deal_id}")
async def delete_deal(deal_id: str, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    await db.execute("DELETE FROM deals WHERE id=$1", deal_id)
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
    ticker = await db.fetch_all("SELECT * FROM homepage_deals ORDER BY sort ASC LIMIT 50")
    banners = await db.fetch_all("SELECT * FROM banners ORDER BY sort ASC LIMIT 20")
    hp = await db.fetch_one("SELECT sections FROM homepage WHERE id='homepage'")
    return {
        "ticker": [dict(t) for t in ticker],
        "banners": [dict(b) for b in banners],
        "sections": _j(hp["sections"] if hp else None, []),
    }


@router.post("/homepage/ticker")
async def create_ticker(body: TickerIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    tid = new_id()
    await db.execute(
        "INSERT INTO homepage_deals (id, text, icon, link, active, sort, start_at, end_at, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
        tid, body.text, body.icon, body.link, body.active, body.sort, body.start_at, body.end_at, iso_now()
    )
    await audit(admin, "create", "ticker_deal", tid, new={"text": body.text}, request=request)
    deal = await db.fetch_one("SELECT * FROM homepage_deals WHERE id=$1", tid)
    return {"deal": dict(deal)}


@router.put("/homepage/ticker/{deal_id}")
async def update_ticker(deal_id: str, body: TickerIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    result = await db.execute(
        "UPDATE homepage_deals SET text=$1, icon=$2, link=$3, active=$4, sort=$5, start_at=$6, end_at=$7 WHERE id=$8",
        body.text, body.icon, body.link, body.active, body.sort, body.start_at, body.end_at, deal_id
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Ticker deal not found")
    return {"ok": True}


@router.delete("/homepage/ticker/{deal_id}")
async def delete_ticker(deal_id: str, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    await db.execute("DELETE FROM homepage_deals WHERE id=$1", deal_id)
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
    bid = new_id()
    await db.execute(
        "INSERT INTO banners (id, title, subtitle, image, link, active, sort, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
        bid, body.title, body.subtitle, body.image, body.link, body.active, body.sort, iso_now()
    )
    banner = await db.fetch_one("SELECT * FROM banners WHERE id=$1", bid)
    return {"banner": dict(banner)}


@router.put("/homepage/banners/{banner_id}")
async def update_banner(banner_id: str, body: BannerIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    result = await db.execute(
        "UPDATE banners SET title=$1, subtitle=$2, image=$3, link=$4, active=$5, sort=$6 WHERE id=$7",
        body.title, body.subtitle, body.image, body.link, body.active, body.sort, banner_id
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Banner not found")
    return {"ok": True}


@router.delete("/homepage/banners/{banner_id}")
async def delete_banner(banner_id: str, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    await db.execute("DELETE FROM banners WHERE id=$1", banner_id)
    return {"ok": True}


class SectionsIn(BaseModel):
    sections: list[dict]


@router.put("/homepage/sections")
async def update_sections(body: SectionsIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    await db.execute(
        "INSERT INTO homepage (id, sections) VALUES ('homepage',$1) ON CONFLICT (id) DO UPDATE SET sections=$1",
        json.dumps(body.sections)
    )
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
    items = await db.fetch_all("SELECT * FROM spin_rewards LIMIT 50")
    spins = await db.fetch_all("SELECT * FROM spin_transactions ORDER BY created_at DESC LIMIT 50")
    return {"items": [dict(i) for i in items], "recent_spins": [dict(s) for s in spins]}


@router.post("/spin/rewards")
async def create_spin_reward(body: SpinRewardIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    if body.type not in ("coupon_percent", "coupon_flat", "points", "free_delivery", "none"):
        raise HTTPException(400, "Invalid reward type")
    rid = new_id()
    await db.execute(
        "INSERT INTO spin_rewards (id, label, type, value, probability, expiry_days, active, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
        rid, body.label, body.type, body.value, body.probability, body.expiry_days, body.active, iso_now()
    )
    await audit(admin, "create", "spin_reward", rid, new=body.model_dump(), request=request)
    reward = await db.fetch_one("SELECT * FROM spin_rewards WHERE id=$1", rid)
    return {"reward": dict(reward)}


@router.put("/spin/rewards/{reward_id}")
async def update_spin_reward(reward_id: str, body: SpinRewardIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    result = await db.execute(
        "UPDATE spin_rewards SET label=$1, type=$2, value=$3, probability=$4, expiry_days=$5, active=$6 WHERE id=$7",
        body.label, body.type, body.value, body.probability, body.expiry_days, body.active, reward_id
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Reward not found")
    return {"ok": True}


@router.delete("/spin/rewards/{reward_id}")
async def delete_spin_reward(reward_id: str, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    await db.execute("DELETE FROM spin_rewards WHERE id=$1", reward_id)
    return {"ok": True}


# ---------- Customers ----------

@router.get("/customers")
async def admin_customers(request: Request, q: str = "", page: int = 1, limit: int = 30):
    await require_admin(request)
    if q:
        total = await db.fetch_val(
            "SELECT COUNT(*) FROM users WHERE name ILIKE $1 OR phone ILIKE $1 OR email ILIKE $1",
            f"%{q}%"
        )
        users = await db.fetch_all(
            "SELECT * FROM users WHERE name ILIKE $1 OR phone ILIKE $1 OR email ILIKE $1 ORDER BY created_at DESC OFFSET $2 LIMIT $3",
            f"%{q}%", (page - 1) * limit, limit
        )
    else:
        total = await db.fetch_val("SELECT COUNT(*) FROM users")
        users = await db.fetch_all(
            "SELECT * FROM users ORDER BY created_at DESC OFFSET $1 LIMIT $2",
            (page - 1) * limit, limit
        )

    out = []
    for u in users:
        orders = await db.fetch_all(
            "SELECT total, created_at FROM orders WHERE user_id=$1 AND payment_status='paid'", u["id"]
        )
        wallet = await db.fetch_one("SELECT balance FROM reward_accounts WHERE user_id=$1", u["id"])
        out.append({
            "id": u["id"], "name": u.get("name", ""), "phone": u.get("phone", ""),
            "email": u.get("email", ""), "created_at": u.get("created_at"),
            "disabled": u.get("disabled", False), "total_orders": len(orders),
            "total_spent": sum(o["total"] for o in orders if o.get("total")),
            "last_order": max((o["created_at"] for o in orders), default=None),
            "points": wallet["balance"] if wallet else 0,
        })
    return {"items": out, "total": total or 0}


class CustomerStatusBody(BaseModel):
    disabled: bool


@router.put("/customers/{user_id}/status")
async def set_customer_status(user_id: str, body: CustomerStatusBody, request: Request):
    admin = await require_admin(request, ["customer_support"])
    result = await db.execute("UPDATE users SET disabled=$1 WHERE id=$2", body.disabled, user_id)
    if result == "UPDATE 0":
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
    items = await db.fetch_all("SELECT * FROM reviews ORDER BY created_at DESC LIMIT 200")
    return {"items": [dict(r) for r in items]}


class ReviewModBody(BaseModel):
    approved: bool


@router.delete("/reviews/{review_id}")
async def admin_delete_review(review_id: str, request: Request):
    admin = await require_admin(request, ["customer_support"])
    review = await db.fetch_one("SELECT product_id FROM reviews WHERE id=$1", review_id)
    if not review:
        raise HTTPException(404, "Review not found")
    await db.execute("DELETE FROM reviews WHERE id=$1", review_id)
    await recompute_product_rating(review["product_id"])
    await audit(admin, "delete_review", "review", review_id, request=request)
    return {"ok": True}


@router.put("/reviews/{review_id}")
async def moderate_review(review_id: str, body: ReviewModBody, request: Request):
    admin = await require_admin(request, ["customer_support"])
    result = await db.execute("UPDATE reviews SET approved=$1 WHERE id=$2", body.approved, review_id)
    if result == "UPDATE 0":
        raise HTTPException(404, "Review not found")
    await audit(admin, "moderate_review", "review", review_id, new={"approved": body.approved}, request=request)
    return {"ok": True}


# ---------- Returns ----------

@router.get("/returns")
async def admin_returns(request: Request):
    await require_admin(request, ORDER_ROLES)
    items = await db.fetch_all("SELECT * FROM returns ORDER BY created_at DESC LIMIT 200")
    return {"items": [_order_dict(r) for r in items]}


class ReturnActionBody(BaseModel):
    action: str


@router.put("/returns/{return_id}")
async def action_return(return_id: str, body: ReturnActionBody, request: Request):
    admin = await require_admin(request, ORDER_ROLES)
    ret = await db.fetch_one("SELECT * FROM returns WHERE id=$1", return_id)
    if not ret:
        raise HTTPException(404, "Return not found")
    ret = _order_dict(ret)
    if body.action not in ("approved", "rejected", "refunded"):
        raise HTTPException(400, "Invalid action")
    await db.execute("UPDATE returns SET status=$1, updated_at=$2 WHERE id=$3",
                     body.action, iso_now(), return_id)
    if body.action == "refunded":
        order = await db.fetch_one("SELECT * FROM orders WHERE id=$1", ret["order_id"])
        if order:
            order = _order_dict(order)
            timeline = list(order.get("timeline") or [])
            timeline.append({"status": "refunded", "at": iso_now(), "note": f"Return {return_id} refunded"})
            await db.execute(
                "UPDATE orders SET status='refunded', payment_status='refunded', updated_at=$1, timeline=$2 WHERE id=$3",
                iso_now(), json.dumps(timeline), ret["order_id"]
            )
            for it in _parse_jsonb(order.get("items"), []):
                await db.execute("SELECT increment_variant_stock($1,$2,$3)",
                                 it["product_id"], it["variant_id"], it["qty"])
        await notify(ret["user_id"], "refund", "Refund processed",
                     f"Refund for order {ret['order_id']} has been processed.",
                     {"order_id": ret["order_id"]})
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
    items = await db.fetch_all("SELECT * FROM delivery_partners LIMIT 100")
    return {"items": [dict(p) for p in items]}


@router.post("/delivery/partners")
async def create_partner(body: PartnerIn, request: Request):
    admin = await require_admin(request, ORDER_ROLES)
    pid = new_id()
    await db.execute(
        "INSERT INTO delivery_partners (id, name, phone, zone, active, created_at) VALUES ($1,$2,$3,$4,$5,$6)",
        pid, body.name.strip(), body.phone.strip(), body.zone, body.active, iso_now()
    )
    partner = await db.fetch_one("SELECT * FROM delivery_partners WHERE id=$1", pid)
    return {"partner": dict(partner)}


@router.delete("/delivery/partners/{partner_id}")
async def delete_partner(partner_id: str, request: Request):
    admin = await require_admin(request, ORDER_ROLES)
    await db.execute("DELETE FROM delivery_partners WHERE id=$1", partner_id)
    return {"ok": True}


# ---------- Synonyms ----------

class SynonymIn(BaseModel):
    keyword: str
    synonyms: list[str] = []


@router.get("/synonyms")
async def list_synonyms(request: Request):
    await require_admin(request, CATALOG_ROLES)
    items = await db.fetch_all("SELECT * FROM search_synonyms LIMIT 200")
    return {"items": [dict(s) for s in items]}


@router.post("/synonyms")
async def create_synonym(body: SynonymIn, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    kw = body.keyword.strip().lower()
    existing = await db.fetch_one("SELECT id FROM search_synonyms WHERE keyword=$1", kw)
    if existing:
        raise HTTPException(400, "Synonym group already exists")
    sid = new_id()
    syns = [s.strip().lower() for s in body.synonyms if s.strip()]
    await db.execute(
        "INSERT INTO search_synonyms (id, keyword, synonyms) VALUES ($1,$2,$3)",
        sid, kw, syns
    )
    await audit(admin, "create", "synonym", kw, request=request)
    s = await db.fetch_one("SELECT * FROM search_synonyms WHERE id=$1", sid)
    return {"synonym": dict(s)}


@router.delete("/synonyms/{syn_id}")
async def delete_synonym(syn_id: str, request: Request):
    admin = await require_admin(request, CATALOG_ROLES)
    await db.execute("DELETE FROM search_synonyms WHERE id=$1", syn_id)
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
    if not upd:
        return {"settings": await get_settings()}
    set_clauses, args = [], []
    for k, v in upd.items():
        args.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
        set_clauses.append(f"{k}=${len(args)}")
    args.append(iso_now())
    await db.execute(
        f"UPDATE settings SET {', '.join(set_clauses)} WHERE id='global'",
        *args
    )
    await audit(admin, "update", "settings", "global", new=upd, request=request)
    return {"settings": await get_settings()}


# ---------- File uploads ----------

ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
ALLOWED_VIDEO_EXT = {"mp4", "webm", "mov", "m4v"}


@router.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    await require_admin(request)
    ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""
    if ext in ALLOWED_IMAGE_EXT:
        kind, max_size = "image", 8 * 1024 * 1024
    elif ext in ALLOWED_VIDEO_EXT:
        kind, max_size = "video", 80 * 1024 * 1024
    else:
        raise HTTPException(400, "Unsupported file type. Use images (jpg/png/webp) or videos (mp4/webm).")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if kind == "image":
        data, ctype, ext = compress_image(data)
    elif len(data) > max_size:
        raise HTTPException(400, f"File too large (max {max_size // (1024 * 1024)}MB for {kind}s)")
    else:
        ctype = file.content_type or "application/octet-stream"
    path = f"stylenow/{kind}s/{new_id()}.{ext}"
    result = put_object(path, data, ctype)
    await db.execute(
        """INSERT INTO files (id, storage_path, original_filename, content_type, size, kind, public, is_deleted, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,true,false,$7)""",
        new_id(), result["path"], file.filename, ctype, result.get("size", len(data)), kind, iso_now()
    )
    return {"path": result["path"], "url": f"/api/files/{result['path']}", "kind": kind}


# ---------- Media library ----------

@router.get("/files")
async def admin_files(request: Request, kind: str = ""):
    await require_admin(request)
    if kind in ("image", "video"):
        items = await db.fetch_all(
            "SELECT * FROM files WHERE is_deleted=false AND kind=$1 ORDER BY created_at DESC LIMIT 500", kind
        )
    else:
        items = await db.fetch_all(
            "SELECT * FROM files WHERE is_deleted=false ORDER BY created_at DESC LIMIT 500"
        )
    result = []
    for f in items:
        d = dict(f)
        d["url"] = f"/api/files/{d['storage_path']}"
        result.append(d)
    return {"items": result}


@router.delete("/files/{file_id}")
async def delete_file(file_id: str, request: Request):
    admin = await require_admin(request)
    result = await db.execute("UPDATE files SET is_deleted=true WHERE id=$1", file_id)
    if result == "UPDATE 0":
        raise HTTPException(404, "File not found")
    await audit(admin, "delete", "file", file_id, request=request)
    return {"ok": True}


# ---------- Videos ----------

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
    items = await db.fetch_all("SELECT * FROM videos ORDER BY sort ASC LIMIT 200")
    result = []
    for v in items:
        d = dict(v)
        p = await db.fetch_one("SELECT name FROM products WHERE id=$1", d.get("product_id", ""))
        d["product_name"] = p["name"] if p else None
        result.append(d)
    return {"items": result}


@router.post("/videos")
async def create_video(body: VideoIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    if body.kind not in ("review", "ad"):
        raise HTTPException(400, "Kind must be review or ad")
    if body.product_id:
        exists = await db.fetch_one("SELECT id FROM products WHERE id=$1", body.product_id)
        if not exists:
            raise HTTPException(400, "Attached product not found")
    vid = new_id()
    await db.execute(
        """INSERT INTO videos (id, username, caption, product_id, video, poster, kind, active, sort, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
        vid, body.username, body.caption, body.product_id, body.video,
        body.poster, body.kind, body.active, body.sort, iso_now()
    )
    await audit(admin, "create", "video", vid, new={"username": body.username, "kind": body.kind}, request=request)
    video = await db.fetch_one("SELECT * FROM videos WHERE id=$1", vid)
    return {"video": dict(video)}


@router.put("/videos/{video_id}")
async def update_video(video_id: str, body: VideoIn, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    result = await db.execute(
        "UPDATE videos SET username=$1, caption=$2, product_id=$3, video=$4, poster=$5, kind=$6, active=$7, sort=$8 WHERE id=$9",
        body.username, body.caption, body.product_id, body.video,
        body.poster, body.kind, body.active, body.sort, video_id
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Video not found")
    await audit(admin, "update", "video", video_id, request=request)
    return {"ok": True}


@router.delete("/videos/{video_id}")
async def delete_video(video_id: str, request: Request):
    admin = await require_admin(request, MARKETING_ROLES)
    v = await db.fetch_one("SELECT video FROM videos WHERE id=$1", video_id)
    if v and v["video"]:
        await db.execute("UPDATE files SET is_deleted=true WHERE storage_path=$1", v["video"])
    await db.execute("DELETE FROM videos WHERE id=$1", video_id)
    await audit(admin, "delete", "video", video_id, request=request)
    return {"ok": True}


# ---------- Analytics & Audit ----------

@router.get("/analytics/search")
async def search_analytics(request: Request):
    await require_admin(request)
    popular = await db.fetch_all(
        """SELECT query, COUNT(*) as searches, AVG(results) as avg_results,
           COUNT(clicked_product) as clicks
           FROM search_logs GROUP BY query ORDER BY searches DESC LIMIT 20"""
    )
    zero = await db.fetch_all(
        """SELECT query, COUNT(*) as searches FROM search_logs
           WHERE results=0 GROUP BY query ORDER BY searches DESC LIMIT 20"""
    )
    return {
        "popular": [{"_id": r["query"], "searches": int(r["searches"]),
                     "avg_results": float(r["avg_results"] or 0),
                     "clicks": int(r["clicks"])} for r in popular],
        "zero_results": [{"_id": r["query"], "searches": int(r["searches"])} for r in zero],
    }


@router.get("/audit-logs")
async def audit_logs(request: Request, page: int = 1, limit: int = 50):
    await require_admin(request)
    total = await db.fetch_val("SELECT COUNT(*) FROM audit_logs")
    items = await db.fetch_all(
        "SELECT * FROM audit_logs ORDER BY created_at DESC OFFSET $1 LIMIT $2",
        (page - 1) * limit, limit
    )
    result = []
    for i in items:
        d = dict(i)
        d["previous"] = _j(d.get("previous"))
        d["new"] = _j(d.get("new"))
        result.append(d)
    return {"items": result, "total": total or 0}
