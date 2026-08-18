import re
import json
import difflib

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from core import iso_now, new_id, get_current_user, public_product, public_product_colors, _parse_jsonb
from storage import get_object
import db

router = APIRouter(tags=["catalog"])


def expand_colors(products):
    out = []
    for p in products:
        out.extend(public_product_colors(p))
    return out


# ---------- Categories ----------

@router.get("/categories")
async def list_categories():
    cats = await db.fetch_all("SELECT * FROM categories WHERE active=true ORDER BY sort ASC")
    return {"items": [dict(c) for c in cats]}


# ---------- Products ----------

@router.get("/products")
async def list_products(
    category: str = "", gender: str = "", size: str = "", color: str = "",
    min_price: float = 0, max_price: float = 0, sort: str = "newest",
    featured: bool = False, page: int = 1, limit: int = 24,
):
    conditions = ["active=true"]
    args = []

    def add(cond, val):
        args.append(val)
        conditions.append(f"{cond}=${len(args)}")

    if category:
        args.append(category)
        conditions.append(f"(category_id=${len(args)} OR category_slug=${len(args)})")
    if gender:
        add("gender", gender)
    if featured:
        conditions.append("featured=true")

    where = " AND ".join(conditions)
    products = await db.fetch_all(f"SELECT * FROM products WHERE {where}", *args)

    items = expand_colors(products)

    # Python-side filtering for variant-level fields
    if size:
        items = [i for i in items if size in (i.get("sizes") or [])]
    if color:
        items = [i for i in items if i.get("color") and re.search(f"^{re.escape(color)}$", i["color"], re.IGNORECASE)]
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
    return {"items": items[start:start + limit], "total": total, "page": page,
            "pages": max(1, (total + limit - 1) // limit)}


@router.get("/products/{product_id}")
async def product_detail(product_id: str, request: Request):
    p = await db.fetch_one("SELECT * FROM products WHERE id=$1 AND active=true", product_id)
    if not p:
        raise HTTPException(404, "Product not found")
    user = await get_current_user(request)
    if user:
        await db.execute(
            """INSERT INTO recently_viewed (user_id, product_id, viewed_at) VALUES ($1,$2,$3)
               ON CONFLICT (user_id, product_id) DO UPDATE SET viewed_at=$3""",
            user["id"], product_id, iso_now()
        )
    reviews = await db.fetch_all(
        "SELECT * FROM reviews WHERE product_id=$1 AND approved=true ORDER BY created_at DESC LIMIT 50",
        product_id
    )
    videos = await db.fetch_all(
        "SELECT * FROM videos WHERE product_id=$1 AND active=true ORDER BY sort ASC LIMIT 12",
        product_id
    )
    similar = await db.fetch_all(
        "SELECT * FROM products WHERE active=true AND category_id=$1 AND id!=$2 LIMIT 8",
        p.get("category_id", ""), product_id
    )
    # Parse array fields from asyncpg
    p_dict = dict(p)
    return {
        "product": p_dict,
        "summary": public_product(p_dict),
        "reviews": [dict(r) for r in reviews],
        "videos": [dict(v) for v in videos],
        "similar": [public_product(dict(s)) for s in similar],
    }


# ---------- Search ----------

async def synonym_map():
    syns = await db.fetch_all("SELECT keyword, synonyms FROM search_synonyms")
    m = {}
    for s in syns:
        raw_syns = s["synonyms"] or []
        if isinstance(raw_syns, str):
            raw_syns = json.loads(raw_syns)
        words = {s["keyword"].lower()} | {x.lower() for x in raw_syns}
        for w in words:
            m.setdefault(w, set()).update(words - {w})
    return m


def score_product(p, q, tokens, expanded):
    """Python-side scoring for synonym expansion and fuzzy match on top of FTS results."""
    name = (p.get("name") or "").lower()
    tags_raw = p.get("tags") or []
    if isinstance(tags_raw, str):
        try:
            tags_raw = json.loads(tags_raw)
        except Exception:
            tags_raw = []
    tags = [t.lower() for t in tags_raw]
    brand = (p.get("brand") or "").lower()
    desc = (p.get("description") or "").lower()
    cat = (p.get("category_name") or "").lower()
    variants = _parse_jsonb(p.get("variants"), [])
    colors = [(v.get("color") or "").lower() for v in variants]
    s = 0.0
    if q and q in name:
        s += 100
    for t in tokens:
        if t in name: s += 40
        if t in tags: s += 50
        elif any(t in tag for tag in tags): s += 25
        if t and t in brand: s += 20
        if t and t in cat: s += 20
        if t and any(t == c or (len(t) > 2 and t in c) for c in colors if c): s += 30
        if len(t) > 3 and t in desc: s += 5
    for t in expanded:
        if t in name: s += 20
        if t in tags: s += 25
        elif any(t in tag for tag in tags): s += 12
    vocab = set(re.findall(r"[a-z0-9]+", name)) | set(tags)
    for t in tokens:
        if len(t) >= 4 and t not in name and t not in tags and not any(t in tag for tag in tags):
            best = max((difflib.SequenceMatcher(None, t, w).ratio() for w in vocab if abs(len(w) - len(t)) <= 2), default=0)
            if best >= 0.82:
                s += 15
    s += min(p.get("order_count", 0), 50) * 0.5
    if any(v.get("stock", 0) > 0 for v in variants):
        s += 5
    return s


@router.get("/search")
async def search(request: Request, q: str = "", page: int = 1, limit: int = 24):
    q = q.strip()
    if not q:
        return {"items": [], "total": 0, "query": q, "page": 1, "pages": 1}

    # Use PostgreSQL FTS first, then re-score with synonym expansion
    products = await db.fetch_all(
        """SELECT * FROM products WHERE active=true AND (
            search_vec @@ plainto_tsquery('english', $1)
            OR name ILIKE $2
            OR brand ILIKE $2
            OR $2 = ANY(tags)
        )""",
        q, f"%{q}%"
    )
    # Also search synonyms
    syn = await synonym_map()
    tokens = re.findall(r"[a-z0-9]+", q.lower())
    expanded = set()
    for t in tokens:
        expanded |= syn.get(t, set())
    expanded -= set(tokens)

    # If expanded synonyms exist, fetch additional products
    if expanded:
        for exp_term in list(expanded)[:3]:  # cap at 3 expansions
            extra = await db.fetch_all(
                "SELECT * FROM products WHERE active=true AND (name ILIKE $1 OR $2 = ANY(tags))",
                f"%{exp_term}%", exp_term
            )
            existing_ids = {p["id"] for p in products}
            for ep in extra:
                if ep["id"] not in existing_ids:
                    products.append(ep)

    scored = [(score_product(dict(p), q.lower(), tokens, expanded), dict(p)) for p in products]
    scored = [x for x in scored if x[0] >= 5]
    scored.sort(key=lambda x: -x[0])

    user = await get_current_user(request)
    await db.execute(
        "INSERT INTO search_logs (id, query, results, user_id, clicked_product, created_at) VALUES ($1,$2,$3,$4,null,$5)",
        new_id(), q, len(scored), user["id"] if user else None, iso_now()
    )

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
        popular = await db.fetch_all(
            """SELECT query, COUNT(*) as n FROM search_logs
               WHERE results > 0 GROUP BY query ORDER BY n DESC LIMIT 6"""
        )
        return {"suggestions": [p["query"] for p in popular], "popular": True}

    out, seen = [], set()

    def add(x):
        if x and x.lower() not in seen and len(out) < 8:
            seen.add(x.lower())
            out.append(x)

    names = await db.fetch_all(
        "SELECT name FROM products WHERE active=true AND name ILIKE $1 LIMIT 5",
        f"%{q}%"
    )
    for p in names:
        add(p["name"])

    cats = await db.fetch_all(
        "SELECT name FROM categories WHERE active=true AND name ILIKE $1 LIMIT 4",
        f"%{q}%"
    )
    for c in cats:
        add(c["name"])

    prods_tags = await db.fetch_all(
        "SELECT tags FROM products WHERE active=true AND tags::text ILIKE $1 LIMIT 20",
        f"%{q}%"
    )
    for p in prods_tags:
        tags = _parse_jsonb(p["tags"], [])
        for t in tags:
            if q.lower() in t.lower():
                add(t.title())

    return {"suggestions": out, "popular": False}


class SearchClickBody(BaseModel):
    query: str
    product_id: str


@router.post("/search/click")
async def search_click(body: SearchClickBody, request: Request):
    await db.execute(
        """UPDATE search_logs SET clicked_product=$1
           WHERE id=(
               SELECT id FROM search_logs WHERE query=$2 AND clicked_product IS NULL
               ORDER BY created_at DESC LIMIT 1
           )""",
        body.product_id, body.query
    )
    return {"ok": True}


# ---------- Homepage & deals ----------

@router.get("/homepage")
async def homepage():
    now = iso_now()
    ticker_all = await db.fetch_all("SELECT * FROM homepage_deals ORDER BY sort ASC")
    ticker = [
        dict(d) for d in ticker_all
        if d.get("active", True)
        and (not d.get("start_at") or d["start_at"] <= now)
        and (not d.get("end_at") or d["end_at"] >= now)
    ]
    banners = await db.fetch_all("SELECT * FROM banners WHERE active=true ORDER BY sort ASC LIMIT 10")
    hp_row = await db.fetch_one("SELECT sections FROM homepage WHERE id='homepage'")
    hp_sections = _parse_jsonb(hp_row["sections"] if hp_row else None, [])

    sections = []
    for sec in sorted(hp_sections, key=lambda x: x.get("sort", 0)):
        if not sec.get("enabled", True):
            continue
        items = []
        stype = sec["type"]
        if stype == "products" and sec.get("product_ids"):
            ids = sec["product_ids"]
            prods = await db.fetch_all(
                f"SELECT * FROM products WHERE id=ANY($1) AND active=true",
                ids
            )
            items = expand_colors([dict(p) for p in prods])
        elif stype == "category" and sec.get("category_id"):
            prods = await db.fetch_all(
                "SELECT * FROM products WHERE category_id=$1 AND active=true ORDER BY created_at DESC LIMIT 12",
                sec["category_id"]
            )
            items = expand_colors([dict(p) for p in prods])
        elif stype == "trending":
            prods = await db.fetch_all(
                "SELECT * FROM products WHERE active=true ORDER BY order_count DESC LIMIT 12"
            )
            items = expand_colors([dict(p) for p in prods])
        elif stype == "new":
            prods = await db.fetch_all(
                "SELECT * FROM products WHERE active=true ORDER BY created_at DESC LIMIT 12"
            )
            items = expand_colors([dict(p) for p in prods])
        sections.append({"key": sec.get("key"), "title": sec.get("title", ""), "type": stype, "items": items})

    categories = await db.fetch_all("SELECT * FROM categories WHERE active=true ORDER BY sort ASC LIMIT 50")
    return {
        "ticker": ticker,
        "banners": [dict(b) for b in banners],
        "sections": sections,
        "categories": [dict(c) for c in categories],
    }


@router.get("/deals/active")
async def active_deals():
    now = iso_now()
    deals = await db.fetch_all("SELECT * FROM deals WHERE active=true")
    live = []
    for d in deals:
        d = dict(d)
        start = d.get("start_at") or ""
        end = d.get("end_at") or ""
        if (not start or start <= now) and (not end or end >= now):
            if d.get("product_ids"):
                pids = d["product_ids"]
                if isinstance(pids, str):
                    pids = json.loads(pids)
                prods = await db.fetch_all(
                    "SELECT * FROM products WHERE id=ANY($1) AND active=true", pids
                )
                d["products"] = [public_product(dict(p)) for p in prods]
            live.append(d)
    return {"items": live}


# ---------- Public files & videos ----------

@router.get("/files/{path:path}")
async def serve_file(path: str):
    rec = await db.fetch_one("SELECT * FROM files WHERE storage_path=$1 AND is_deleted=false", path)
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
    if product_id:
        vids = await db.fetch_all(
            "SELECT * FROM videos WHERE active=true AND product_id=$1 ORDER BY sort ASC LIMIT 24",
            product_id
        )
    else:
        vids = await db.fetch_all(
            "SELECT * FROM videos WHERE active=true ORDER BY sort ASC LIMIT 24"
        )
    result = []
    for v in vids:
        v = dict(v)
        p = await db.fetch_one("SELECT * FROM products WHERE id=$1 AND active=true", v.get("product_id", ""))
        v["product"] = public_product(dict(p)) if p else None
        result.append(v)
    return {"items": result}


@router.get("/config")
async def public_config():
    import os as _os
    s = await db.fetch_one("SELECT * FROM settings WHERE id='global'") or {}
    s = dict(s) if s else {}
    razorpay_on = bool(_os.environ.get("RAZORPAY_KEY_ID") and _os.environ.get("RAZORPAY_KEY_SECRET"))
    social = _parse_jsonb(s.get("social_links"), {})
    phones = _parse_jsonb(s.get("contact_phones"), [])
    return {
        "brand": "StyleNow",
        "city": s.get("city", "Bahraich"),
        "eta_min": s.get("delivery_eta_min", 30),
        "eta_max": s.get("delivery_eta_max", 60),
        "delivery_fee": s.get("delivery_fee", 0),
        "free_delivery": (s.get("delivery_fee") or 0) == 0,
        "payment_mode": "razorpay" if razorpay_on else "simulated",
        "razorpay_key_id": _os.environ.get("RAZORPAY_KEY_ID", "") if razorpay_on else "",
        "points_per_spin": s.get("points_per_spin", 50),
        "spin_enabled": s.get("spin_enabled", True),
        "accent": s.get("brand_accent", "#BD8EE4"),
        "social_links": social,
        "contact_phones": phones,
        "try_at_doorstep_enabled": s.get("try_at_doorstep_enabled", True),
        "try_at_doorstep_threshold": s.get("try_at_doorstep_threshold", 499),
        "try_at_doorstep_fee": s.get("try_at_doorstep_fee", 50),
    }
