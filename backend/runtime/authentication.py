"""One owner, two explicit authentication modes. WebAuthn verification stays here."""
from datetime import timedelta
import hashlib
import ipaddress
import json
import secrets
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.auth.backends import BaseBackend
from django.db import transaction
from django.utils import timezone
from webauthn import (generate_registration_options, generate_authentication_options,
    verify_registration_response, verify_authentication_response, options_to_json, base64url_to_bytes)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, ResidentKeyRequirement,
    UserVerificationRequirement, PublicKeyCredentialDescriptor)

from library.errors import ApiError
from .models import PasskeyCredential, PasskeySetupToken, PasskeyChallenge, FileKeyCredential


class PublicKeyBackend(BaseBackend):
    # No authenticate(password=...) implementation: normal mode cannot accept passwords.
    def get_user(self, user_id):
        user = get_user_model().objects.filter(pk=user_id, is_active=True, username=settings.BOOKMARK_OWNER).first()
        return user


def auth_mode():
    return "debug" if settings.BOOKMARK_AUTH_DEBUG else "public_key"


def normalize_origin(value):
    parsed = urlsplit(value)
    host = parsed.hostname
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Origin must contain only scheme, host and optional port")
    local = host == "localhost"
    try:
        local = local or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("Passkey requires HTTPS, except on localhost")
    # Accessing port also validates malformed/out-of-range ports.
    port = parsed.port
    authority = f"[{host}]" if ":" in host else host
    if port and port != (443 if parsed.scheme == "https" else 80):
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}", host


def request_origin(request):
    try:
        actual, rp_id = normalize_origin(f"{request.scheme}://{request.get_host()}")
        configured = settings.BOOKMARK_PASSKEY_ORIGIN
        if configured:
            if normalize_origin(configured)[0] != actual:
                raise ValueError("Unexpected origin")
        elif rp_id not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Configure BOOKMARK_PASSKEY_ORIGIN for a public deployment")
    except ValueError:
        raise ApiError("PASSKEY_ORIGIN", "请检查 Passkey 访问地址与 HTTPS 配置", 400) from None
    if request.headers.get("Origin") != actual:
        raise ApiError("PASSKEY_ORIGIN", "Passkey 请求来源不匹配", 403)
    return actual, rp_id


def issue_setup_token(origin, name="Passkey"):
    origin, _ = normalize_origin(origin)
    if settings.BOOKMARK_PASSKEY_ORIGIN and normalize_origin(settings.BOOKMARK_PASSKEY_ORIGIN)[0] != origin:
        raise ValueError("Origin differs from BOOKMARK_PASSKEY_ORIGIN")
    user = get_user_model().objects.get(username=settings.BOOKMARK_OWNER, is_active=True)
    token = secrets.token_urlsafe(32)
    PasskeySetupToken.objects.filter(expires_at__lte=timezone.now()).delete()
    PasskeySetupToken.objects.create(user=user, token_hash=hashlib.sha256(token.encode()).hexdigest(),
        origin=origin, name=name[:100], expires_at=timezone.now() + timedelta(minutes=10))
    return token


def _session_hash(request):
    if not request.session.session_key:
        request.session.create()
    return hashlib.sha256(request.session.session_key.encode()).hexdigest()


def can_add_passkey(request):
    return (request.user.is_authenticated and request.user.username == settings.BOOKMARK_OWNER
        and request.session.get("_auth_user_backend") == "runtime.authentication.PublicKeyBackend"
        and request.session.get("auth_mode") in ("file_key", "passkey"))


def passkey_inventory(request):
    return {"can_add": can_add_passkey(request), "items": [
        {"id": str(item.id), "name": item.name, "rp_id": item.rp_id,
         "created_at": item.created_at.isoformat(),
         "last_used_at": item.last_used_at.isoformat() if item.last_used_at else None}
        for item in PasskeyCredential.objects.filter(user=request.user).order_by("created_at")]}


def _options(request, purpose, data):
    origin, rp_id = request_origin(request)
    user = get_user_model().objects.filter(username=settings.BOOKMARK_OWNER, is_active=True).first()
    if user is None:
        raise ApiError("OWNER_NOT_INITIALIZED", "请先在部署电脑运行 initialize", 409)
    credentials = list(PasskeyCredential.objects.filter(user=user, rp_id=rp_id))
    descriptors = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(item.credential_id)) for item in credentials]
    setup = None
    if purpose == "register":
        if not can_add_passkey(request):
            token = data.get("setup_token")
            if not isinstance(token, str) or not 32 <= len(token) <= 100:
                raise ApiError("PASSKEY_SETUP_REQUIRED", "请先使用私钥文件或 Passkey 登录，再在设置中添加", 403)
            setup = PasskeySetupToken.objects.filter(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                origin=origin, user=user, expires_at__gt=timezone.now()).first()
            if not setup:
                raise ApiError("PASSKEY_SETUP_REQUIRED", "绑定链接无效或已过期，请在部署电脑重新生成", 403)
        options = generate_registration_options(rp_id=rp_id, rp_name="网站收藏", user_name=settings.BOOKMARK_OWNER,
            user_id=f"website-owner-{user.pk}".encode(), user_display_name="我的收藏库", exclude_credentials=descriptors,
            authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED), timeout=120000)
    else:
        if not credentials:
            raise ApiError("PASSKEY_NOT_CONFIGURED", "请先使用私钥文件登录，再在设置中添加 Passkey", 409)
        options = generate_authentication_options(rp_id=rp_id, allow_credentials=descriptors,
            user_verification=UserVerificationRequirement.REQUIRED, timeout=120000)
    now = timezone.now()
    session_hash = _session_hash(request)
    PasskeyChallenge.objects.filter(expires_at__lte=now).delete()
    PasskeyChallenge.objects.filter(session_hash=session_hash).delete()
    challenge = PasskeyChallenge.objects.create(session_hash=session_hash, purpose=purpose, origin=origin,
        rp_id=rp_id, setup=setup, challenge=bytes_to_base64url(options.challenge), expires_at=now + timedelta(minutes=3))
    return {"options": json.loads(options_to_json(options)), "challenge_id": str(challenge.id)}


def _consume_challenge(request, purpose, data):
    origin, _ = request_origin(request)
    # Commit consumption separately from signature verification, including failures.
    with transaction.atomic(using="default"):
        challenge = PasskeyChallenge.objects.filter(pk=data.get("challenge_id"), purpose=purpose,
            session_hash=_session_hash(request), origin=origin, expires_at__gt=timezone.now()).first()
        if not challenge:
            raise ApiError("PASSKEY_CHALLENGE", "认证请求已过期，请重新操作", 400)
        challenge.delete()
    credential = data.get("credential")
    if not isinstance(credential, dict):
        raise ApiError("PASSKEY_INVALID", "Passkey 认证数据无效", 400)
    try:
        client = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
        if client.get("crossOrigin", False) is not False:
            raise ValueError("Cross-origin ceremony")
    except (ValueError, KeyError, TypeError):
        raise ApiError("PASSKEY_INVALID", "Passkey 认证数据无效", 400) from None
    return challenge, credential


def passkey_request(request, action, data):
    if action in ("register/options", "login/options"):
        return _options(request, "register" if action.startswith("register/") else "login", data)
    if action not in ("register/verify", "login/verify"):
        raise ApiError("NOT_FOUND", "操作不存在", 404)
    purpose = "register" if action.startswith("register/") else "login"
    challenge, credential = _consume_challenge(request, purpose, data)
    try:
        if purpose == "register":
            verification = verify_registration_response(credential=credential,
                expected_challenge=base64url_to_bytes(challenge.challenge), expected_rp_id=challenge.rp_id,
                expected_origin=challenge.origin, require_user_verification=True)
            with transaction.atomic(using="default"):
                if challenge.setup_id:
                    setup = PasskeySetupToken.objects.filter(pk=challenge.setup_id, expires_at__gt=timezone.now()).first()
                    if not setup:
                        raise ApiError("PASSKEY_SETUP_REQUIRED", "绑定链接已使用或已过期", 403)
                    user, name = setup.user, setup.name
                elif can_add_passkey(request):
                    setup = None
                    user, name = request.user, data.get("name", "Passkey")
                    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
                        raise ApiError("INVALID_NAME", "请输入 1–100 字的 Passkey 名称", 400)
                    name = name.strip()
                else:
                    raise ApiError("PASSKEY_SETUP_REQUIRED", "请重新使用私钥文件或 Passkey 登录", 403)
                PasskeyCredential.objects.create(user=user, credential_id=bytes_to_base64url(verification.credential_id),
                    public_key=verification.credential_public_key, sign_count=verification.sign_count,
                    rp_id=challenge.rp_id, name=name,
                    transports=[item for item in credential.get("response", {}).get("transports", [])
                        if item in ("usb", "nfc", "ble", "internal", "hybrid", "smart-card")])
                if setup:
                    setup.delete()
        else:
            with transaction.atomic(using="default"):
                stored = PasskeyCredential.objects.select_related("user").filter(credential_id=credential.get("id"),
                    rp_id=challenge.rp_id, user__username=settings.BOOKMARK_OWNER, user__is_active=True).first()
                if not stored:
                    raise ApiError("PASSKEY_INVALID", "此 Passkey 不属于当前收藏库", 401)
                user_handle = credential.get("response", {}).get("userHandle")
                if user_handle is not None and base64url_to_bytes(user_handle) != f"website-owner-{stored.user_id}".encode():
                    raise ValueError("Wrong user handle")
                verification = verify_authentication_response(credential=credential,
                    expected_challenge=base64url_to_bytes(challenge.challenge), expected_rp_id=challenge.rp_id,
                    expected_origin=challenge.origin, credential_public_key=bytes(stored.public_key),
                    credential_current_sign_count=stored.sign_count, require_user_verification=True)
                stored.sign_count, stored.last_used_at = verification.new_sign_count, timezone.now()
                stored.save(update_fields=["sign_count", "last_used_at"])
                user = stored.user
    except ApiError:
        raise
    except Exception:
        # Library exceptions may include credential contents; never echo/log them.
        raise ApiError("PASSKEY_INVALID", "Passkey 验证未通过，请重新操作", 400) from None
    login(request, user, backend="runtime.authentication.PublicKeyBackend")
    request.session["auth_mode"] = "passkey"
    return {"authenticated": True}


def file_key_request(request, action, data):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    origin, rp_id = request_origin(request)
    if action == "options":
        if not FileKeyCredential.objects.filter(user__username=settings.BOOKMARK_OWNER, user__is_active=True).exists():
            raise ApiError("KEY_NOT_CONFIGURED", "请先在部署电脑配置公钥", 409)
        now = timezone.now()
        session_hash = _session_hash(request)
        PasskeyChallenge.objects.filter(expires_at__lte=now).delete()
        PasskeyChallenge.objects.filter(session_hash=session_hash).delete()
        challenge = PasskeyChallenge.objects.create(session_hash=session_hash, purpose="file_key", origin=origin,
            rp_id=rp_id, challenge=secrets.token_urlsafe(32), expires_at=now + timedelta(minutes=3))
        message = f"website-file-key-login-v1\n{origin}\n{challenge.id}\n{challenge.challenge}"
        return {"challenge_id": str(challenge.id), "message": message}
    if action != "verify":
        raise ApiError("NOT_FOUND", "操作不存在", 404)
    with transaction.atomic(using="default"):
        challenge = PasskeyChallenge.objects.filter(pk=data.get("challenge_id"), purpose="file_key",
            session_hash=_session_hash(request), origin=origin, expires_at__gt=timezone.now()).first()
        if not challenge:
            raise ApiError("KEY_CHALLENGE", "认证请求已过期，请重新操作", 400)
        message = f"website-file-key-login-v1\n{origin}\n{challenge.id}\n{challenge.challenge}".encode()
        challenge.delete()
    try:
        encoded = data["signature"]
        if not isinstance(encoded, str) or len(encoded) > 100:
            raise ValueError("Invalid signature")
        signature = base64url_to_bytes(encoded)
        if len(signature) != 64:
            raise ValueError("Invalid signature")
    except (ValueError, KeyError, TypeError):
        raise ApiError("KEY_INVALID", "私钥签名无效", 400) from None
    # Only registered owner keys may authenticate. The private key never leaves
    # the browser; no public key supplied in this request is trusted.
    matched = None
    with transaction.atomic(using="default"):
        for stored in FileKeyCredential.objects.select_related("user").filter(
                user__username=settings.BOOKMARK_OWNER, user__is_active=True):
            try:
                Ed25519PublicKey.from_public_bytes(bytes(stored.public_key)).verify(signature, message)
            except InvalidSignature:
                continue
            matched = stored
            break
        if not matched:
            raise ApiError("KEY_INVALID", "此私钥不属于当前收藏库", 401)
        matched.last_used_at = timezone.now()
        matched.save(update_fields=["last_used_at"])
    login(request, matched.user, backend="runtime.authentication.PublicKeyBackend")
    request.session["auth_mode"] = "file_key"
    return {"authenticated": True}
