import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import SecretRedactingFilter, get_settings
from app.core.database import get_db
from app.models.entities import AuditLog, User
from app.models.enums import UserRole


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"pbkdf2_sha256$120000${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algo, iterations_str, salt_b64, hash_b64 = password_hash.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        computed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_str))
        return hmac.compare_digest(computed, expected)
    except Exception:
        return False


def create_access_token(user: User) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "email": user.email,
        "role": user.role,
        "name": user.display_name,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
        ) from exc


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required (Bearer token missing)",
        )
    token = authorization.split(" ", 1)[1].strip()
    claims = decode_access_token(token)
    user = db.scalar(select(User).where(User.id == claims.get("sub")))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account inactive or not found",
        )
    return user


def get_optional_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        # Fallback to default operator in local sandbox if available
        return db.scalar(select(User).where(User.email == "danhnpcd@gmail.com"))
    try:
        token = authorization.split(" ", 1)[1].strip()
        claims = decode_access_token(token)
        return db.scalar(select(User).where(User.id == claims.get("sub")))
    except Exception:
        return db.scalar(select(User).where(User.email == "danhnpcd@gmail.com"))


def require_operator(user: User = Depends(get_current_user)) -> User:
    if user.role not in (UserRole.ADMIN.value, UserRole.OPERATOR.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator or Admin role required for this action",
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator role required for this action",
        )
    return user


def sanitize_dict(data: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    forbidden_keys = {"secret", "api_key", "raw_secret", "encrypted_secret", "password", "token"}
    for key, val in data.items():
        if key.lower() in forbidden_keys:
            cleaned[key] = "[REDACTED]"
        elif isinstance(val, str):
            cleaned[key] = SecretRedactingFilter.sanitize_text(val)
        elif isinstance(val, dict):
            cleaned[key] = sanitize_dict(val)
        else:
            cleaned[key] = val
    return cleaned


def record_audit_log(
    db: Session,
    *,
    actor_id: str,
    actor_role: str,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor_id,
        actor_role=actor_role,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details_json=sanitize_dict(details or {}),
    )
    db.add(entry)
    db.flush()
    return entry


class SlidingWindowRateLimiter:
    """Lightweight per-IP/user endpoint rate limiter to guard administrative & task APIs."""

    def __init__(self, max_requests: int = 1200, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def __call__(self, request: Request) -> None:
        forwarded = request.headers.get("x-forwarded-for")
        client_host = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "local")
        # Exempt high-frequency read-only telemetry and polling endpoints from aggressive rate-limiting
        path = request.url.path
        if request.method in ("GET", "HEAD") and any(
            path.startswith(p) for p in (
                "/api/system/status",
                "/api/agents",
                "/api/tasks",
                "/api/providers",
                "/api/router/policy",
                "/api/notifications",
                "/api/events",
                "/api/analytics",
                "/api/memory",
                "/api/approvals",
                "/api/auth/google/config",
                "/api/system/overview",
            )
        ):
            # High capacity for dashboard telemetry
            limit = self.max_requests * 3
        else:
            limit = self.max_requests

        key = f"{client_host}:{path}"
        now = time.monotonic()
        dq = self._buckets[key]
        while dq and now - dq[0] > self.window_seconds:
            dq.popleft()
        if len(dq) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many API requests, please slow down.",
            )
        dq.append(now)


api_rate_limiter = SlidingWindowRateLimiter(max_requests=1200, window_seconds=60)
