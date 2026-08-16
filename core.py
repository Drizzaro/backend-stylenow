import os
import uuid
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt
import bcrypt
from fastapi import HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger("stylenow")

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALG = "HS256"
USER_COOKIE = "sn_token"
ADMIN_COOKIE = "sn_admin"


def utcnow():
    return datetime.now(timezone.utc)


def iso_now():
    return utcnow().isoformat()


def new_id():
    return uuid.uuid4().hex


def short_id():
    return uuid.uuid4().hex[:8].upper()


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_token(sub: str, kind: str, days: int = 0, hours: int = 0) -> str:
    exp = utcnow() + (timedelta(days=days) if days else timedelta(hours=hours or 12))
    return jwt.encode({"sub": sub, "type": kind, "exp": exp}, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str, kind: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
        if payload.get("type") != kind:
            return None
        return payload
    except jwt.PyJWTError:
        return None


def public_user(u: dict) -> dict:
    return {
        "id": u["id"],
        "phone": u.get("phone", ""),
        "name": u.get("name", ""),
        "email": u.get("email", ""),
        "theme_preference": u.get("theme_preference", "system"),
        "created_at": u.get("created_at"),
    }


async def get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(USER_COOKIE)
    auth = request.headers.get("Authorization", "")
    if not token and auth.startswith("Bearer "):
        token = auth[7:]
    if not token:
        return None
    payload = decode_token(token, "user")
    if not payload:
        return None
    return await db.users.find_one({"id": payload["sub"]}, {"_id": 0})


async def require_user(request: Request) -> dict:
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Please log in to continue")
    if user.get("disabled"):
        raise HTTPException(403, "Your account has been disabled")
    return user


ADMIN_ROLES = ["super_admin", "order_manager", "product_manager", "marketing_manager", "customer_support"]


async def require_admin(request: Request, roles: Optional[list] = None) -> dict:
    token = request.cookies.get(ADMIN_COOKIE)
    payload = decode_token(token, "admin") if token else None
    if not payload:
        raise HTTPException(401, "Admin authentication required")
    admin = await db.admin_users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not admin or not admin.get("active", True):
        raise HTTPException(401, "Admin authentication required")
    if roles and admin.get("role") not in roles and admin.get("role") != "super_admin":
        raise HTTPException(403, "Insufficient permissions for this action")
    return admin


async def audit(admin: dict, action: str, entity: str, entity_id: str, prev=None, new=None, request: Request = None):
    await db.audit_logs.insert_one({
        "id": new_id(),
        "admin_id": admin["id"],
        "admin_email": admin.get("email", ""),
        "action": action,
        "entity": entity,
        "entity_id": str(entity_id),
        "previous": prev,
        "new": new,
        "ip": request.client.host if request and request.client else None,
        "created_at": iso_now(),
    })


async def notify(user_id: str, ntype: str, title: str, message: str, data: dict = None):
    await db.notifications.insert_one({
        "id": new_id(), "user_id": user_id, "type": ntype, "title": title,
        "message": message, "data": data or {}, "read": False, "created_at": iso_now(),
    })


class SSEHub:
    def __init__(self):
        self.queues = set()

    def subscribe(self):
        q = asyncio.Queue()
        self.queues.add(q)
        return q

    def unsubscribe(self, q):
        self.queues.discard(q)

    def publish(self, event: str, data: dict):
        for q in list(self.queues):
            try:
                q.put_nowait({"event": event, "data": data})
            except asyncio.QueueFull:
                pass


hub = SSEHub()


async def get_settings() -> dict:
    return await db.settings.find_one({"id": "global"}, {"_id": 0}) or {}


def cart_key(request: Request, user: Optional[dict] = None) -> str:
    if user:
        return f"user:{user['id']}"
    return f"guest:{request.headers.get('X-Guest-Id', 'anon')}"


async def merge_carts(src: str, dst: str):
    src_cart = await db.carts.find_one({"key": src})
    if not src_cart:
        return
    dst_cart = await db.carts.find_one({"key": dst})
    if not dst_cart:
        await db.carts.update_one({"key": src}, {"$set": {"key": dst}})
        return
    items = {f"{i['product_id']}:{i['variant_id']}": i for i in dst_cart.get("items", [])}
    for i in src_cart.get("items", []):
        k = f"{i['product_id']}:{i['variant_id']}"
        if k in items:
            items[k]["qty"] += i["qty"]
        else:
            items[k] = i
    await db.carts.update_one({"key": dst}, {"$set": {"items": list(items.values()), "updated_at": iso_now()}})
    await db.carts.delete_one({"key": src})


async def validate_coupon(code: str, subtotal: float, user_id: Optional[str] = None):
    if not code:
        return 0, None
    c = await db.coupons.find_one({"code": code.strip().upper()}, {"_id": 0})
    if not c or not c.get("active", True):
        return 0, {"error": "Invalid coupon code"}
    if c.get("expires_at") and c["expires_at"] < iso_now():
        return 0, {"error": "This coupon has expired"}
    if subtotal < c.get("min_order", 0):
        return 0, {"error": f"Minimum order of ₹{c['min_order']} required"}
    if c.get("usage_limit") is not None and c.get("used_count", 0) >= c["usage_limit"]:
        return 0, {"error": "This coupon has been fully redeemed"}
    if c.get("user_id") and c["user_id"] != user_id:
        return 0, {"error": "This coupon is not valid for your account"}
    if user_id and c.get("per_user_limit"):
        used = await db.orders.count_documents({"user_id": user_id, "coupon_code": c["code"], "status": {"$nin": ["cancelled"]}})
        if used >= c["per_user_limit"]:
            return 0, {"error": "You have already used this coupon"}
    if c.get("first_order_only") and user_id:
        prior = await db.orders.count_documents({"user_id": user_id, "status": {"$nin": ["cancelled"]}})
        if prior > 0:
            return 0, {"error": "This coupon is valid on your first order only"}
    if c["type"] == "percent":
        d = subtotal * c["value"] / 100
        if c.get("max_discount"):
            d = min(d, c["max_discount"])
    elif c["type"] == "free_delivery":
        d = 0
    else:
        d = min(c["value"], subtotal)
    return round(d), {"code": c["code"], "discount": round(d), "type": c["type"], "label": c.get("label") or c["code"]}


async def credit_points(user_id: str, points: int, kind: str, note: str, ref: str = ""):
    if points == 0:
        return
    await db.reward_accounts.update_one(
        {"user_id": user_id},
        {"$inc": {"balance": points, "earned": points if points > 0 else 0, "used": -points if points < 0 else 0},
         "$setOnInsert": {"id": new_id()}},
        upsert=True,
    )
    await db.reward_transactions.insert_one({
        "id": new_id(), "user_id": user_id, "points": points, "kind": kind,
        "note": note, "ref": ref, "created_at": iso_now(),
    })


async def get_wallet(user_id: str) -> dict:
    w = await db.reward_accounts.find_one({"user_id": user_id}, {"_id": 0})
    if not w:
        w = {"id": new_id(), "user_id": user_id, "balance": 0, "earned": 0, "used": 0, "expired": 0}
        await db.reward_accounts.insert_one(dict(w))
    return w


async def recompute_product_rating(product_id: str):
    agg = await db.reviews.aggregate([
        {"$match": {"product_id": product_id, "approved": True}},
        {"$group": {"_id": None, "avg": {"$avg": "$rating"}, "n": {"$sum": 1}}},
    ]).to_list(1)
    if agg:
        await db.products.update_one({"id": product_id},
            {"$set": {"rating_avg": round(agg[0]["avg"], 1), "rating_count": agg[0]["n"]}})
    else:
        await db.products.update_one({"id": product_id}, {"$set": {"rating_avg": 0, "rating_count": 0}})


def public_product(p: dict) -> dict:
    variants = p.get("variants", [])
    prices = [v.get("price", 0) for v in variants] or [0]
    mrps = [v.get("mrp", v.get("price", 0)) for v in variants] or [0]
    price = min(prices)
    mrp = max(mrps) if mrps else price
    oos = p.get("out_of_stock", False)
    live_variants = [v for v in variants if not v.get("out_of_stock")]
    stock = 0 if oos else sum(v.get("stock", 0) for v in live_variants)
    colors = sorted({v.get("color") for v in live_variants if v.get("color")})
    first_ok = None if oos else next((v for v in live_variants if v.get("stock", 0) > 0), None)
    return {
        "id": p["id"], "name": p.get("name", ""), "brand": p.get("brand", ""),
        "category_id": p.get("category_id"), "category_name": p.get("category_name", ""),
        "gender": p.get("gender", ""), "images": p.get("images", []),
        "price": price, "mrp": mrp,
        "discount_pct": round((mrp - price) / mrp * 100) if mrp and mrp > price else 0,
        "stock": stock, "colors": colors,
        "rating_avg": p.get("rating_avg", 0), "rating_count": p.get("rating_count", 0),
        "featured": p.get("featured", False), "created_at": p.get("created_at"),
        "first_variant_id": first_ok["id"] if first_ok else (variants[0]["id"] if variants else None),
    }


def public_product_colors(p: dict) -> list:
    """Each color group of a product is its own discoverable card, linked to the parent product."""
    variants = p.get("variants", [])
    groups = {}
    for v in variants:
        key = (v.get("color") or "").strip().lower() or "__default__"
        groups.setdefault(key, []).append(v)
    if len(groups) <= 1:
        return [public_product(p)]
    cards = []
    for ckey, vs in groups.items():
        base = public_product(p)
        live = [v for v in vs if not v.get("out_of_stock")]
        prices = [v.get("price", 0) for v in vs] or [0]
        mrps = [v.get("mrp", v.get("price", 0)) for v in vs] or [0]
        price = min(prices)
        mrp = max(mrps)
        imgs = []
        for v in vs:
            for im in (v.get("images") or []):
                if im not in imgs:
                    imgs.append(im)
        if not imgs:
            imgs = base["images"]
        color_label = "" if ckey == "__default__" else (vs[0].get("color") or "")
        first_ok = None if p.get("out_of_stock") else next((v for v in live if v.get("stock", 0) > 0), None)
        base.update({
            "card_key": f"{p['id']}:{ckey}",
            "color": color_label,
            "images": imgs,
            "price": price,
            "mrp": mrp,
            "discount_pct": round((mrp - price) / mrp * 100) if mrp and mrp > price else 0,
            "stock": 0 if p.get("out_of_stock") else sum(v.get("stock", 0) for v in live),
            "sizes": sorted({v.get("size") for v in vs if v.get("size")}),
            "first_variant_id": first_ok["id"] if first_ok else (vs[0]["id"] if vs else None),
            "link": f"/product/{p['id']}?color={color_label}" if color_label else f"/product/{p['id']}",
        })
        cards.append(base)
    return cards
