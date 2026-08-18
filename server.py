# ruff: noqa: E402
import os
import json
import logging
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from fastapi import FastAPI, APIRouter
from starlette.middleware.cors import CORSMiddleware

import db
from core import (
    hash_password, new_id, iso_now, get_settings
)
from routes_auth import router as auth_router
from routes_catalog import router as catalog_router
from routes_shop import router as shop_router
from routes_orders import router as orders_router
from routes_admin import router as admin_router
import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("stylenow")

app = FastAPI(title="StyleNow API")

api_router = APIRouter(prefix="/api")
api_router.include_router(auth_router)
api_router.include_router(catalog_router)
api_router.include_router(shop_router)
api_router.include_router(orders_router)
api_router.include_router(admin_router)
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


def _split_sql(sql: str) -> list:
    """Split SQL script on semicolons, but NOT inside dollar-quoted $$ blocks."""
    statements = []
    buf = []
    in_dollars = False
    i = 0
    while i < len(sql):
        if not in_dollars and sql[i:i+2] == "--":
            # skip comment
            while i < len(sql) and sql[i] != "\n":
                i += 1
            continue
        # Detect $$ delimiter
        if sql[i:i+2] == "$$":
            in_dollars = not in_dollars
            buf.append("$$")
            i += 2
            continue
        if sql[i] == ";" and not in_dollars:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(sql[i])
        i += 1
    # Remaining
    stmt = "".join(buf).strip()
    if stmt:
        statements.append(stmt)
    return statements


async def _run_schema():
    """Apply schema.sql (idempotent — all CREATE IF NOT EXISTS)."""
    schema_path = ROOT_DIR / "schema.sql"
    if not schema_path.exists():
        logger.warning("schema.sql not found, skipping schema apply")
        return
    sql = schema_path.read_text(encoding="utf-8")
    statements = _split_sql(sql)
    async with db._pool.acquire() as conn:
        for stmt in statements:
            if not stmt.strip():
                continue
            try:
                await conn.execute(stmt)
            except Exception as e:
                logger.warning("Schema stmt skipped: %s | %.80s", str(e)[:120], stmt)
    logger.info("Schema applied — %d statements processed", len(statements))


async def _seed_data():
    """Seed default data — all INSERT ... ON CONFLICT DO NOTHING."""

    # Global settings
    existing = await db.fetch_one("SELECT id FROM settings WHERE id='global'")
    if not existing:
        await db.execute(
            """INSERT INTO settings (id, city, delivery_fee, delivery_eta_min, delivery_eta_max,
               points_per_spin, points_per_rupee, points_value_rupee, low_stock_threshold,
               spin_enabled, brand_accent, social_links, contact_phones,
               try_at_doorstep_enabled, try_at_doorstep_threshold, try_at_doorstep_fee, created_at)
               VALUES ('global','Bahraich',0,30,60,50,0.05,1,5,true,'#BD8EE4',
               '{"facebook":"","instagram":"","x":"","youtube":"","pinterest":"","whatsapp":""}',
               '[{"label":"Customer Care","number":"+91 80000 00000"}]',
               true,499,50,$1)
               ON CONFLICT DO NOTHING""",
            iso_now()
        )

    # Default store
    store_count = await db.fetch_val("SELECT COUNT(*) FROM stores")
    if not store_count:
        await db.execute(
            """INSERT INTO stores (id, name, city, active, radius_km, eta, created_at)
               VALUES ($1,'StyleNow Bahraich Central','Bahraich',true,10,'30-60 min',$2)
               ON CONFLICT DO NOTHING""",
            new_id(), iso_now()
        )

    # Homepage deals
    deals_count = await db.fetch_val("SELECT COUNT(*) FROM homepage_deals")
    if not deals_count:
        for sort, text, icon, link in [
            (1, "FREE DELIVERY on every order", "truck", ""),
            (2, "30–60 MINUTE DELIVERY in Bahraich", "zap", ""),
            (3, "SPIN & WIN — earn StylePoints with every order", "gift", "/spin"),
        ]:
            await db.execute(
                """INSERT INTO homepage_deals (id, text, icon, link, active, sort, start_at, end_at, created_at)
                   VALUES ($1,$2,$3,$4,true,$5,'','',$6) ON CONFLICT DO NOTHING""",
                new_id(), text, icon, link, sort, iso_now()
            )

    # Spin rewards
    spin_count = await db.fetch_val("SELECT COUNT(*) FROM spin_rewards")
    if not spin_count:
        spin_defaults = [
            ("₹50 OFF", "coupon_flat", 50, 20, 7),
            ("₹100 OFF", "coupon_flat", 100, 10, 7),
            ("10% OFF", "coupon_percent", 10, 15, 7),
            ("20% OFF", "coupon_percent", 20, 8, 7),
            ("50 StylePoints", "points", 50, 15, 0),
            ("100 StylePoints", "points", 100, 7, 0),
            ("Free Delivery", "free_delivery", 0, 10, 7),
            ("Better Luck Next Time", "none", 0, 15, 0),
        ]
        for label, stype, value, prob, expiry in spin_defaults:
            await db.execute(
                """INSERT INTO spin_rewards (id, label, type, value, probability, expiry_days, active, created_at)
                   VALUES ($1,$2,$3,$4,$5,$6,true,$7) ON CONFLICT DO NOTHING""",
                new_id(), label, stype, value, prob, expiry, iso_now()
            )

    # Homepage sections
    hp = await db.fetch_one("SELECT id FROM homepage WHERE id='homepage'")
    if not hp:
        sections = json.dumps([
            {"key": "trending", "title": "Trending Now", "type": "trending", "enabled": True, "sort": 1},
            {"key": "new", "title": "New Arrivals", "type": "new", "enabled": True, "sort": 2},
        ])
        await db.execute(
            "INSERT INTO homepage (id, sections) VALUES ('homepage',$1) ON CONFLICT DO NOTHING",
            sections
        )

    # Search synonyms
    syn_count = await db.fetch_val("SELECT COUNT(*) FROM search_synonyms")
    if not syn_count:
        synonyms = [
            ("tshirt", ["t-shirt", "tee", "tees", "tshirts"]),
            ("oversized", ["oversize", "baggy", "loose fit", "relaxed fit", "loose"]),
            ("pants", ["trousers", "lowers"]),
            ("hoodie", ["hoodies", "sweatshirt", "sweatshirts"]),
            ("shoes", ["sneakers", "footwear", "kicks"]),
        ]
        for kw, syns in synonyms:
            await db.execute(
                "INSERT INTO search_synonyms (id, keyword, synonyms) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                new_id(), kw, syns
            )

    # Admin user seed
    admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if admin_email and admin_password:
        existing_admin = await db.fetch_one(
            "SELECT id, password_hash FROM admin_users WHERE email=$1", admin_email
        )
        if not existing_admin:
            await db.execute(
                """INSERT INTO admin_users (id, email, name, password_hash, role, active, theme_preference, created_at)
                   VALUES ($1,$2,'StyleNow Admin',$3,'super_admin',true,'system',$4)
                   ON CONFLICT DO NOTHING""",
                new_id(), admin_email, hash_password(admin_password), iso_now()
            )
            logger.info("Super admin seeded: %s", admin_email)

    logger.info("Seed data applied")


@app.on_event("startup")
async def startup():
    try:
        storage.init_storage()
        logger.info("Object storage initialized")
    except Exception as e:
        logger.error("Storage init failed: %s", e)

    await db.init_pool()
    logger.info("Database pool initialized")

    await _run_schema()
    await _seed_data()
    logger.info("StyleNow API ready")


@app.on_event("shutdown")
async def shutdown():
    await db.close_pool()
    logger.info("Database pool closed")
