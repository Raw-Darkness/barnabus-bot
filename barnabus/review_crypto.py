"""Encrypt bug-review exports for a separate workstation-held private key.

Only the public key belongs on the server. Generate and retain the private key on
an encrypted workstation volume; never deploy it with the bot or its backups.
This module deliberately has no imports from the bot, its database, or record.key.
"""

import base64
import binascii
import json
import math
import os
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SCHEMA = 1
ALGORITHM = "RSA-OAEP-SHA256+AES-256-GCM"
MAX_AGE_SECONDS = 30 * 24 * 60 * 60
MAX_CIPHERTEXT_BYTES = 64 * 1024 * 1024 + 16
_FIELDS = frozenset(("schema", "algorithm", "expires_at", "wrapped_key", "nonce", "ciphertext"))


def _header(expires_at: float) -> bytes:
    return json.dumps(
        {"schema": SCHEMA, "algorithm": ALGORITHM, "expires_at": expires_at},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("ascii")


def _expiry(value: object, now: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid review envelope expiry")
    try:
        expiry = float(value)
    except (OverflowError, ValueError):
        raise ValueError("Invalid review envelope expiry") from None
    if not math.isfinite(expiry) or expiry <= now or expiry > now + MAX_AGE_SECONDS:
        raise ValueError("Invalid or expired review envelope")
    return expiry


def _decode(value: object, max_bytes: int) -> bytes:
    if not isinstance(value, str) or len(value) > ((max_bytes + 2) // 3) * 4:
        raise ValueError("Invalid review envelope")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid review envelope") from None
    if len(decoded) > max_bytes:
        raise ValueError("Invalid review envelope")
    return decoded


def _encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _rsa_public(path: Path) -> rsa.RSAPublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 3072:
        raise ValueError("Review public key must be RSA 3072 bits or larger")
    return key


def _rsa_private(path: Path) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072:
        raise ValueError("Review private key must be RSA 3072 bits or larger")
    return key


def seal(payload: dict, public_key_path: Path, expires_at: float) -> dict:
    """Encrypt a JSON report with a fresh AES key; return a portable envelope."""
    if not isinstance(payload, dict):
        raise ValueError("Review payload must be an object")
    expiry = _expiry(expires_at, time.time())
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
    if len(plaintext) + 16 > MAX_CIPHERTEXT_BYTES:
        raise ValueError("Review payload is too large")
    public_key = _rsa_public(public_key_path)
    aes_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    wrapped_key = public_key.encrypt(
        aes_key,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None),
    )
    ciphertext = AESGCM(aes_key).encrypt(nonce, plaintext, _header(expiry))
    return {"schema": SCHEMA, "algorithm": ALGORITHM, "expires_at": expiry,
            "wrapped_key": _encode(wrapped_key), "nonce": _encode(nonce),
            "ciphertext": _encode(ciphertext)}


def unseal(envelope: dict, private_key_path: Path, now: float | None = None) -> dict:
    """Decrypt an envelope, rejecting tampering and expiration as ValueError."""
    if not isinstance(envelope, dict) or set(envelope) != _FIELDS:
        raise ValueError("Invalid review envelope")
    if type(envelope["schema"]) is not int or envelope["schema"] != SCHEMA:
        raise ValueError("Invalid review envelope header")
    if envelope["algorithm"] != ALGORITHM:
        raise ValueError("Invalid review envelope header")
    instant = time.time() if now is None else now
    if isinstance(instant, bool) or not isinstance(instant, (int, float)):
        raise ValueError("Invalid review time")
    try:
        instant = float(instant)
    except (OverflowError, ValueError):
        raise ValueError("Invalid review time") from None
    if not math.isfinite(instant):
        raise ValueError("Invalid review time")
    expiry = _expiry(envelope["expires_at"], instant)
    wrapped_key = _decode(envelope["wrapped_key"], 1024)
    nonce = _decode(envelope["nonce"], 12)
    ciphertext = _decode(envelope["ciphertext"], MAX_CIPHERTEXT_BYTES)
    if len(nonce) != 12 or len(ciphertext) < 16:
        raise ValueError("Invalid review envelope")
    try:
        private_key = _rsa_private(private_key_path)
        aes_key = private_key.decrypt(
            wrapped_key,
            padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                         algorithm=hashes.SHA256(), label=None),
        )
        if len(aes_key) != 32:
            raise ValueError("Invalid AES key")
        plaintext = AESGCM(aes_key).decrypt(nonce, ciphertext, _header(expiry))
        payload = json.loads(plaintext)
    except (OSError, TypeError, ValueError, InvalidTag) as exc:
        raise ValueError("Invalid review envelope or key") from None
    if not isinstance(payload, dict):
        raise ValueError("Invalid review payload")
    return payload


def generate_keypair(private_path: Path, public_path: Path) -> None:
    """Create RSA-3072 PEM files without replacing existing files.

    The caller must place the private key on an encrypted workstation volume.
    POSIX private file mode is 0600; on Windows, also restrict the volume ACL.
    """
    private_path, public_path = Path(private_path), Path(public_path)
    if private_path == public_path or private_path.exists() or public_path.exists():
        raise FileExistsError("Review key path already exists")
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    created = []
    try:
        for path, content, mode in ((private_path, private_pem, 0o600),
                                    (public_path, public_pem, 0o644)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
            created.append(path)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            os.chmod(path, mode)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
