from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

import os
import logging
from fastapi import FastAPI, APIRouter
from starlette.middleware.cors import CORSMiddleware

from core import db, hash_password, new_id, iso_now
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


async def ensure_index(coll, keys, **kwargs):
    try:
        await coll.create_index(keys, **kwargs)
    except Exception as e:
        logger.warning(f"index skipped: {e}")


@app.on_event("startup")
async def startup():
    try:
        storage.init_storage()
        logger.info("Object storage initialized")
    except Exception as e:
        logger.error("Storage init failed: %s", e)
    try:
        await db.users.drop_index("phone_1")
    except Exception:
        pass
    await ensure_index(db.users, "phone", unique=True, sparse=True)
    await ensure_index(db.products, [("name", "text"), ("tags", "text"), ("brand", "text"), ("description", "text")])
    await ensure_index(db.products, "category_id")
    await ensure_index(db.orders, "user_id")
    await ensure_index(db.orders, "status")
    await ensure_index(db.coupons, "code", unique=True)
    await ensure_index(db.otp_requests, "phone")
    await ensure_index(db.notifications, "user_id")
    await ensure_index(db.search_logs, "query")
    await ensure_index(db.admin_users, "email", unique=True)
    await ensure_index(db.videos, "product_id")
    await db.settings.update_one({"id": "global", "points_value_rupee": {"$exists": False}},
                                 {"$set": {"points_value_rupee": 1}})
    await db.settings.update_one({"id": "global", "social_links": {"$exists": False}},
                                 {"$set": {"social_links": {"facebook": "", "instagram": "", "x": "", "youtube": "", "pinterest": "", "whatsapp": ""}}})
    await db.settings.update_one({"id": "global", "social_links.whatsapp": {"$exists": False}},
                                 {"$set": {"social_links.whatsapp": ""}})
    await db.settings.update_one({"id": "global", "contact_phones": {"$exists": False}},
                                 {"$set": {"contact_phones": [{"label": "Customer Care", "number": "+91 80000 00000"}]}})
    await db.settings.update_one({"id": "global", "try_at_doorstep_threshold": {"$exists": False}},
                                 {"$set": {"try_at_doorstep_threshold": 499, "try_at_doorstep_fee": 50, "try_at_doorstep_enabled": True}})

    if not await db.settings.find_one({"id": "global"}):
        await db.settings.insert_one({
            "id": "global", "city": "Bahraich", "delivery_fee": 0,
            "delivery_eta_min": 30, "delivery_eta_max": 60,
            "points_per_spin": 50, "points_per_rupee": 0.05,
            "low_stock_threshold": 5, "spin_enabled": True, "brand_accent": "#BD8EE4",
            "created_at": iso_now(),
        })
    if not await db.stores.find_one({}):
        await db.stores.insert_one({
            "id": new_id(), "name": "StyleNow Bahraich Central", "city": "Bahraich",
            "active": True, "radius_km": 10, "eta": "30-60 min", "created_at": iso_now(),
        })

    admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    if admin_email and admin_password:
        existing = await db.admin_users.find_one({"email": admin_email})
        if not existing:
            await db.admin_users.insert_one({
                "id": new_id(), "email": admin_email, "name": "StyleNow Admin",
                "password_hash": hash_password(admin_password), "role": "super_admin",
                "active": True, "theme_preference": "system", "created_at": iso_now(),
            })
            logger.info("Super admin seeded: %s", admin_email)
        elif not existing.get("password_hash") or not __import__("bcrypt").checkpw(admin_password.encode(), existing["password_hash"].encode()):
            await db.admin_users.update_one({"email": admin_email},
                {"$set": {"password_hash": hash_password(admin_password), "active": True, "role": "super_admin"}})

    if await db.search_synonyms.count_documents({}) == 0:
        await db.search_synonyms.insert_many([
            {"id": new_id(), "keyword": "tshirt", "synonyms": ["t-shirt", "tee", "tees", "tshirts"]},
            {"id": new_id(), "keyword": "oversized", "synonyms": ["oversize", "baggy", "loose fit", "relaxed fit", "loose"]},
            {"id": new_id(), "keyword": "pants", "synonyms": ["trousers", "lowers"]},
            {"id": new_id(), "keyword": "hoodie", "synonyms": ["hoodies", "sweatshirt", "sweatshirts"]},
            {"id": new_id(), "keyword": "shoes", "synonyms": ["sneakers", "footwear", "kicks"]},
        ])

    if await db.spin_rewards.count_documents({}) == 0:
        await db.spin_rewards.insert_many([
            {"id": new_id(), "label": "₹50 OFF", "type": "coupon_flat", "value": 50, "probability": 20, "expiry_days": 7, "active": True, "created_at": iso_now()},
            {"id": new_id(), "label": "₹100 OFF", "type": "coupon_flat", "value": 100, "probability": 10, "expiry_days": 7, "active": True, "created_at": iso_now()},
            {"id": new_id(), "label": "10% OFF", "type": "coupon_percent", "value": 10, "probability": 15, "expiry_days": 7, "active": True, "created_at": iso_now()},
            {"id": new_id(), "label": "20% OFF", "type": "coupon_percent", "value": 20, "probability": 8, "expiry_days": 7, "active": True, "created_at": iso_now()},
            {"id": new_id(), "label": "50 StylePoints", "type": "points", "value": 50, "probability": 15, "expiry_days": 0, "active": True, "created_at": iso_now()},
            {"id": new_id(), "label": "100 StylePoints", "type": "points", "value": 100, "probability": 7, "expiry_days": 0, "active": True, "created_at": iso_now()},
            {"id": new_id(), "label": "Free Delivery", "type": "free_delivery", "value": 0, "probability": 10, "expiry_days": 7, "active": True, "created_at": iso_now()},
            {"id": new_id(), "label": "Better Luck Next Time", "type": "none", "value": 0, "probability": 15, "expiry_days": 0, "active": True, "created_at": iso_now()},
        ])

    if await db.homepage_deals.count_documents({}) == 0:
        await db.homepage_deals.insert_many([
            {"id": new_id(), "text": "FREE DELIVERY on every order", "icon": "truck", "link": "", "active": True, "sort": 1, "start_at": "", "end_at": "", "created_at": iso_now()},
            {"id": new_id(), "text": "30–60 MINUTE DELIVERY in Bahraich", "icon": "zap", "link": "", "active": True, "sort": 2, "start_at": "", "end_at": "", "created_at": iso_now()},
            {"id": new_id(), "text": "SPIN & WIN — earn StylePoints with every order", "icon": "gift", "link": "/spin", "active": True, "sort": 3, "start_at": "", "end_at": "", "created_at": iso_now()},
        ])

    if not await db.homepage.find_one({"id": "homepage"}):
        await db.homepage.insert_one({
            "id": "homepage",
            "sections": [
                {"key": "trending", "title": "Trending Now", "type": "trending", "enabled": True, "sort": 1},
                {"key": "new", "title": "New Arrivals", "type": "new", "enabled": True, "sort": 2},
            ],
        })
    logger.info("StyleNow API ready")


@app.on_event("shutdown")
async def shutdown_db_client():
    from core import client
    client.close()
