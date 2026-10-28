import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]
BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT_DIR / ".env"


class SecretRedactingFilter(logging.Filter):
    """Prevents API keys, Bearer tokens, and registered secrets from ever entering logs."""

    SECRET_PATTERNS = [
        re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
        re.compile(r"AIza[A-Za-z0-9_\-]{10,}"),
        re.compile(r"gsk_[A-Za-z0-9_\-]{10,}"),
        re.compile(r"sk-or-v1-[A-Za-z0-9_\-]{10,}"),
        re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    ]
    _explicit_secrets: set[str] = set()

    @classmethod
    def register_secret(cls, secret: str) -> None:
        if secret and len(secret) >= 6:
            cls._explicit_secrets.add(secret)

    @classmethod
    def sanitize_text(cls, text: str) -> str:
        if not text:
            return text
        sanitized = text
        for raw in list(cls._explicit_secrets):
            if raw in sanitized:
                sanitized = sanitized.replace(raw, "[REDACTED_SECRET]")
        for pattern in cls.SECRET_PATTERNS:
            sanitized = pattern.sub("[REDACTED_SECRET]", sanitized)
        return sanitized

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.sanitize_text(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self.sanitize_text(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: self.sanitize_text(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True


def configure_safe_logging() -> None:
    redactor = SecretRedactingFilter()
    root_logger = logging.getLogger()
    if not any(isinstance(f, SecretRedactingFilter) for f in root_logger.filters):
        root_logger.addFilter(redactor)
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "app", "httpx"):
        lg = logging.getLogger(logger_name)
        if not any(isinstance(f, SecretRedactingFilter) for f in lg.filters):
            lg.addFilter(redactor)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE) if ENV_FILE.exists() else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "AI Business Simulator 2.5D"
    APP_VERSION: str = "0.1.0"
    APP_ENV: str = "development"
    DEMO_MODE: bool = True

    DATABASE_URL: str = f"sqlite:///{(BACKEND_DIR / 'ai_business_simulator.db').as_posix()}"

    JWT_SECRET_KEY: str = "dev-only-jwt-secret-replace-in-production-9f8e7d6c5b4a"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60 * 24

    CREDENTIAL_ENCRYPTION_KEY: str = ""
    LOCAL_VAULT_KEY_PATH: str = str(ROOT_DIR / ".local_vault.key")

    BOOTSTRAP_ADMIN_EMAIL: str = "admin@simulator.local"
    BOOTSTRAP_ADMIN_PASSWORD: str = "Admin@123456!"
    PRIMARY_OPERATOR_EMAIL: str = "danhnpcd@gmail.com"
    BOOTSTRAP_OPERATOR_EMAIL: str = "operator@simulator.local"
    BOOTSTRAP_OPERATOR_PASSWORD: str = "Operator@123456!"
    BOOTSTRAP_VIEWER_EMAIL: str = "viewer@simulator.local"
    BOOTSTRAP_VIEWER_PASSWORD: str = "Viewer@123456!"

    # Optional provider keys loaded safely from .env into CredentialVault on startup
    GEMINI_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""

    # Google OAuth 2.0 strictly for USER IDENTITY AUTHENTICATION ONLY
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:5173/auth/google/callback"

    DEFAULT_ROUTING_MODE: str = "FREE_FIRST"
    DAILY_BUDGET_LIMIT_USD: float = 5.0
    MONTHLY_BUDGET_LIMIT_USD: float = 50.0
    APPROVAL_COST_THRESHOLD_USD: float = 1.0
    DEFAULT_AUTOMATION_LEVEL: str = "LEVEL_2_SANDBOX"

    MAX_WORKFLOW_DEPTH: int = 4
    MAX_WORKFLOW_TASKS: int = 12
    DEFAULT_TASK_TIMEOUT_SECONDS: int = 120
    STALLED_TASK_TIMEOUT_SECONDS: int = 300

    WORKSPACES_DIR: str = str(ROOT_DIR / "workspaces")
    DEPLOYMENTS_DIR: str = str(ROOT_DIR / "deployments")
    PAYMENT_WEBHOOK_SECRET: str = "sandbox-webhook-hmac-secret-replace-in-prod"

    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    configure_safe_logging()
    return Settings()
