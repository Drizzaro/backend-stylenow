import os
import json
import hmac
import hashlib
from datetime import timedelta

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from core import (
    iso_now, utcnow, new_id, short_id, require_user, validate_coupon,
    get_settings, get_wallet, credit_points, notify, hub, cart_key, _parse_jsonb,
)
import db

router = APIRouter(tags=["orders"])

ORDER_STATUSES = ["placed", "confirmed", "preparing", "packed", "out_for_delivery", "delivered"]


def razorpay_client():
    kid = os.environ.get("RAZORPAY_KEY_ID", "")
    ks = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not kid or not ks:
        return None
    import razorpay
    return razorpay.Client(auth=(kid, ks))


async def build_quote(cart: dict, coupon_code: str, user_id: str,
                      redeem_points: bool = False, try_items: list = None) -> dict:
    items_raw = _parse_jsonb(cart.get("items"), [])
    items, subtotal = [], 0
    problems = []
    for it in items_raw:
        p = await db.fetch_one("SELECT * FROM products WHERE id=$1 AND active=true", it["product_id"])
        if not p:
            problems.append("A product in your cart is no longer available")
            continue
        variants = _parse_jsonb(p["variants"], [])
        v = next((x for x in variants if x["id"] == it["variant_id"]), None)
        if not v:
            problems.append(f"A variant of {p.get('name', 'a product')} is unavailable")
            continue
        if p.get("out_of_stock") or v.get("out_of_stock"):
            problems.append(f"{p.get('name', 'A product')} ({v.get('size', '')}) is out of stock")
        elif v.get("stock", 0) < it["qty"]:
            problems.append(f"Only {v.get('stock', 0)} left for {p.get('name')} ({v.get('size', '')})")
        line = v.get("price", 0) * it["qty"]
        subtotal += line
        p_images = _parse_jsonb(p.get("images"), [])
        items.append({
            "product_id": p["id"], "variant_id": v["id"], "qty": it["qty"],
            "name": p.get("name", ""), "sku": v.get("sku", ""),
            "image": (v.get("images") or p_images or [""])[0],
            "price": v.get("price", 0), "mrp": v.get("mrp", v.get("price", 0)),
            "color": v.get("color", ""), "size": v.get("size", ""), "line_total": line,
        })

    settings = await get_settings()
    delivery_fee = settings.get("delivery_fee", 0)
    try_list, try_fee, try_free, try_total = [], 0, False, 0
    threshold = settings.get("try_at_doorstep_threshold", 499)
    if try_items and settings.get("try_at_doorstep_enabled", True):
        wanted = []
        for t in try_items:
            if not isinstance(t, dict):
                continue
            pid, vid = t.get("product_id"), t.get("variant_id")
            if any(i["product_id"] == pid and i["variant_id"] == vid for i in items):
                wanted.append(vid)
            if len(wanted) >= 2:
                break
        try_list = [dict(i) for i in items if i["variant_id"] in wanted]
        try_total = sum(i["line_total"] for i in try_list)

    billed_subtotal = subtotal - try_total
    discount, coupon_info = await validate_coupon(coupon_code, billed_subtotal, user_id)
    if coupon_info and coupon_info.get("error"):
        discount, coupon_info = 0, None
    if coupon_info and coupon_info.get("type") == "free_delivery":
        delivery_fee = 0

    points_used = 0
    points_discount = 0
    wallet_balance = 0
    rate = settings.get("points_value_rupee", 1) or 1
    if user_id:
        wallet = await get_wallet(user_id)
        wallet_balance = wallet.get("balance", 0)
        if redeem_points and wallet_balance > 0:
            max_redeem_value = int((billed_subtotal - discount) * 0.10)
            points_used = min(int(wallet_balance), int(max_redeem_value / rate))
            points_discount = points_used * rate

    if try_list:
        try_free = (billed_subtotal - discount) >= threshold
        try_fee = 0 if try_free else settings.get("try_at_doorstep_fee", 50)

    total = max(0, billed_subtotal - discount - points_discount) + delivery_fee + try_fee
    return {
        "items": items, "subtotal": billed_subtotal, "discount": discount,
        "coupon": coupon_info, "delivery_fee": delivery_fee, "total": total,
        "points_used": points_used, "points_discount": points_discount,
        "points_balance": wallet_balance, "points_rate": rate,
        "try_items": try_list, "try_fee": try_fee, "try_free": try_free, "try_total": try_total,
        "try_threshold": threshold, "try_enabled": settings.get("try_at_doorstep_enabled", True),
        "problems": problems,
        "eta": f"{settings.get('delivery_eta_min', 30)}–{settings.get('delivery_eta_max', 60)} min",
    }


class QuoteBody(BaseModel):
    redeem_points: bool = False
    try_items: list = []


@router.post("/checkout/quote")
async def checkout_quote(body: QuoteBody, request: Request):
    user = await require_user(request)
    cart = await db.fetch_one("SELECT * FROM carts WHERE key=$1", cart_key(request, user))
    cart = dict(cart) if cart else {"items": []}
    if not _parse_jsonb(cart.get("items"), []):
        raise HTTPException(400, "Your cart is empty")
    return await build_quote(cart, cart.get("coupon_code") or "", user["id"], body.redeem_points, body.try_items)


class CreateOrderBody(BaseModel):
    address_id: str
    coupon_code: str = ""
    note: str = ""
    payment_method: str = "online"
    redeem_points: bool = False
    try_items: list = []


@router.post("/orders")
async def create_order(body: CreateOrderBody, request: Request):
    user = await require_user(request)
    if body.payment_method not in ("online", "cod"):
        raise HTTPException(400, "Invalid payment method")
    cart = await db.fetch_one("SELECT * FROM carts WHERE key=$1", cart_key(request, user))
    cart = dict(cart) if cart else {"items": []}
    if not _parse_jsonb(cart.get("items"), []):
        raise HTTPException(400, "Your cart is empty")
    quote = await build_quote(cart, body.coupon_code or cart.get("coupon_code") or "",
                              user["id"], body.redeem_points, body.try_items)
    if not quote["items"]:
        raise HTTPException(400, "Your cart is empty")
    if quote["problems"]:
        raise HTTPException(409, " ".join(quote["problems"]))
    address = await db.fetch_one(
        "SELECT * FROM addresses WHERE id=$1 AND user_id=$2", body.address_id, user["id"]
    )
    if not address:
        raise HTTPException(400, "Please select a valid delivery address")

    # Atomic stock deduction via PostgreSQL function
    deducted = []
    for it in quote["items"]:
        result = await db.fetch_val(
            "SELECT decrement_variant_stock($1,$2,$3)",
            it["product_id"], it["variant_id"], it["qty"]
        )
        if result is None:
            # Rollback already-deducted stock
            for pid, vid, q in deducted:
                await db.execute("SELECT increment_variant_stock($1,$2,$3)", pid, vid, q)
            raise HTTPException(409, f"Insufficient stock for {it['name']}. Please review your cart.")
        deducted.append((it["product_id"], it["variant_id"], it["qty"]))
        await db.execute(
            "INSERT INTO inventory_transactions (id, product_id, variant_id, change, reason, ref, created_at) VALUES ($1,$2,$3,$4,'order','',$5)",
            new_id(), it["product_id"], it["variant_id"], -it["qty"], iso_now()
        )

    settings = await get_settings()
    order_id = "SN" + short_id()
    address_dict = dict(address)
    timeline = json.dumps([{"status": "placed", "at": iso_now(), "note": "Order placed"}])
    try_doorstep = json.dumps({
        "enabled": bool(quote["try_items"]),
        "fee": quote["try_fee"],
        "items": [{**t, "outcome": None} for t in quote["try_items"]],
        "total": quote.get("try_total", 0),
    })

    await db.execute(
        """INSERT INTO orders (
            id, user_id, customer, items, subtotal, discount, coupon_code, delivery_fee, total,
            address, note, points_redeemed, points_discount, try_at_doorstep,
            status, payment_status, payment_method, payment_id, razorpay_order_id,
            rider, internal_notes, reward_points_awarded, eta, timeline, created_at, updated_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,null,null,null,'[]',0,$18,$19,$20,$20)""",
        order_id, user["id"],
        json.dumps({"name": address_dict.get("name") or user.get("name") or "Customer", "phone": user.get("phone", "")}),
        json.dumps(quote["items"]),
        quote["subtotal"], quote["discount"],
        (quote["coupon"] or {}).get("code"),
        quote["delivery_fee"], quote["total"],
        json.dumps(address_dict),
        body.note, quote["points_used"], quote["points_discount"],
        try_doorstep,
        "placed",
        "cod" if body.payment_method == "cod" else "pending",
        body.payment_method,
        f"{settings.get('delivery_eta_min', 30)}–{settings.get('delivery_eta_max', 60)} min",
        timeline, iso_now()
    )

    if (quote["coupon"] or {}).get("code"):
        await db.execute(
            "UPDATE coupons SET used_count=used_count+1 WHERE code=$1",
            quote["coupon"]["code"]
        )
    if quote["points_used"] > 0:
        await credit_points(user["id"], -quote["points_used"], "redeem",
                            f"Redeemed on order {order_id}", order_id)

    await db.execute(
        "UPDATE carts SET items='[]', coupon_code=null, updated_at=$1 WHERE key=$2",
        iso_now(), cart_key(request, user)
    )

    order = await db.fetch_one("SELECT * FROM orders WHERE id=$1", order_id)
    order = _order_to_dict(order)

    if body.payment_method == "cod":
        for it in quote["items"]:
            await db.execute("UPDATE products SET order_count=order_count+$1 WHERE id=$2",
                             it["qty"], it["product_id"])
        await notify(user["id"], "order", "Order placed",
                     f"Order {order_id} placed with Cash on Delivery. Estimated delivery: {order['eta']}.",
                     {"order_id": order_id})
        hub.publish("new_order", {
            "order_id": order_id, "customer": order["customer"].get("name", ""),
            "items": len(quote["items"]), "total": order["total"], "payment_status": "cod",
        })
        return {"order": order, "payment": {"mode": "cod"}}

    rzp = razorpay_client()
    payment = {"mode": "simulated"}
    if rzp:
        try:
            ro = rzp.order.create({"amount": int(order["total"] * 100), "currency": "INR",
                                   "receipt": order_id[:40], "payment_capture": 1})
            await db.execute("UPDATE orders SET razorpay_order_id=$1 WHERE id=$2", ro["id"], order_id)
            payment = {"mode": "razorpay", "razorpay_order_id": ro["id"],
                       "key_id": os.environ["RAZORPAY_KEY_ID"], "amount": ro["amount"], "currency": "INR"}
        except Exception as e:
            payment = {"mode": "simulated", "warning": f"Razorpay unavailable: {e}"}
    return {"order": order, "payment": payment}


def _order_to_dict(order) -> dict:
    """Convert asyncpg Record with JSONB fields to plain dict."""
    if order is None:
        return {}
    d = dict(order)
    for field in ("customer", "items", "address", "try_at_doorstep", "timeline",
                  "internal_notes", "rider", "refund_details"):
        v = d.get(field)
        if isinstance(v, str):
            try:
                d[field] = json.loads(v)
            except Exception:
                pass
    return d


class VerifyPaymentBody(BaseModel):
    razorpay_payment_id: str = ""
    razorpay_signature: str = ""
    simulated: bool = False


async def mark_paid(order: dict, payment_id: str):
    await db.execute(
        "UPDATE orders SET payment_status='paid', payment_id=$1, updated_at=$2 WHERE id=$3",
        payment_id, iso_now(), order["id"]
    )
    for it in _parse_jsonb(order.get("items"), []):
        await db.execute("UPDATE products SET order_count=order_count+$1 WHERE id=$2",
                         it["qty"], it["product_id"])
    await notify(order["user_id"], "order", "Payment successful",
                 f"Payment received for order {order['id']}. Your fashion is on its way!", {"order_id": order["id"]})
    await notify(order["user_id"], "order", "Order placed",
                 f"Order {order['id']} placed successfully. Estimated delivery: {order.get('eta', '30–60 min')}.",
                 {"order_id": order["id"]})
    hub.publish("new_order", {
        "order_id": order["id"], "customer": _parse_jsonb(order.get("customer"), {}).get("name", ""),
        "items": len(_parse_jsonb(order.get("items"), [])), "total": order["total"], "payment_status": "paid",
    })


@router.post("/orders/{order_id}/verify-payment")
async def verify_payment(order_id: str, body: VerifyPaymentBody, request: Request):
    user = await require_user(request)
    order = await db.fetch_one("SELECT * FROM orders WHERE id=$1 AND user_id=$2", order_id, user["id"])
    if not order:
        raise HTTPException(404, "Order not found")
    order = _order_to_dict(order)
    if order.get("payment_status") == "paid":
        return {"ok": True, "payment_status": "paid"}
    rzp = razorpay_client()
    if body.simulated:
        if rzp:
            raise HTTPException(400, "Simulated payments are disabled while Razorpay is configured")
        await mark_paid(order, "SIMULATED-" + short_id())
        return {"ok": True, "payment_status": "paid", "simulated": True}
    if not rzp:
        raise HTTPException(400, "Razorpay is not configured")
    generated = hmac.new(
        os.environ["RAZORPAY_KEY_SECRET"].encode(),
        f"{order.get('razorpay_order_id')}|{body.razorpay_payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(generated, body.razorpay_signature or ""):
        raise HTTPException(400, "Payment verification failed")
    await mark_paid(order, body.razorpay_payment_id)
    return {"ok": True, "payment_status": "paid"}


@router.post("/payments/webhook")
async def razorpay_webhook(request: Request):
    payload = await request.body()
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
    if secret:
        sig = request.headers.get("X-Razorpay-Signature", "")
        generated = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(generated, sig):
            raise HTTPException(400, "Invalid webhook signature")
    event = json.loads(payload.decode() or "{}")
    if event.get("event") == "payment.captured":
        pid = event.get("payload", {}).get("payment", {}).get("entity", {})
        ro_id = pid.get("order_id")
        order = await db.fetch_one("SELECT * FROM orders WHERE razorpay_order_id=$1", ro_id)
        if order and order.get("payment_status") != "paid":
            await mark_paid(_order_to_dict(order), pid.get("id", ""))
    return {"status": "processed"}


@router.get("/orders")
async def my_orders(request: Request):
    user = await require_user(request)
    orders = await db.fetch_all(
        "SELECT * FROM orders WHERE user_id=$1 ORDER BY created_at DESC LIMIT 100", user["id"]
    )
    return {"items": [_order_to_dict(o) for o in orders]}


@router.get("/orders/{order_id}")
async def order_detail(order_id: str, request: Request):
    user = await require_user(request)
    order = await db.fetch_one("SELECT * FROM orders WHERE id=$1 AND user_id=$2", order_id, user["id"])
    if not order:
        raise HTTPException(404, "Order not found")
    return {"order": _order_to_dict(order)}


class CancelBody(BaseModel):
    refund_method: str = ""
    refund_details: dict = {}


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, body: CancelBody, request: Request):
    user = await require_user(request)
    order = await db.fetch_one("SELECT * FROM orders WHERE id=$1 AND user_id=$2", order_id, user["id"])
    if not order:
        raise HTTPException(404, "Order not found")
    order = _order_to_dict(order)
    if order["status"] not in ("placed", "confirmed"):
        raise HTTPException(400, "This order can no longer be cancelled")
    needs_refund = order.get("payment_method") == "online" and order.get("payment_status") == "paid"
    if needs_refund:
        if body.refund_method not in ("upi", "bank"):
            raise HTTPException(400, "Online payments are refunded to a UPI ID or bank account. Please choose one.")
        d = body.refund_details or {}
        if body.refund_method == "upi" and not (d.get("upi_id") or "").strip():
            raise HTTPException(400, "Please provide your UPI ID for the refund")
        if body.refund_method == "bank":
            for f in ("account_holder", "account_number", "ifsc"):
                if not (d.get(f) or "").strip():
                    raise HTTPException(400, "Please provide account holder name, account number and IFSC")

    for it in _parse_jsonb(order.get("items"), []):
        await db.execute("SELECT increment_variant_stock($1,$2,$3)",
                         it["product_id"], it["variant_id"], it["qty"])

    if order.get("points_redeemed"):
        await credit_points(user["id"], order["points_redeemed"], "refund",
                            f"Order {order_id} cancelled", order_id)

    timeline = _parse_jsonb(order.get("timeline"), [])
    timeline.append({"status": "cancelled", "at": iso_now(), "note": "Cancelled by customer"})

    upd_fields = {
        "status": "cancelled",
        "updated_at": iso_now(),
        "timeline": json.dumps(timeline),
    }
    if needs_refund:
        d = body.refund_details or {}
        upd_fields["refund_method"] = body.refund_method
        upd_fields["refund_details"] = json.dumps({k: str(v).strip() for k, v in d.items()
                                                    if k in ("upi_id", "account_holder", "account_number", "ifsc")})
        upd_fields["refund_status"] = "pending"

    await db.execute(
        """UPDATE orders SET status=$1, updated_at=$2, timeline=$3,
           refund_method=$4, refund_details=$5, refund_status=$6 WHERE id=$7""",
        upd_fields["status"], upd_fields["updated_at"], upd_fields["timeline"],
        upd_fields.get("refund_method"), upd_fields.get("refund_details"),
        upd_fields.get("refund_status"), order_id
    )
    await notify(user["id"], "order", "Order cancelled",
                 f"Order {order_id} has been cancelled." +
                 (" Your refund will be processed to the given details." if needs_refund else ""),
                 {"order_id": order_id})
    return {"ok": True, "refund_pending": needs_refund}


class ReturnBody(BaseModel):
    reason: str
    refund_method: str = "cash"
    refund_details: dict = {}


@router.post("/orders/{order_id}/return")
async def request_return(order_id: str, body: ReturnBody, request: Request):
    user = await require_user(request)
    order = await db.fetch_one("SELECT * FROM orders WHERE id=$1 AND user_id=$2", order_id, user["id"])
    if not order:
        raise HTTPException(404, "Order not found")
    order = _order_to_dict(order)
    if order["status"] != "delivered":
        raise HTTPException(400, "Returns are only available for delivered orders")
    if body.refund_method not in ("cash", "upi", "bank"):
        raise HTTPException(400, "Invalid refund method")
    day_ago = (utcnow() - timedelta(hours=24)).isoformat()
    recent = await db.fetch_val(
        "SELECT COUNT(*) FROM returns WHERE user_id=$1 AND created_at >= $2",
        user["id"], day_ago
    )
    if (recent or 0) >= 3:
        raise HTTPException(429, "Return request limit reached for today. Please try again tomorrow.")
    d = body.refund_details or {}
    if body.refund_method == "upi" and not (d.get("upi_id") or "").strip():
        raise HTTPException(400, "Please provide your UPI ID for the refund")
    if body.refund_method == "bank":
        for f in ("account_holder", "account_number", "ifsc"):
            if not (d.get(f) or "").strip():
                raise HTTPException(400, "Please provide account holder name, account number and IFSC")
    existing = await db.fetch_one(
        "SELECT id FROM returns WHERE order_id=$1 AND status != 'rejected'", order_id
    )
    if existing:
        raise HTTPException(400, "A return request already exists for this order")

    rid = "RET" + short_id()
    clean_details = {k: str(v).strip() for k, v in d.items()
                     if k in ("upi_id", "account_holder", "account_number", "ifsc")}
    await db.execute(
        """INSERT INTO returns (id, order_id, user_id, reason, items, amount, refund_method, refund_details, status, created_at, updated_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'requested',$9,$9)""",
        rid, order_id, user["id"], body.reason.strip(),
        json.dumps(order.get("items", [])), order.get("total", 0),
        body.refund_method, json.dumps(clean_details), iso_now()
    )
    doc = await db.fetch_one("SELECT * FROM returns WHERE id=$1", rid)
    return {"return": _order_to_dict(doc)}
