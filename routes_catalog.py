import re
import difflib

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from core import db, iso_now, new_id, get_current_user, public_product, public_product_colors
from storage import get_object


def expand_colors(products):
    out = []
    for p in products:
        out.extend(public_product_colors(p))
    return out

router = APIRouter(tags=["catalog"])


# ---------- Categories ----------

@router.get("/categories")
async def list_categories():
    cats = await db.categories.find({"active": True}, {"_id": 0}).sort("sort", 1).to_list(200)
    return {"items": cats}


# ---------- Products ----------

@router.get("/products")
async def list_products(
    category: str = "", gender: str = "", size: str = "", color: str = "",
    min_price: float = 0, max_price: float = 0, sort: str = "newest",
    featured: bool = False, page: int = 1, limit: int = 24,
):
    filt = {"active": True}
    if category:
        filt["$or"] = [{"category_id": category}, {"category_slug": category}]
    if gender:
        filt["gender"] = gender
    if featured:
        filt["featured"] = True
    if size:
        filt["variants.size"] = size
    if color:
        filt["variants.color"] = {"$regex": f"^{re.escape(color)}$", "$options": "i"}
    products = await db.products.find(filt, {"_id": 0}).to_list(2000)
    items = expand_colors(products)
    if size:
        items = [i for i in items if size in (i.get("sizes") or [])]
    if min_price:
        items = [i for i in items if i["price"] >= min_price]
    if max_price:
        items = [i for i in items if i["price"] <= max_price]
    if sort == "price_low":
        items.sort(key=lambda x: x["price"])
    elif sort == "price_high":
        items.sort(key=lambda x: -x["price"])
    elif sort == "discount":
        items.sort(key=lambda x: -x["discount_pct"])
    elif sort == "popular":
        items.sort(key=lambda x: -x.get("rating_count", 0))
    else:
        items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    total = len(items)
    start = (page - 1) * limit
    return {"items": items[start:start + limit], "total": total, "page": page, "pages": max(1, (total + limit - 1) // limit)}


@router.get("/products/{product_id}")
async def product_detail(product_id: str, request: Request):
    p = await db.products.find_one({"id": product_id, "active": True}, {"_id": 0})
    if not p:
        raise HTTPException(404, "Product not found")
    user = await get_current_user(request)
    if user:
        await db.recently_viewed.update_one(
            {"user_id": user["id"], "product_id": product_id},
            {"$set": {"viewed_at": iso_now()}}, upsert=True)
    reviews = await db.reviews.find({"product_id": product_id, "approved": True}, {"_id": 0}).sort("created_at", -1).to_list(50)
    videos = await db.videos.find({"product_id": product_id, "active": True}, {"_id": 0}).sort("sort", 1).to_list(12)
    similar = await db.products.find(
        {"active": True, "category_id": p.get("category_id"), "id": {"$ne": p["id"]}}, {"_id": 0}).to_list(8)
    return {
        "product": p,
        "summary": public_product(p),
        "reviews": reviews,
        "videos": videos,
        "similar": [public_product(s) for s in similar],
    }


# ---------- Search ----------

async def synonym_map():
    syns = await db.search_synonyms.find({}, {"_id": 0}).to_list(500)
    m = {}
    for s in syns:
        words = {s["keyword"].lower()} | {x.lower() for x in s.get("synonyms", [])}
        for w in words:
            m.setdefault(w, set()).update(words - {w})
    return m


def score_product(p, q, tokens, expanded):
    name = (p.get("name") or "").lower()
    tags = [t.lower() for t in p.get("tags", [])]
    brand = (p.get("brand") or "").lower()
    desc = (p.get("description") or "").lower()
    cat = (p.get("category_name") or "").lower()
    colors = [(v.get("color") or "").lower() for v in p.get("variants", [])]
    s = 0.0
    if q and q in name:
        s += 100
    for t in tokens:
        if t in name:
            s += 40
        if t in tags:
            s += 50
        elif any(t in tag for tag in tags):
            s += 25
        if t and t in brand:
            s += 20
        if t and t in cat:
            s += 20
        if t and any(t == c or (len(t) > 2 and t in c) for c in colors if c):
            s += 30
        if len(t) > 3 and t in desc:
            s += 5
    for t in expanded:
        if t in name:
            s += 20
        if t in tags:
            s += 25
        elif any(t in tag for tag in tags):
            s += 12
    vocab = set(re.findall(r"[a-z0-9]+", name)) | set(tags)
    for t in tokens:
        if len(t) >= 4 and t not in name and t not in tags and not any(t in tag for tag in tags):
            best = max((difflib.SequenceMatcher(None, t, w).ratio() for w in vocab if abs(len(w) - len(t)) <= 2), default=0)
            if best >= 0.82:
                s += 15
    s += min(p.get("order_count", 0), 50) * 0.5
    if any(v.get("stock", 0) > 0 for v in p.get("variants", [])):
        s += 5
    return s


@router.get("/search")
async def search(request: Request, q: str = "", page: int = 1, limit: int = 24):
    q = q.strip()
    if not q:
        return {"items": [], "total": 0, "query": q, "page": 1, "pages": 1}
    products = await db.products.find({"active": True}, {"_id": 0}).to_list(2000)
    syn = await synonym_map()
    tokens = re.findall(r"[a-z0-9]+", q.lower())
    expanded = set()
    for t in tokens:
        expanded |= syn.get(t, set())
    expanded -= set(tokens)
    scored = [(score_product(p, q.lower(), tokens, expanded), p) for p in products]
    scored = [x for x in scored if x[0] >= 20]
    scored.sort(key=lambda x: -x[0])
    user = await get_current_user(request)
    await db.search_logs.insert_one({
        "id": new_id(), "query": q, "results": len(scored),
        "user_id": user["id"] if user else None, "clicked_product": None,
        "created_at": iso_now(),
    })
    items = expand_colors([p for _, p in scored])
    ql = q.lower()
    items.sort(key=lambda i: 0 if i.get("color") and i["color"].lower() in ql else 1)
    total = len(items)
    start = (page - 1) * limit
    return {"items": items[start:start + limit], "total": total, "query": q, "page": page,
            "pages": max(1, (total + limit - 1) // limit)}


@router.get("/search/suggestions")
async def search_suggestions(q: str = ""):
    q = q.strip()
    if len(q) < 2:
        popular = await db.search_logs.aggregate([
            {"$match": {"results": {"$gt": 0}}},
            {"$group": {"_id": "$query", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}}, {"$limit": 6},
        ]).to_list(6)
        return {"suggestions": [p["_id"] for p in popular], "popular": True}
    rx = {"$regex": re.escape(q), "$options": "i"}
    out, seen = [], set()

    def add(x):
        if x and x.lower() not in seen and len(out) < 8:
            seen.add(x.lower())
            out.append(x)

    for p in await db.products.find({"active": True, "name": rx}, {"_id": 0, "name": 1}).to_list(5):
        add(p["name"])
    for c in await db.categories.find({"active": True, "name": rx}, {"_id": 0, "name": 1}).to_list(4):
        add(c["name"])
    for p in await db.products.find({"active": True, "tags": rx}, {"_id": 0, "tags": 1}).to_list(20):
        for t in p.get("tags", []):
            if q.lower() in t.lower():
                add(t.title())
    for s in await db.search_synonyms.find({"$or": [{"keyword": rx}, {"synonyms": rx}]}, {"_id": 0}).to_list(4):
        add(s["keyword"].title())
    return {"suggestions": out, "popular": False}


class SearchClickBody(BaseModel):
    query: str
    product_id: str


@router.post("/search/click")
async def search_click(body: SearchClickBody, request: Request):
    await db.search_logs.update_one(
        {"query": body.query, "clicked_product": None},
        {"$set": {"clicked_product": body.product_id}}, upsert=False)
    return {"ok": True}


# ---------- Homepage & deals ----------

@router.get("/homepage")
async def homepage():
    now = iso_now()
    ticker_all = await db.homepage_deals.find({}, {"_id": 0}).sort("sort", 1).to_list(50)
    ticker = [d for d in ticker_all if d.get("active", True)
              and (not d.get("start_at") or d["start_at"] <= now)
              and (not d.get("end_at") or d["end_at"] >= now)]
    banners = await db.banners.find({"active": True}, {"_id": 0}).sort("sort", 1).to_list(10)
    hp = await db.homepage.find_one({"id": "homepage"}, {"_id": 0}) or {"sections": []}
    sections = []
    for sec in sorted(hp.get("sections", []), key=lambda x: x.get("sort", 0)):
        if not sec.get("enabled", True):
            continue
        items = []
        if sec["type"] == "products" and sec.get("product_ids"):
            prods = await db.products.find({"id": {"$in": sec["product_ids"]}, "active": True}, {"_id": 0}).to_list(24)
            items = expand_colors(prods)
        elif sec["type"] == "category" and sec.get("category_id"):
            prods = await db.products.find({"category_id": sec["category_id"], "active": True}, {"_id": 0}).sort("created_at", -1).to_list(12)
            items = expand_colors(prods)
        elif sec["type"] == "trending":
            prods = await db.products.find({"active": True}, {"_id": 0}).sort("order_count", -1).to_list(12)
            items = expand_colors(prods)
        elif sec["type"] == "new":
            prods = await db.products.find({"active": True}, {"_id": 0}).sort("created_at", -1).to_list(12)
            items = expand_colors(prods)
        sections.append({"key": sec.get("key"), "title": sec.get("title", ""), "type": sec["type"], "items": items})
    categories = await db.categories.find({"active": True}, {"_id": 0}).sort("sort", 1).to_list(50)
    return {"ticker": ticker, "banners": banners, "sections": sections, "categories": categories}


@router.get("/deals/active")
async def active_deals():
    now = iso_now()
    deals = await db.deals.find({"active": True}, {"_id": 0}).to_list(50)
    live = [d for d in deals if (not d.get("start_at") or d["start_at"] <= now) and (not d.get("end_at") or d["end_at"] >= now)]
    for d in live:
        if d.get("product_ids"):
            prods = await db.products.find({"id": {"$in": d["product_ids"]}, "active": True}, {"_id": 0}).to_list(12)
            d["products"] = [public_product(p) for p in prods]
    return {"items": live}


# ---------- Public files & videos ----------

@router.get("/files/{path:path}")
async def serve_file(path: str):
    rec = await db.files.find_one({"storage_path": path, "is_deleted": False}, {"_id": 0})
    if not rec:
        raise HTTPException(404, "File not found")
    try:
        data, ct = get_object(path)
    except Exception:
        raise HTTPException(404, "File unavailable")
    return Response(content=data, media_type=rec.get("content_type") or ct,
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/videos")
async def public_videos(product_id: str = ""):
    filt = {"active": True}
    if product_id:
        filt["product_id"] = product_id
    items = await db.videos.find(filt, {"_id": 0}).sort("sort", 1).to_list(24)
    for v in items:
        p = await db.products.find_one({"id": v.get("product_id"), "active": True}, {"_id": 0})
        v["product"] = public_product(p) if p else None
    return {"items": items}


@router.get("/config")
async def public_config():
    import os
    s = await db.settings.find_one({"id": "global"}, {"_id": 0}) or {}
    razorpay_on = bool(os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET"))
    return {
        "brand": "StyleNow",
        "city": s.get("city", "Bahraich"),
        "eta_min": s.get("delivery_eta_min", 30),
        "eta_max": s.get("delivery_eta_max", 60),
        "delivery_fee": s.get("delivery_fee", 0),
        "free_delivery": s.get("delivery_fee", 0) == 0,
        "payment_mode": "razorpay" if razorpay_on else "simulated",
        "razorpay_key_id": os.environ.get("RAZORPAY_KEY_ID", "") if razorpay_on else "",
        "points_per_spin": s.get("points_per_spin", 50),
        "spin_enabled": s.get("spin_enabled", True),
        "accent": s.get("brand_accent", "#BD8EE4"),
        "social_links": s.get("social_links", {}),
        "contact_phones": s.get("contact_phones", []),
        "try_at_doorstep_enabled": s.get("try_at_doorstep_enabled", True),
        "try_at_doorstep_threshold": s.get("try_at_doorstep_threshold", 499),
        "try_at_doorstep_fee": s.get("try_at_doorstep_fee", 50),
    }
