"""Read local key material and register only its public half."""
import hashlib
import os
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from .models import FileKeyCredential


def read_private_key(path):
    path = Path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= 16384:
        raise ValueError("Private key file is missing or too large")
    data = path.read_bytes()
    if not data.lstrip().startswith(b"-----BEGIN PRIVATE KEY-----"):
        raise ValueError("Use an unencrypted PKCS8 PEM private key")
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Only Ed25519 private keys are supported")
    return key


def generate_private_key(path):
    key = Ed25519PrivateKey.generate()
    data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    descriptor = os.open(Path(path).absolute(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return key


def register_public_key(user, public, name="私钥文件"):
    if not isinstance(public, Ed25519PublicKey):
        raise ValueError("Only Ed25519 public keys are supported")
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    fingerprint = hashlib.sha256(raw).hexdigest()
    stored, created = FileKeyCredential.objects.get_or_create(fingerprint=fingerprint,
        defaults={"user": user, "public_key": raw, "name": name[:100]})
    if not created and (stored.user_id != user.pk or bytes(stored.public_key) != raw):
        raise ValueError("Existing public key record does not match")
    return stored
