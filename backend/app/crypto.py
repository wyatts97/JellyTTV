"""Secret handling.

Credentials (Twitch client secret, Jellyfin API key, EventSub secret) are
encrypted at rest with a Fernet key stored beside the database, so a leaked
`jellyttv.db` alone is not enough to impersonate the user's accounts.
"""

from __future__ import annotations

import os
import secrets
import stat

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_config
from app.logging_conf import get_logger

log = get_logger(__name__)

_fernet: Fernet | None = None
_hasher = PasswordHasher()


def _load_key() -> bytes:
    cfg = get_config()
    path = cfg.secret_key_path
    if path.exists():
        return path.read_bytes().strip()

    cfg.ensure_dirs()
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - best effort on exotic filesystems
        log.warning("could not tighten permissions on secret key", path=str(path))
    log.info("generated new encryption key", path=str(path))
    return key


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return _cipher().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _cipher().decrypt(value.encode()).decode()
    except InvalidToken:
        log.error("failed to decrypt stored secret - encryption key changed?")
        return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hashed: str | None, password: str) -> bool:
    if not hashed:
        return False
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, Exception):  # noqa: B014 - argon2 raises several types
        return False


def random_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def random_eventsub_secret() -> str:
    """Twitch requires 10-100 characters."""
    return secrets.token_urlsafe(32)[:64]


def random_device_id() -> str:
    """A device id shaped like the one Twitch's own web client sends.

    32 lower-case alphanumerics, generated once per install and then kept. The
    shape matters less than the two properties: it is stable, so this install
    looks like one returning viewer rather than a new one every resolve, and it
    is unique, so it is not the shared literal that ad-block scripts have
    published and Twitch can therefore match on.
    """
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(32))


def generate_vapid_keypair() -> tuple[str, str]:
    """A P-256 key pair for Web Push, as (public, private) base64url strings.

    The public key is the 65-byte uncompressed point browsers expect as
    `applicationServerKey`; the private key is the raw 32-byte scalar, which is
    one of the forms pywebpush accepts directly.
    """
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    private = key.private_numbers().private_value.to_bytes(32, "big")

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return b64(public), b64(private)


def reset_cipher_cache() -> None:
    """Test helper."""
    global _fernet
    _fernet = None
