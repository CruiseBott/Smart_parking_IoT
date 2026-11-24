import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from bson import ObjectId
from fastapi import Depends, Header, HTTPException

import database as d

JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me")


def hash_pw(password: str, salt: str | None = None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return salt, h


def verify_pw(password: str, salt: str, expected: str) -> bool:
    return hmac.compare_digest(hash_pw(password, salt)[1], expected)


def make_token(user: dict) -> str:
    exp = datetime.now(timezone.utc) + timedelta(days=7)
    return jwt.encode({"sub": str(user["_id"]), "role": user["role"], "exp": exp}, JWT_SECRET, "HS256")


def public_user(u: dict) -> dict:
    return {"id": str(u["_id"]), "name": u["name"], "email": u["email"],
            "phone": u.get("phone"), "role": u["role"]}


async def current_user(authorization: str = Header(default="")) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Please log in")
    try:
        payload = jwt.decode(authorization[7:], JWT_SECRET, algorithms=["HS256"])
        user = await d.users.find_one({"_id": ObjectId(payload["sub"])})
    except Exception:
        raise HTTPException(401, "Session expired. Please log in again")
    if not user or not user.get("active", True):
        raise HTTPException(401, "Account not available")
    return user


async def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return user
