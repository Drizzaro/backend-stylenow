import os
import re
import random
from datetime import timedelta

from fastapi import APIRouter, Request, Response, HTTPException
from pydantic import BaseModel

from core import (
    db, utcnow, iso_now, new_id, create_token, public_user, require_user,
    require_admin, verify_password, merge_carts, USER_COOKIE, ADMIN_COOKIE,
)

router = APIRouter(tags=["auth"])

OTP_DEV_MODE = os.environ.get("OTP_DEV_MODE", "true").lower() == "true"


def normalize_phone(p: str) -> str:
    return re.sub(r"\D", "", p or "")[-10:]


class OTPRequestBody(BaseModel):
    phone: str


class OTPVerifyBody(BaseModel):
    phone: str
    otp: str
    name: str | None = None


class ProfileUpdate(BaseModel):
    name: str | None = None
    email: str | None = None
    theme_preference: str | None = None


class AdminLoginBody(BaseModel):
    email: str
    password: str


class AdminProfileUpdate(BaseModel):
    name: str | None = None
    theme_preference: str | None = None


@router.post("/auth/otp/request")
async def request_otp(body: OTPRequestBody, request: Request):
    phone = normalize_phone(body.phone)
    if len(phone) != 10:
        raise HTTPException(400, "Enter a valid 10-digit mobile number")
    hour_ago = (utcnow() - timedelta(hours=1)).isoformat()
    recent = await db.otp_requests.count_documents({"phone": phone, "created_at": {"$gte": hour_ago}})
    if recent >= 5:
        raise HTTPException(429, "Too many OTP requests. Please try again later.")
    code = f"{random.randint(100000, 999999)}"
    await db.otp_requests.update_many({"phone": phone, "consumed": False}, {"$set": {"consumed": True}})
    await db.otp_requests.insert_one({
        "id": new_id(), "phone": phone, "otp": code, "attempts": 0, "consumed": False,
        "expires_at": (utcnow() + timedelta(minutes=10)).isoformat(), "created_at": iso_now(),
        "ip": request.client.host if request.client else None,
    })
    resp = {"sent": True, "dev_mode": OTP_DEV_MODE, "message": "OTP sent to your mobile number"}
    if OTP_DEV_MODE:
        resp["dev_otp"] = code
    return resp


@router.post("/auth/otp/verify")
async def verify_otp(body: OTPVerifyBody, request: Request, response: Response):
    phone = normalize_phone(body.phone)
    rec = await db.otp_requests.find_one({"phone": phone, "consumed": False}, sort=[("created_at", -1)])
    if not rec or rec["expires_at"] < iso_now():
        raise HTTPException(400, "OTP expired. Please request a new one.")
    if rec.get("attempts", 0) >= 5:
        raise HTTPException(429, "Too many incorrect attempts. Request a new OTP.")
    if rec["otp"] != body.otp.strip():
        await db.otp_requests.update_one({"id": rec["id"]}, {"$inc": {"attempts": 1}})
        raise HTTPException(400, "Incorrect OTP. Please try again.")
    await db.otp_requests.update_one({"id": rec["id"]}, {"$set": {"consumed": True}})

    user = await db.users.find_one({"phone": phone}, {"_id": 0})
    is_new = user is None
    if not user:
        user = {
            "id": new_id(), "phone": phone, "name": (body.name or "").strip(), "email": "",
            "theme_preference": "system", "disabled": False,
            "created_at": iso_now(), "last_login_at": iso_now(),
        }
        await db.users.insert_one(user)
        await db.reward_accounts.insert_one({"id": new_id(), "user_id": user["id"], "balance": 0, "earned": 0, "used": 0, "expired": 0})
    else:
        if user.get("disabled"):
            raise HTTPException(403, "Your account has been disabled. Contact support.")
        upd = {"last_login_at": iso_now()}
        if body.name and not user.get("name"):
            upd["name"] = body.name.strip()
            user["name"] = upd["name"]
        await db.users.update_one({"id": user["id"]}, {"$set": upd})

    guest = request.headers.get("X-Guest-Id")
    if guest:
        await merge_carts(f"guest:{guest}", f"user:{user['id']}")

    token = create_token(user["id"], "user", days=30)
    response.set_cookie(USER_COOKIE, token, httponly=True, secure=True, samesite="lax", max_age=30 * 86400, path="/")
    return {"user": public_user(user), "is_new": is_new}


@router.get("/auth/me")
async def auth_me(request: Request):
    user = await require_user(request)
    return {"user": public_user(user)}


@router.put("/auth/me")
async def update_profile(body: ProfileUpdate, request: Request):
    user = await require_user(request)
    upd = {}
    if body.name is not None:
        upd["name"] = body.name.strip()
    if body.email is not None:
        upd["email"] = body.email.strip().lower()
    if body.theme_preference in ("light", "dark", "system"):
        upd["theme_preference"] = body.theme_preference
    if upd:
        await db.users.update_one({"id": user["id"]}, {"$set": upd})
        user.update(upd)
    return {"user": public_user(user)}


@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(USER_COOKIE, path="/")
    return {"ok": True}


# ---------- Google (Emergent-managed OAuth) ----------

class GoogleSessionBody(BaseModel):
    session_id: str


@router.post("/auth/google/session")
async def google_session(body: GoogleSessionBody, request: Request, response: Response):
    import requests as rq
    try:
        r = rq.get(
            "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
            headers={"X-Session-ID": body.session_id}, timeout=15,
        )
    except Exception:
        raise HTTPException(502, "Could not reach Google sign-in service")
    if r.status_code != 200:
        raise HTTPException(401, "Google sign-in failed or session expired")
    data = r.json()
    email = (data.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "Google account has no email")
    user = await db.users.find_one({"email": email}, {"_id": 0})
    is_new = user is None
    if not user:
        user = {
            "id": new_id(), "name": data.get("name", ""), "email": email,
            "picture": data.get("picture", ""), "auth_provider": "google",
            "theme_preference": "system", "disabled": False,
            "created_at": iso_now(), "last_login_at": iso_now(),
        }
        await db.users.insert_one(user)
        await db.reward_accounts.insert_one({"id": new_id(), "user_id": user["id"], "balance": 0, "earned": 0, "used": 0, "expired": 0})
    else:
        if user.get("disabled"):
            raise HTTPException(403, "Your account has been disabled. Contact support.")
        upd = {"last_login_at": iso_now()}
        if data.get("name") and not user.get("name"):
            upd["name"] = data["name"]
        if data.get("picture") and not user.get("picture"):
            upd["picture"] = data["picture"]
        await db.users.update_one({"id": user["id"]}, {"$set": upd})
        user.update(upd)
    await db.user_sessions.insert_one({
        "id": new_id(), "user_id": user["id"], "provider": "google",
        "session_token": data.get("session_token", ""), "created_at": iso_now(),
    })
    guest = request.headers.get("X-Guest-Id")
    if guest:
        await merge_carts(f"guest:{guest}", f"user:{user['id']}")
    token = create_token(user["id"], "user", days=30)
    response.set_cookie(USER_COOKIE, token, httponly=True, secure=True, samesite="lax", max_age=30 * 86400, path="/")
    return {"user": public_user(user), "is_new": is_new}


# ---------- Admin auth ----------

@router.post("/admin/auth/login")
async def admin_login(body: AdminLoginBody, request: Request, response: Response):
    email = body.email.strip().lower()
    ident = f"{request.client.host if request.client else 'unknown'}:{email}"
    att = await db.login_attempts.find_one({"id": ident})
    if att and att.get("count", 0) >= 5 and (att.get("locked_until") or "") > iso_now():
        raise HTTPException(429, "Too many failed attempts. Locked for 15 minutes.")
    admin = await db.admin_users.find_one({"email": email})
    ok = admin and admin.get("active", True) and verify_password(body.password, admin.get("password_hash", ""))
    if not ok:
        count = (att.get("count", 0) if att else 0) + 1
        await db.login_attempts.update_one(
            {"id": ident},
            {"$set": {"count": count, "updated_at": iso_now(),
                      "locked_until": (utcnow() + timedelta(minutes=15)).isoformat() if count >= 5 else None}},
            upsert=True,
        )
        raise HTTPException(401, "Invalid email or password")
    await db.login_attempts.delete_one({"id": ident})
    token = create_token(admin["id"], "admin", hours=12)
    response.set_cookie(ADMIN_COOKIE, token, httponly=True, secure=True, samesite="lax", max_age=12 * 3600, path="/")
    return {"admin": {"id": admin["id"], "email": admin["email"], "name": admin.get("name", "Admin"),
                      "role": admin.get("role", "super_admin"),
                      "theme_preference": admin.get("theme_preference", "system")}}


@router.get("/admin/auth/me")
async def admin_me(request: Request):
    admin = await require_admin(request)
    return {"admin": admin}


@router.put("/admin/auth/me")
async def admin_update(body: AdminProfileUpdate, request: Request):
    admin = await require_admin(request)
    upd = {}
    if body.name is not None:
        upd["name"] = body.name.strip()
    if body.theme_preference in ("light", "dark", "system"):
        upd["theme_preference"] = body.theme_preference
    if upd:
        await db.admin_users.update_one({"id": admin["id"]}, {"$set": upd})
    return {"ok": True}


@router.post("/admin/auth/logout")
async def admin_logout(response: Response):
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return {"ok": True}
