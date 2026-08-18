import os
import re
import json
import random
from datetime import timedelta

from fastapi import APIRouter, Request, Response, HTTPException
from pydantic import BaseModel

from core import (
    utcnow, iso_now, new_id, create_token, public_user, require_user,
    require_admin, verify_password, merge_carts, USER_COOKIE, ADMIN_COOKIE, hash_password
)
import db

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
    recent = await db.fetch_val(
        "SELECT COUNT(*) FROM otp_requests WHERE phone=$1 AND created_at >= $2",
        phone, hour_ago
    )
    if (recent or 0) >= 5:
        raise HTTPException(429, "Too many OTP requests. Please try again later.")
    code = f"{random.randint(100000, 999999)}"
    await db.execute(
        "UPDATE otp_requests SET consumed=true WHERE phone=$1 AND consumed=false",
        phone
    )
    await db.execute(
        """INSERT INTO otp_requests (id, phone, otp, attempts, consumed, expires_at, created_at, ip)
           VALUES ($1,$2,$3,0,false,$4,$5,$6)""",
        new_id(), phone, code,
        (utcnow() + timedelta(minutes=10)).isoformat(), iso_now(),
        request.client.host if request.client else None
    )
    resp = {"sent": True, "dev_mode": OTP_DEV_MODE, "message": "OTP sent to your mobile number"}
    if OTP_DEV_MODE:
        resp["dev_otp"] = code
    return resp


@router.post("/auth/otp/verify")
async def verify_otp(body: OTPVerifyBody, request: Request, response: Response):
    phone = normalize_phone(body.phone)
    rec = await db.fetch_one(
        "SELECT * FROM otp_requests WHERE phone=$1 AND consumed=false ORDER BY created_at DESC LIMIT 1",
        phone
    )
    if not rec or rec["expires_at"] < iso_now():
        raise HTTPException(400, "OTP expired. Please request a new one.")
    if (rec.get("attempts") or 0) >= 5:
        raise HTTPException(429, "Too many incorrect attempts. Request a new OTP.")
    if rec["otp"] != body.otp.strip():
        await db.execute(
            "UPDATE otp_requests SET attempts=attempts+1 WHERE id=$1", rec["id"]
        )
        raise HTTPException(400, "Incorrect OTP. Please try again.")
    await db.execute("UPDATE otp_requests SET consumed=true WHERE id=$1", rec["id"])

    user = await db.fetch_one("SELECT * FROM users WHERE phone=$1", phone)
    is_new = user is None
    if not user:
        uid = new_id()
        await db.execute(
            """INSERT INTO users (id, phone, name, email, theme_preference, disabled, created_at, last_login_at)
               VALUES ($1,$2,$3,'','system',false,$4,$4)""",
            uid, phone, (body.name or "").strip(), iso_now()
        )
        await db.execute(
            "INSERT INTO reward_accounts (id, user_id, balance, earned, used, expired) VALUES ($1,$2,0,0,0,0)",
            new_id(), uid
        )
        user = await db.fetch_one("SELECT * FROM users WHERE id=$1", uid)
    else:
        if user.get("disabled"):
            raise HTTPException(403, "Your account has been disabled. Contact support.")
        upd_fields = ["last_login_at=$1"]
        upd_vals = [iso_now(), user["id"]]
        if body.name and not user.get("name"):
            upd_fields.append(f"name=${len(upd_vals)+1}")
            upd_vals.insert(-1, body.name.strip())
        await db.execute(
            f"UPDATE users SET {', '.join(upd_fields)} WHERE id=${len(upd_vals)}",
            *upd_vals
        )
        user = await db.fetch_one("SELECT * FROM users WHERE id=$1", user["id"])

    guest = request.headers.get("X-Guest-Id")
    if guest:
        await merge_carts(f"guest:{guest}", f"user:{user['id']}")

    token = create_token(user["id"], "user", days=30)
    response.set_cookie(USER_COOKIE, token, httponly=True, secure=True, samesite="none", max_age=30 * 86400, path="/")
    return {"user": public_user(user), "is_new": is_new}


@router.get("/auth/me")
async def auth_me(request: Request):
    user = await require_user(request)
    return {"user": public_user(user)}


@router.put("/auth/me")
async def update_profile(body: ProfileUpdate, request: Request):
    user = await require_user(request)
    fields, vals = [], [user["id"]]
    if body.name is not None:
        vals.insert(-1, body.name.strip())
        fields.append(f"name=${len(vals)}")
    if body.email is not None:
        vals.insert(-1, body.email.strip().lower())
        fields.append(f"email=${len(vals)}")
    if body.theme_preference in ("light", "dark", "system"):
        vals.insert(-1, body.theme_preference)
        fields.append(f"theme_preference=${len(vals)}")
    if fields:
        await db.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE id=$1",
            *vals
        )
    user = await db.fetch_one("SELECT * FROM users WHERE id=$1", user["id"])
    return {"user": public_user(user)}


@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(USER_COOKIE, path="/")
    return {"ok": True}


# ---------- Google OAuth ----------

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

    user = await db.fetch_one("SELECT * FROM users WHERE email=$1", email)
    is_new = user is None
    if not user:
        uid = new_id()
        await db.execute(
            """INSERT INTO users (id, name, email, picture, auth_provider, theme_preference, disabled, created_at, last_login_at)
               VALUES ($1,$2,$3,$4,'google','system',false,$5,$5)""",
            uid, data.get("name", ""), email, data.get("picture", ""), iso_now()
        )
        await db.execute(
            "INSERT INTO reward_accounts (id, user_id, balance, earned, used, expired) VALUES ($1,$2,0,0,0,0)",
            new_id(), uid
        )
        user = await db.fetch_one("SELECT * FROM users WHERE id=$1", uid)
    else:
        if user.get("disabled"):
            raise HTTPException(403, "Your account has been disabled. Contact support.")
        await db.execute("UPDATE users SET last_login_at=$1 WHERE id=$2", iso_now(), user["id"])

    await db.execute(
        "INSERT INTO user_sessions (id, user_id, provider, session_token, created_at) VALUES ($1,$2,'google',$3,$4)",
        new_id(), user["id"], data.get("session_token", ""), iso_now()
    )
    guest = request.headers.get("X-Guest-Id")
    if guest:
        await merge_carts(f"guest:{guest}", f"user:{user['id']}")
    token = create_token(user["id"], "user", days=30)
    response.set_cookie(USER_COOKIE, token, httponly=True, secure=True, samesite="none", max_age=30 * 86400, path="/")
    return {"user": public_user(user), "is_new": is_new}


# ---------- Admin auth ----------

@router.post("/admin/auth/login")
async def admin_login(body: AdminLoginBody, request: Request, response: Response):
    email = body.email.strip().lower()
    ident = f"{request.client.host if request.client else 'unknown'}:{email}"
    att = await db.fetch_one("SELECT * FROM login_attempts WHERE id=$1", ident)
    if att and (att.get("count") or 0) >= 5 and (att.get("locked_until") or "") > iso_now():
        raise HTTPException(429, "Too many failed attempts. Locked for 15 minutes.")
    admin = await db.fetch_one("SELECT * FROM admin_users WHERE email=$1", email)
    ok = admin and admin.get("active", True) and verify_password(body.password, admin.get("password_hash", ""))
    if not ok:
        count = ((att.get("count") or 0) if att else 0) + 1
        locked = (utcnow() + timedelta(minutes=15)).isoformat() if count >= 5 else None
        await db.execute(
            """INSERT INTO login_attempts (id, count, locked_until, updated_at) VALUES ($1,$2,$3,$4)
               ON CONFLICT (id) DO UPDATE SET count=$2, locked_until=$3, updated_at=$4""",
            ident, count, locked, iso_now()
        )
        raise HTTPException(401, "Invalid email or password")
    await db.execute("DELETE FROM login_attempts WHERE id=$1", ident)
    token = create_token(admin["id"], "admin", hours=12)
    response.set_cookie(ADMIN_COOKIE, token, httponly=True, secure=True, samesite="none", max_age=12 * 3600, path="/")
    return {"admin": {
        "id": admin["id"], "email": admin["email"], "name": admin.get("name", "Admin"),
        "role": admin.get("role", "super_admin"),
        "theme_preference": admin.get("theme_preference", "system"),
    }}


@router.get("/admin/auth/me")
async def admin_me(request: Request):
    admin = await require_admin(request)
    return {"admin": admin}


@router.put("/admin/auth/me")
async def admin_update(body: AdminProfileUpdate, request: Request):
    admin = await require_admin(request)
    fields, vals = [], [admin["id"]]
    if body.name is not None:
        vals.insert(-1, body.name.strip())
        fields.append(f"name=${len(vals)}")
    if body.theme_preference in ("light", "dark", "system"):
        vals.insert(-1, body.theme_preference)
        fields.append(f"theme_preference=${len(vals)}")
    if fields:
        await db.execute(f"UPDATE admin_users SET {', '.join(fields)} WHERE id=$1", *vals)
    return {"ok": True}


@router.post("/admin/auth/logout")
async def admin_logout(response: Response):
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return {"ok": True}
