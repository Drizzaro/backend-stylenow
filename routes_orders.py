import os
import hmac
import hashlib
from datetime import timedelta

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from core import (
    db, iso_now, utcnow, new_id, short_id, require_user, validate_coupon,
    get_settings, get_wallet, credit_points, notify, hub, cart_key,
)

router = APIRouter(tags=["orders"])

ORDER_STATUSES = ["placed", "confirmed", "preparing", "packed", "out_for_delivery", "delivered"]


def razorpay_client():
    kid = os.environ.get("RAZORPAY_KEY_ID", "")
    ks = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not kid or not ks:
        return None
    import razorpay
    return razorpay.Client(auth=(kid, ks))


async def build_quote(cart: dict, coupon_code: str, user_id: str, redeem_points: bool = False, try_items: list = None) -> dict:
    items, subtotal = [], 0
    problems = []
    for it in cart.get("items", []):
        p = await db.products.find_one({"id": it["product_id"], "active": True}, {"_id": 0})
        if not p:
            problems.append("A product in your cart is no longer available")
            continue
        v = next((x for x in p.get("variants", []) if x["id"] == it["variant_id"]), None)
        if not v:
            problems.append(f"A variant of {p.get('name', 'a product')} is unavailable")
            continue
        if p.get("out_of_stock") or v.get("out_of_stock"):
            problems.append(f"{p.get('name', 'A product')} ({v.get('size', '')}) is out of stock")
        elif v.get("stock", 0) < it["qty"]:
            problems.append(f"Only {v.get('stock', 0)} left for {p.get('name')} ({v.get('size', '')})")
        line = v.get("price", 0) * it["qty"]
        subtotal += line
        items.append({
            "product_id": p["id"], "variant_id": v["id"], "qty": it["qty"],
            "name": p.get("name", ""), "sku": v.get("sku", ""),
            "image": (v.get("images") or p.get("images") or [""])[0],
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
    cart = await db.carts.find_one({"key": cart_key(request, user)}, {"_id": 0}) or {"items": []}
    if not cart.get("items"):
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
    cart = await db.carts.find_one({"key": cart_key(request, user)}, {"_id": 0}) or {"items": []}
    if not cart.get("items"):
        raise HTTPException(400, "Your cart is empty")
    quote = await build_quote(cart, body.coupon_code or cart.get("coupon_code") or "", user["id"], body.redeem_points, body.try_items)
    if not quote["items"]:
        raise HTTPException(400, "Your cart is empty")
    if quote["problems"]:
        raise HTTPException(409, " ".join(quote["problems"]))
    address = await db.addresses.find_one({"id": body.address_id, "user_id": user["id"]}, {"_id": 0})
    if not address:
        raise HTTPException(400, "Please select a valid delivery address")

    deducted = []
    for it in quote["items"]:
        res = await db.products.find_one_and_update(
            {"id": it["product_id"], "variants.id": it["variant_id"], "variants.stock": {"$gte": it["qty"]}},
            {"$inc": {"variants.$.stock": -it["qty"]}},
        )
        if not res:
            for pid, vid, q in deducted:
                await db.products.update_one({"id": pid, "variants.id": vid}, {"$inc": {"variants.$.stock": q}})
            raise HTTPException(409, f"Insufficient stock for {it['name']}. Please review your cart.")
        deducted.append((it["product_id"], it["variant_id"], it["qty"]))
        await db.inventory_transactions.insert_one({
            "id": new_id(), "product_id": it["product_id"], "variant_id": it["variant_id"],
            "change": -it["qty"], "reason": "order", "ref": "", "created_at": iso_now(),
        })

    settings = await get_settings()
    order_id = "SN" + short_id()
    order = {
        "id": order_id, "user_id": user["id"],
        "customer": {"name": address.get("name") or user.get("name") or "Customer", "phone": user.get("phone", "")},
        "items": quote["items"], "subtotal": quote["subtotal"], "discount": quote["discount"],
        "coupon_code": (quote["coupon"] or {}).get("code"), "delivery_fee": quote["delivery_fee"],
        "total": quote["total"], "address": address, "note": body.note,
        "points_redeemed": quote["points_used"], "points_discount": quote["points_discount"],
        "try_at_doorstep": {"enabled": bool(quote["try_items"]), "fee": quote["try_fee"],
                            "items": [{**t, "outcome": None} for t in quote["try_items"]],
                            "total": quote.get("try_total", 0)},
        "status": "placed", "payment_status": "cod" if body.payment_method == "cod" else "pending",
        "payment_method": body.payment_method,
        "payment_id": None, "razorpay_order_id": None, "rider": None, "internal_notes": [],
        "reward_points_awarded": 0,
        "eta": f"{settings.get('delivery_eta_min', 30)}–{settings.get('delivery_eta_max', 60)} min",
        "timeline": [{"status": "placed", "at": iso_now(), "note": "Order placed"}],
        "created_at": iso_now(), "updated_at": iso_now(),
    }
    await db.orders.insert_one(order)
    if order["coupon_code"]:
        await db.coupons.update_one({"code": order["coupon_code"]}, {"$inc": {"used_count": 1}})
    if quote["points_used"] > 0:
        await credit_points(user["id"], -quote["points_used"], "redeem", f"Redeemed on order {order_id}", order_id)
    await db.carts.update_one({"key": cart_key(request, user)}, {"$set": {"items": [], "coupon_code": None, "updated_at": iso_now()}})

    if body.payment_method == "cod":
        for it in order["items"]:
            await db.products.update_one({"id": it["product_id"]}, {"$inc": {"order_count": it["qty"]}})
        await notify(user["id"], "order", "Order placed",
                     f"Order {order_id} placed with Cash on Delivery. Estimated delivery: {order['eta']}.",
                     {"order_id": order_id})
        hub.publish("new_order", {
            "order_id": order_id, "customer": order["customer"].get("name", ""),
            "items": len(order["items"]), "total": order["total"], "payment_status": "cod",
        })
        order.pop("_id", None)
        return {"order": order, "payment": {"mode": "cod"}}

    rzp = razorpay_client()
    payment = {"mode": "simulated"}
    if rzp:
        try:
            ro = rzp.order.create({"amount": int(order["total"] * 100), "currency": "INR",
                                   "receipt": order_id[:40], "payment_capture": 1})
            await db.orders.update_one({"id": order_id}, {"$set": {"razorpay_order_id": ro["id"]}})
            payment = {"mode": "razorpay", "razorpay_order_id": ro["id"],
                       "key_id": os.environ["RAZORPAY_KEY_ID"], "amount": ro["amount"], "currency": "INR"}
        except Exception as e:
            payment = {"mode": "simulated", "warning": f"Razorpay unavailable: {e}"}
    order.pop("_id", None)
    return {"order": order, "payment": payment}


class VerifyPaymentBody(BaseModel):
    razorpay_payment_id: str = ""
    razorpay_signature: str = ""
    simulated: bool = False


async def mark_paid(order: dict, payment_id: str):
    await db.orders.update_one({"id": order["id"]}, {"$set": {
        "payment_status": "paid", "payment_id": payment_id, "updated_at": iso_now()}})
    for it in order["items"]:
        await db.products.update_one({"id": it["product_id"]}, {"$inc": {"order_count": it["qty"]}})
    await notify(order["user_id"], "order", "Payment successful",
                 f"Payment received for order {order['id']}. Your fashion is on its way!", {"order_id": order["id"]})
    await notify(order["user_id"], "order", "Order placed",
                 f"Order {order['id']} placed successfully. Estimated delivery: {order.get('eta', '30–60 min')}.",
                 {"order_id": order["id"]})
    hub.publish("new_order", {
        "order_id": order["id"], "customer": order["customer"].get("name", ""),
        "items": len(order["items"]), "total": order["total"], "payment_status": "paid",
    })


@router.post("/orders/{order_id}/verify-payment")
async def verify_payment(order_id: str, body: VerifyPaymentBody, request: Request):
    user = await require_user(request)
    order = await db.orders.find_one({"id": order_id, "user_id": user["id"]}, {"_id": 0})
    if not order:
        raise HTTPException(404, "Order not found")
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
    import json
    event = json.loads(payload.decode() or "{}")
    if event.get("event") == "payment.captured":
        pid = event.get("payload", {}).get("payment", {}).get("entity", {})
        ro_id = pid.get("order_id")
        order = await db.orders.find_one({"razorpay_order_id": ro_id}, {"_id": 0})
        if order and order.get("payment_status") != "paid":
            await mark_paid(order, pid.get("id", ""))
    return {"status": "processed"}


@router.get("/orders")
async def my_orders(request: Request):
    user = await require_user(request)
    items = await db.orders.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(100)
    return {"items": items}


@router.get("/orders/{order_id}")
async def order_detail(order_id: str, request: Request):
    user = await require_user(request)
    order = await db.orders.find_one({"id": order_id, "user_id": user["id"]}, {"_id": 0})
    if not order:
        raise HTTPException(404, "Order not found")
    return {"order": order}


class CancelBody(BaseModel):
    refund_method: str = ""
    refund_details: dict = {}


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, body: CancelBody, request: Request):
    user = await require_user(request)
    order = await db.orders.find_one({"id": order_id, "user_id": user["id"]}, {"_id": 0})
    if not order:
        raise HTTPException(404, "Order not found")
    if order["status"] not in ("placed", "confirmed"):
        raise HTTPException(400, "This order can no longer be cancelled")
    needs_refund = order.get("payment_method") == "online" and order.get("payment_status") == "paid"
    set_fields = {"status": "cancelled", "updated_at": iso_now()}
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
        set_fields["refund_method"] = body.refund_method
        set_fields["refund_details"] = {k: str(v).strip() for k, v in d.items() if k in ("upi_id", "account_holder", "account_number", "ifsc")}
        set_fields["refund_status"] = "pending"
    for it in order["items"]:
        await db.products.update_one({"id": it["product_id"], "variants.id": it["variant_id"]},
                                     {"$inc": {"variants.$.stock": it["qty"]}})
    if order.get("points_redeemed"):
        await credit_points(user["id"], order["points_redeemed"], "refund", f"Order {order_id} cancelled", order_id)
    await db.orders.update_one({"id": order_id}, {"$set": set_fields,
        "$push": {"timeline": {"status": "cancelled", "at": iso_now(), "note": "Cancelled by customer"}}})
    await notify(user["id"], "order", "Order cancelled",
                 f"Order {order_id} has been cancelled." + (" Your refund will be processed to the given details." if needs_refund else ""),
                 {"order_id": order_id})
    return {"ok": True, "refund_pending": needs_refund}


class ReturnBody(BaseModel):
    reason: str
    refund_method: str = "cash"
    refund_details: dict = {}


@router.post("/orders/{order_id}/return")
async def request_return(order_id: str, body: ReturnBody, request: Request):
    user = await require_user(request)
    order = await db.orders.find_one({"id": order_id, "user_id": user["id"]}, {"_id": 0})
    if not order:
        raise HTTPException(404, "Order not found")
    if order["status"] != "delivered":
        raise HTTPException(400, "Returns are only available for delivered orders")
    if body.refund_method not in ("cash", "upi", "bank"):
        raise HTTPException(400, "Invalid refund method")
    day_ago = (utcnow() - timedelta(hours=24)).isoformat()
    recent = await db.returns.count_documents({"user_id": user["id"], "created_at": {"$gte": day_ago}})
    if recent >= 3:
        raise HTTPException(429, "Return request limit reached for today. Please try again tomorrow.")
    d = body.refund_details or {}
    if body.refund_method == "upi" and not (d.get("upi_id") or "").strip():
        raise HTTPException(400, "Please provide your UPI ID for the refund")
    if body.refund_method == "bank":
        for f in ("account_holder", "account_number", "ifsc"):
            if not (d.get(f) or "").strip():
                raise HTTPException(400, "Please provide account holder name, account number and IFSC")
    existing = await db.returns.find_one({"order_id": order_id, "status": {"$nin": ["rejected"]}})
    if existing:
        raise HTTPException(400, "A return request already exists for this order")
    doc = {"id": "RET" + short_id(), "order_id": order_id, "user_id": user["id"],
           "reason": body.reason.strip(), "items": order["items"], "amount": order["total"],
           "refund_method": body.refund_method,
           "refund_details": {k: str(v).strip() for k, v in d.items() if k in ("upi_id", "account_holder", "account_number", "ifsc")},
           "status": "requested", "created_at": iso_now(), "updated_at": iso_now()}
    await db.returns.insert_one(doc)
    doc.pop("_id", None)
    return {"return": doc}
