import base64
import hashlib
import hmac
import os
import secrets
import time

import db

SESSION_COOKIE = "pf_session"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 días
PBKDF2_ITERATIONS = 200_000

_SECRET_PATH = db.DATA_DIR / "secret.key"


def _load_or_create_secret() -> bytes:
    env_secret = os.environ.get("SESSION_SECRET")
    if env_secret:
        return env_secret.encode("utf-8")
    if _SECRET_PATH.exists():
        return _SECRET_PATH.read_bytes()
    secret = secrets.token_bytes(32)
    _SECRET_PATH.write_bytes(secret)
    return secret


_SECRET = _load_or_create_secret()


def hash_password(password: str, salt: str = None) -> tuple:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS)
    return base64.b64encode(digest).decode("ascii"), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, password_hash)


def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(username: str) -> str:
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{username}:{expires_at}"
    signature = _sign(payload)
    raw = f"{payload}:{signature}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def verify_session_token(token: str):
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        username, expires_at, signature = raw.rsplit(":", 2)
    except Exception:
        return None

    payload = f"{username}:{expires_at}"
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    if int(expires_at) < int(time.time()):
        return None
    return username
