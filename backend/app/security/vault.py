import base64
import hashlib
import os
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import SecretRedactingFilter, get_settings


class CredentialVault:
    """
    Encrypts/decrypts provider credentials using Fernet (AES-128-CBC + HMAC-SHA256)
    with a key stored strictly outside the database (environment variable or external keyfile).
    """

    def __init__(self, explicit_key: str | None = None):
        self._fernet = Fernet(self._resolve_key(explicit_key))

    @staticmethod
    def _resolve_key(explicit_key: str | None = None) -> bytes:
        settings = get_settings()
        raw_key = explicit_key or os.environ.get("CREDENTIAL_ENCRYPTION_KEY") or settings.CREDENTIAL_ENCRYPTION_KEY
        if raw_key:
            key_bytes = raw_key.strip().encode("utf-8") if isinstance(raw_key, str) else raw_key.strip()
            try:
                Fernet(key_bytes)
                return key_bytes
            except Exception:
                # Deterministically derive a valid 32-byte urlsafe base64 Fernet key from any secret string
                return base64.urlsafe_b64encode(hashlib.sha256(key_bytes).digest())

        key_path = Path(settings.LOCAL_VAULT_KEY_PATH)
        if key_path.exists():
            return key_path.read_bytes().strip()

        generated = Fernet.generate_key()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(generated)
        return generated

    @staticmethod
    def mask_secret(secret: str) -> str:
        clean = (secret or "").strip()
        if not clean:
            return "****"
        SecretRedactingFilter.register_secret(clean)
        if len(clean) <= 8:
            return f"{clean[:2]}****"
        return f"{clean[:4]}...{clean[-4:]}"

    def encrypt(self, plaintext_secret: str) -> tuple[str, str]:
        clean = plaintext_secret.strip()
        if not clean:
            raise ValueError("Secret must not be empty")
        SecretRedactingFilter.register_secret(clean)
        ciphertext = self._fernet.encrypt(clean.encode("utf-8")).decode("utf-8")
        masked = self.mask_secret(clean)
        return ciphertext, masked

    def decrypt(self, encrypted_secret: str) -> str:
        try:
            plaintext = self._fernet.decrypt(encrypted_secret.encode("utf-8")).decode("utf-8")
            SecretRedactingFilter.register_secret(plaintext)
            return plaintext
        except InvalidToken as exc:
            raise ValueError("Unable to decrypt credential with current vault key") from exc


_vault_instance: CredentialVault | None = None


def get_vault() -> CredentialVault:
    global _vault_instance
    if _vault_instance is None:
        _vault_instance = CredentialVault()
    return _vault_instance
