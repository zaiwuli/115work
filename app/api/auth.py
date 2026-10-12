import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from app.persistence.models import Settings


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
    return salt + ":" + digest


def password_matches(password: str, saved: str) -> bool:
    return hmac.compare_digest(password_hash(password, saved.split(":")[0]), saved)


def configured(request: Request) -> bool:
    with request.app.state.db.session() as session:
        return session.get(Settings, "admin_password") is not None


def authenticate(request: Request):
    if request.app.state.config.local_no_auth:
        origin = request.headers.get("origin")
        if (origin and origin != str(request.base_url).rstrip("/")) or request.headers.get(
            "sec-fetch-site"
        ) == "cross-site":
            raise HTTPException(403, detail="CROSS_SITE_REQUEST_REJECTED")
        request.state.actor = "local-admin"
        request.state.role = "admin"
        return "admin"
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        if hmac.compare_digest(authorization[7:], request.app.state.api_token):
            request.state.actor = "admin-token"
            request.state.role = "admin"
            return "admin"
        key = "access-token:" + hashlib.sha256(authorization[7:].encode()).hexdigest()
        with request.app.state.db.session() as db:
            saved = db.get(Settings, key)
            if not saved or saved.value.get("expires", 0) < time.time():
                raise HTTPException(401, detail="INVALID_TOKEN")
            role = saved.value["role"]
            request.state.role = role
            request.state.actor = saved.value.get("name", role)
            request.state.token_key = key
            enforce_role(request, role)
            return role
    token = request.cookies.get("work115_session", "")
    session_key = "session:" + hashlib.sha256(token.encode()).hexdigest()
    with request.app.state.db.session() as db:
        saved = db.get(Settings, session_key)
        if not saved or saved.value["expires"] < time.time():
            raise HTTPException(401, detail="LOGIN_REQUIRED")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), saved.value["csrf"]):
                raise HTTPException(403, detail="CSRF_REQUIRED")
        role = saved.value.get("role", "admin")
        request.state.role = role
        request.state.actor = saved.value.get("name", role)
        enforce_role(request, role)
    return role


def enforce_role(request, role):
    if role == "readonly":
        if request.method not in {"GET", "HEAD", "OPTIONS"} or request.url.path in {
            "/api/v1/offline", "/api/115/offline",
        } or request.url.path.startswith(("/api/v1/security", "/api/v1/offline/")):
            raise HTTPException(403, detail="READ_ONLY_PERMISSION")
        if request.url.path in {"/api/settings", "/api/v1/settings", "/api/v1/connections"}:
            raise HTTPException(403, detail="ADMIN_CONFIGURATION_PERMISSION")
    elif role == "import":
        if request.method == "POST" and request.url.path in {
            "/api/115/offline/import", "/api/v1/offline/import",
        }:
            return
        if request.method == "GET" and request.url.path.startswith("/api/v1/import/batches/"):
            return
        raise HTTPException(403, detail="IMPORT_ONLY_PERMISSION")
    elif role != "admin":
        raise HTTPException(403, detail="UNKNOWN_ROLE")
