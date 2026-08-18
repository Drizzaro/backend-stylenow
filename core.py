import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt
import bcrypt
from fastapi import HTTPException, Request

import db as _db

logger = logging.getLogger("stylenow")

JWT_SECRET = os.environ["JWT_SECRET"]
JWT_ALG = "HS256"
USER_COOKIE = "sn_token"
ADMIN_COOKIE = "sn_admin"


# ─────────────────────────────────────────
# Time helpers
# ─────────────────────────────────────────

def utcnow():
    return datetime.now(timezone.utc)


def iso_now():
    return utcnow().isoformat()


def new_id():
    return uuid.uuid4().hex


def short_id():
    return uuid.uuid4().hex[:8].upper()


# ─────────────────────────────────────────
# Password helpers
# ─────────────────────────────────────────

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ─────────────────────────────────────────
# JWT helpers
# ─────────────────────────────────────────

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


# ─────────────────────────────────────────
# User auth
# ─────────────────────────────────────────

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
    return await _db.fetch_one("SELECT * FROM users WHERE id=$1", payload["sub"])


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
    admin = await _db.fetch_one(
        "SELECT id, email, name, role, active, theme_preference FROM admin_users WHERE id=$1",
        payload["sub"]
    )
    if not admin or not admin.get("active", True):
        raise HTTPException(401, "Admin authentication required")
    if roles and admin.get("role") not in roles and admin.get("role") != "super_admin":
        raise HTTPException(403, "Insufficient permissions for this action")
    return admin


# ─────────────────────────────────────────
# Settings
# ─────────────────────────────────────────

async def get_settings() -> dict:
    row = await _db.fetch_one("SELECT * FROM settings WHERE id='global'")
    if not row:
        return {}
    # Parse JSONB fields from string if needed
    for field in ("social_links", "contact_phones"):
        v = row.get(field)
        if isinstance(v, str):
            try:
                row[field] = json.loads(v)
            except Exception:
                row[field] = {} if field == "social_links" else []
    return dict(row)


# ─────────────────────────────────────────
# Cart helpers
# ─────────────────────────────────────────

def cart_key(request: Request, user: Optional[dict] = None) -> str:
    if user:
        return f"user:{user['id']}"
    return f"guest:{request.headers.get('X-Guest-Id', 'anon')}"


async def merge_carts(src: str, dst: str):
    src_cart = await _db.fetch_one("SELECT * FROM carts WHERE key=$1", src)
    if not src_cart:
        return
    dst_cart = await _db.fetch_one("SELECT * FROM carts WHERE key=$1", dst)

    src_items = _parse_jsonb(src_cart.get("items"), [])
    if not dst_cart:
        await _db.execute(
            "UPDATE carts SET key=$1 WHERE key=$2",
            dst, src
        )
        return

    dst_items = _parse_jsonb(dst_cart.get("items"), [])
    items = {f"{i['product_id']}:{i['variant_id']}": i for i in dst_items}
    for i in src_items:
        k = f"{i['product_id']}:{i['variant_id']}"
        if k in items:
            items[k]["qty"] += i["qty"]
        else:
            items[k] = i
    merged = list(items.values())
    await _db.execute(
        "UPDATE carts SET items=$1, updated_at=$2 WHERE key=$3",
        json.dumps(merged), iso_now(), dst
    )
    await _db.execute("DELETE FROM carts WHERE key=$1", src)


def _parse_jsonb(val, default):
    """asyncpg returns JSONB as Python objects already; guard against string."""
    if val is None:
        return default
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return val


# ─────────────────────────────────────────
# Coupon validation
# ─────────────────────────────────────────

async def validate_coupon(code: str, subtotal: float, user_id: Optional[str] = None):
    if not code:
        return 0, None
    c = await _db.fetch_one(
        "SELECT * FROM coupons WHERE code=$1",
        code.strip().upper()
    )
    if not c or not c.get("active", True):
        return 0, {"error": "Invalid coupon code"}
    if c.get("expires_at") and c["expires_at"] < iso_now():
        return 0, {"error": "This coupon has expired"}
    if subtotal < (c.get("min_order") or 0):
        return 0, {"error": f"Minimum order of ₹{c['min_order']} required"}
    if c.get("usage_limit") is not None and (c.get("used_count") or 0) >= c["usage_limit"]:
        return 0, {"error": "This coupon has been fully redeemed"}
    if c.get("user_id") and c["user_id"] != user_id:
        return 0, {"error": "This coupon is not valid for your account"}
    if user_id and c.get("per_user_limit"):
        used = await _db.fetch_val(
            "SELECT COUNT(*) FROM orders WHERE user_id=$1 AND coupon_code=$2 AND status != 'cancelled'",
            user_id, c["code"]
        )
        if (used or 0) >= c["per_user_limit"]:
            return 0, {"error": "You have already used this coupon"}
    if c.get("first_order_only") and user_id:
        prior = await _db.fetch_val(
            "SELECT COUNT(*) FROM orders WHERE user_id=$1 AND status != 'cancelled'",
            user_id
        )
        if (prior or 0) > 0:
            return 0, {"error": "This coupon is valid on your first order only"}

    ctype = c["type"]
    value = c.get("value") or 0
    if ctype == "percent":
        d = subtotal * value / 100
        if c.get("max_discount"):
            d = min(d, c["max_discount"])
    elif ctype == "free_delivery":
        d = 0
    else:
        d = min(value, subtotal)
    return round(d), {"code": c["code"], "discount": round(d), "type": ctype, "label": c.get("label") or c["code"]}


# ─────────────────────────────────────────
# Rewards / wallet
# ─────────────────────────────────────────

async def credit_points(user_id: str, points: int, kind: str, note: str, ref: str = ""):
    if points == 0:
        return
    # Upsert reward account
    await _db.execute(
        """
        INSERT INTO reward_accounts (id, user_id, balance, earned, used, expired)
        VALUES ($1, $2, $3, $4, $5, 0)
        ON CONFLICT (user_id) DO UPDATE SET
            balance = reward_accounts.balance + $3,
            earned  = reward_accounts.earned  + CASE WHEN $3 > 0 THEN $3 ELSE 0 END,
            used    = reward_accounts.used    + CASE WHEN $3 < 0 THEN -$3 ELSE 0 END
        """,
        new_id(), user_id, points,
        points if points > 0 else 0,
        -points if points < 0 else 0,
    )
    await _db.execute(
        "INSERT INTO reward_transactions (id, user_id, points, kind, note, ref, created_at) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        new_id(), user_id, points, kind, note, ref, iso_now()
    )


async def get_wallet(user_id: str) -> dict:
    w = await _db.fetch_one("SELECT * FROM reward_accounts WHERE user_id=$1", user_id)
    if not w:
        wid = new_id()
        await _db.execute(
            "INSERT INTO reward_accounts (id, user_id, balance, earned, used, expired) VALUES ($1,$2,0,0,0,0) ON CONFLICT DO NOTHING",
            wid, user_id
        )
        w = {"id": wid, "user_id": user_id, "balance": 0, "earned": 0, "used": 0, "expired": 0}
    return dict(w)


# ─────────────────────────────────────────
# Product rating
# ─────────────────────────────────────────

async def recompute_product_rating(product_id: str):
    row = await _db.fetch_one(
        "SELECT AVG(rating) as avg, COUNT(*) as n FROM reviews WHERE product_id=$1 AND approved=true",
        product_id
    )
    if row and row["n"]:
        await _db.execute(
            "UPDATE products SET rating_avg=$1, rating_count=$2 WHERE id=$3",
            round(float(row["avg"] or 0), 1), int(row["n"]), product_id
        )
    else:
        await _db.execute(
            "UPDATE products SET rating_avg=0, rating_count=0 WHERE id=$1",
            product_id
        )


# ─────────────────────────────────────────
# Notifications
# ─────────────────────────────────────────

async def notify(user_id: str, ntype: str, title: str, message: str, data: dict = None):
    await _db.execute(
        "INSERT INTO notifications (id, user_id, type, title, message, data, read, created_at) VALUES ($1,$2,$3,$4,$5,$6,false,$7)",
        new_id(), user_id, ntype, title, message, json.dumps(data or {}), iso_now()
    )


# ─────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────

async def audit(admin: dict, action: str, entity: str, entity_id: str,
                prev=None, new=None, request: Request = None):
    await _db.execute(
        """INSERT INTO audit_logs (id, admin_id, admin_email, action, entity, entity_id, previous, new, ip, created_at)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
        new_id(), admin["id"], admin.get("email", ""), action, entity, str(entity_id),
        json.dumps(prev) if prev is not None else None,
        json.dumps(new) if new is not None else None,
        request.client.host if request and request.client else None,
        iso_now()
    )


# ─────────────────────────────────────────
# SSE Hub (unchanged — pure Python)
# ─────────────────────────────────────────

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


# ─────────────────────────────────────────
# Product public views (pure Python — unchanged)
# ─────────────────────────────────────────

def public_product(p: dict) -> dict:
    variants = _parse_jsonb(p.get("variants"), [])
    prices = [v.get("price", 0) for v in variants] or [0]
    mrps = [v.get("mrp", v.get("price", 0)) for v in variants] or [0]
    price = min(prices)
    mrp = max(mrps) if mrps else price
    oos = p.get("out_of_stock", False)
    live_variants = [v for v in variants if not v.get("out_of_stock")]
    stock = 0 if oos else sum(v.get("stock", 0) for v in live_variants)
    colors = sorted({v.get("color") for v in live_variants if v.get("color")})
    first_ok = None if oos else next((v for v in live_variants if v.get("stock", 0) > 0), None)
    tags = p.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    images = p.get("images") or []
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except Exception:
            images = []
    return {
        "id": p["id"], "name": p.get("name", ""), "brand": p.get("brand", ""),
        "category_id": p.get("category_id"), "category_name": p.get("category_name", ""),
        "gender": p.get("gender", ""), "images": images,
        "price": price, "mrp": mrp,
        "discount_pct": round((mrp - price) / mrp * 100) if mrp and mrp > price else 0,
        "stock": stock, "colors": colors,
        "rating_avg": p.get("rating_avg", 0), "rating_count": p.get("rating_count", 0),
        "featured": p.get("featured", False), "created_at": p.get("created_at"),
        "first_variant_id": first_ok["id"] if first_ok else (variants[0]["id"] if variants else None),
    }


def public_product_colors(p: dict) -> list:
    variants = _parse_jsonb(p.get("variants"), [])
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
