"""Owner authentication and WebAuthn ceremonies; grants and sessions own their lifetimes."""
from datetime import timedelta
import hashlib
import ipaddress
import json
import secrets
import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.backends import BaseBackend
from django.contrib.sessions.models import Session
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from webauthn import (generate_registration_options, generate_authentication_options,
    verify_registration_response, verify_authentication_response, options_to_json, base64url_to_bytes)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, ResidentKeyRequirement,
    UserVerificationRequirement, PublicKeyCredentialDescriptor, AuthenticatorTransport)

from library.errors import ApiError
from .auth_grants import (authorize_registration, credential_exists, get_auth_state,
    issue_device_grant, owner_exists, validate_grant)
from .auth_sessions import establish_session, require_credential_session, validate_session
from .models import EnrollmentGrant, PasskeyCredential, PasskeyChallenge, FileKeyCredential


class PublicKeyBackend(BaseBackend):
    # No password implementation: normal mode only authenticates registered public keys.
    def get_user(self, user_id):
        return get_user_model().objects.filter(pk=user_id, is_active=True, username=settings.BOOKMARK_OWNER).first()


def auth_mode():
    return "debug" if settings.BOOKMARK_AUTH_DEBUG else "public_key"


def normalize_origin(value, *, allow_ip=False):
    parsed = urlsplit(value)
    host = parsed.hostname
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Origin must contain only scheme, host and optional port")
    local = host == "localhost"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not allow_ip:
            raise ValueError("Passkey RP ID must be a domain; use localhost for local development")
        local = address.is_loopback
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("Passkey requires HTTPS, except on localhost")
    port = parsed.port
    authority = f"[{host}]" if ":" in host else host
    if port and port != (443 if parsed.scheme == "https" else 80):
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}", host


def request_origin(request, *, allow_ip=False):
    try:
        actual, rp_id = normalize_origin(f"{request.scheme}://{request.get_host()}", allow_ip=allow_ip)
        configured = settings.BOOKMARK_PASSKEY_ORIGIN
        if configured:
            canonical = normalize_origin(configured, allow_ip=allow_ip)[0]
            expected, received = urlsplit(canonical), urlsplit(actual)
            local_alias = (allow_ip and expected.hostname in ("localhost", "127.0.0.1", "::1")
                and received.hostname in ("localhost", "127.0.0.1", "::1")
                and expected.scheme == received.scheme and expected.port == received.port)
            if canonical != actual and not local_alias:
                raise ValueError("Unexpected origin")
        elif rp_id not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Configure BOOKMARK_PASSKEY_ORIGIN for a public deployment")
    except ValueError:
        raise ApiError("PASSKEY_ORIGIN", "请使用配置的 HTTPS 地址；本机请使用 localhost", 400) from None
    if request.headers.get("Origin") != actual:
        raise ApiError("PASSKEY_ORIGIN", "Passkey 请求来源不匹配", 403)
    return actual, rp_id


def passkey_availability(request):
    try:
        actual = f"{request.scheme}://{request.get_host()}"
        configured = settings.BOOKMARK_PASSKEY_ORIGIN
        if configured:
            origin, _ = normalize_origin(configured)
        else:
            parsed = urlsplit(actual)
            if parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
                return {"passkey_origin": None, "passkey_available": False}
            port = f":{parsed.port}" if parsed.port else ""
            origin, _ = normalize_origin(f"{request.scheme}://localhost{port}")
        return {"passkey_origin": origin, "passkey_available": normalize_origin(actual, allow_ip=True)[0] == origin}
    except ValueError:
        return {"passkey_origin": None, "passkey_available": False}


def _session_hash(request):
    if not request.session.session_key:
        request.session.create()
    return hashlib.sha256(request.session.session_key.encode()).hexdigest()


def can_add_passkey(request):
    return validate_session(request, clear_invalid=False) and request.session.get("auth_mode") in ("file_key", "passkey")


def _name(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100:
        raise ApiError("INVALID_NAME", "请输入 1–100 字的 Passkey 名称", 400)
    return value.strip()


def _id(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ApiError("INVALID_ID", "凭据或认证请求不存在", 400) from None


def _usable_rp(request):
    configured = settings.BOOKMARK_PASSKEY_ORIGIN
    try:
        return normalize_origin(configured or f"{request.scheme}://{request.get_host()}")[1]
    except ValueError:
        return None


def _other_login_exists(request, exclude=None):
    keys = PasskeyCredential.objects.filter(user=request.user, rp_id=_usable_rp(request))
    if exclude:
        keys = keys.exclude(pk=exclude)
    return keys.exists() or FileKeyCredential.objects.filter(user=request.user).exists()


def _credential_item(request, item):
    return {"id": str(item.id), "name": item.name, "rp_id": item.rp_id,
        "created_at": item.created_at.isoformat(),
        "last_used_at": item.last_used_at.isoformat() if item.last_used_at else None,
        "is_current": request.session.get("auth_mode") == "passkey" and request.session.get("credential_id") == str(item.id),
        "can_delete": can_add_passkey(request) and _other_login_exists(request, item.id)}


def passkey_inventory(request):
    return {"can_add": can_add_passkey(request), "items": [
        _credential_item(request, item) for item in PasskeyCredential.objects.filter(user=request.user).order_by("created_at")]}


def rename_passkey(request, credential_id, data):
    name = _name(data.get("name"))
    with transaction.atomic(using="default"):
        require_credential_session(request)
        item = PasskeyCredential.objects.filter(pk=_id(credential_id), user=request.user).first()
        if item is None:
            raise ApiError("NOT_FOUND", "Passkey 不存在", 404)
        item.name = name
        item.save(update_fields=["name"])
        return _credential_item(request, item)


def delete_passkey(request, credential_id):
    with transaction.atomic(using="default"):
        kind, current_id, _ = require_credential_session(request)
        item = PasskeyCredential.objects.filter(pk=_id(credential_id), user=request.user).first()
        if item is None:
            raise ApiError("NOT_FOUND", "Passkey 不存在", 404)
        if not _other_login_exists(request, item.id):
            raise ApiError("AUTH_LAST_CREDENTIAL", "不能删除最后一种可用登录凭据", 409)
        logged_out = kind == "passkey" and current_id == item.id
        EnrollmentGrant.objects.filter(issuer_kind="passkey", issuer_credential_id=item.id,
            consumed_at__isnull=True).update(invalidated_at=timezone.now())
        item.delete()
    if logged_out:
        logout(request)
    return {"deleted": True, "logged_out": logged_out}


def manage_passkey(request, credential_id, method, data):
    if method == "PATCH":
        return rename_passkey(request, credential_id, data)
    if method == "DELETE":
        return delete_passkey(request, credential_id)
    raise ApiError("METHOD_NOT_ALLOWED", "请求方法不受支持", 405)


def _cleanup_challenges():
    now = timezone.now()
    PasskeyChallenge.objects.filter(Q(completed_at__lt=now - timedelta(minutes=10)) |
        (Q(completed_at__isnull=True) & Q(expires_at__lte=now))).delete()


def _options(request, purpose, data):
    origin, rp_id = request_origin(request)
    grant = authorize_registration(data, origin) if purpose == "register" else None
    mode, name, issuer_kind, issuer_id = "", "Passkey", "", None
    session_hash = _session_hash(request)
    with transaction.atomic(using="default"):
        state = get_auth_state()
        user = get_user_model().objects.filter(username=settings.BOOKMARK_OWNER, is_active=True).first()
        credentials = list(PasskeyCredential.objects.filter(user=user, rp_id=rp_id)) if user else []
        descriptors = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(item.credential_id),
            transports=[AuthenticatorTransport(value) for value in item.transports] or None)
            for item in credentials]
        if purpose == "register":
            name = _name(data.get("name", grant.name if grant else "Passkey"))
            if grant:
                grant = EnrollmentGrant.objects.get(pk=grant.pk)
                validate_grant(grant, origin, state.credential_epoch)
                mode = grant.purpose
            else:
                if not can_add_passkey(request):
                    raise ApiError("PASSKEY_SETUP_REQUIRED", "请输入终端初始化码，或先登录再添加 Passkey", 403)
                issuer_kind, issuer_id, _ = require_credential_session(request)
                mode = "local"
            options = generate_registration_options(rp_id=rp_id, rp_name="Linkarium", user_name=settings.BOOKMARK_OWNER,
                user_id=bytes(state.webauthn_user_handle), user_display_name="我的收藏库", exclude_credentials=descriptors,
                authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=UserVerificationRequirement.REQUIRED), timeout=120000)
        else:
            if not credentials:
                raise ApiError("PASSKEY_NOT_CONFIGURED", "尚无可用 Passkey，请使用其他登录方式或终端授权", 409)
            options = generate_authentication_options(rp_id=rp_id, allow_credentials=descriptors,
                user_verification=UserVerificationRequirement.REQUIRED, timeout=120000)
        _cleanup_challenges()
        expires = timezone.now() + timedelta(minutes=3)
        if grant and grant.expires_at is not None:
            expires = min(expires, grant.expires_at)
        challenge = PasskeyChallenge.objects.create(session_hash=session_hash, purpose=purpose, origin=origin,
            rp_id=rp_id, grant=grant, registration_mode=mode, credential_epoch=state.credential_epoch, name=name,
            issuer_kind=issuer_kind, issuer_credential_id=issuer_id,
            challenge=bytes_to_base64url(options.challenge), expires_at=expires)
    return {"options": json.loads(options_to_json(options)), "challenge_id": str(challenge.id), "expires_at": expires.isoformat()}


def _claim_challenge(request, purpose, data):
    origin, _ = request_origin(request)
    credential = data.get("credential")
    if not isinstance(credential, dict):
        raise ApiError("PASSKEY_INVALID", "Passkey 认证数据无效", 400)
    digest = hashlib.sha256(json.dumps(credential, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    current_hash = _session_hash(request)
    with transaction.atomic(using="default"):
        challenge = PasskeyChallenge.objects.filter(pk=_id(data.get("challenge_id")), purpose=purpose, origin=origin).first()
        if challenge and purpose == "register" and challenge.state == "completed":
            # GET /session can discard a rotated cookie before this retry arrives.
            # The exact signed response retrieves only a receipt, never a new session.
            if (challenge.request_hash == digest and challenge.completed_at > timezone.now() - timedelta(minutes=10)
                    and (challenge.registration_mode != "local" or can_add_passkey(request))):
                return challenge, credential, True
        if not challenge or challenge.session_hash != current_hash or challenge.expires_at <= timezone.now():
            raise ApiError("PASSKEY_CHALLENGE", "认证请求已过期，请重新操作", 400)
        if challenge.state == "processing":
            raise ApiError("AUTH_ATTEMPT_IN_PROGRESS", "认证正在处理，请稍后重试", 409)
        if challenge.state != "pending":
            raise ApiError("PASSKEY_CHALLENGE", "认证请求已结束，请重新操作", 400)
        challenge.state, challenge.request_hash, challenge.processing_at = "processing", digest, timezone.now()
        challenge.save(update_fields=["state", "request_hash", "processing_at"])
    return challenge, credential, False


def _check_client_data(credential):
    try:
        client = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
        if client.get("crossOrigin", False) is not False:
            raise ValueError("Cross-origin ceremony")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ApiError("PASSKEY_INVALID", "Passkey 认证数据无效", 400) from None


def _registration_commit(request, challenge, credential, verification):
    with transaction.atomic(using="default"):
        state = get_auth_state()
        current = PasskeyChallenge.objects.filter(pk=challenge.pk, state="processing").first()
        if not current or current.expires_at <= timezone.now() or state.credential_epoch != challenge.credential_epoch:
            raise ApiError("AUTH_REVOKED", "认证请求已失效，请重新操作", 403)
        grant = EnrollmentGrant.objects.filter(pk=challenge.grant_id).first() if challenge.grant_id else None
        if challenge.registration_mode == "local":
            kind, credential_id, epoch = require_credential_session(request)
            if (kind, credential_id, epoch) != (challenge.issuer_kind, challenge.issuer_credential_id, challenge.credential_epoch):
                raise ApiError("AUTH_REVOKED", "发起注册的登录凭据已失效", 403)
            user = request.user
        else:
            validate_grant(grant, challenge.origin, state.credential_epoch)
            if challenge.registration_mode == "bootstrap":
                if owner_exists():
                    raise ApiError("AUTH_ALREADY_INITIALIZED", "收藏库已初始化，请登录", 409)
                user = get_user_model().objects.create_user(settings.BOOKMARK_OWNER, password=None)
            else:
                user = grant.user
        stored = PasskeyCredential.objects.create(user=user, credential_id=bytes_to_base64url(verification.credential_id),
            public_key=verification.credential_public_key, sign_count=verification.sign_count,
            rp_id=challenge.rp_id, name=challenge.name,
            last_used_at=None if challenge.registration_mode == "local" else timezone.now(),
            transports=[item for item in credential.get("response", {}).get("transports", [])
                if item in ("usb", "nfc", "ble", "internal", "hybrid", "smart-card")])
        if challenge.registration_mode in ("bootstrap", "reset"):
            if challenge.registration_mode == "reset":
                PasskeyCredential.objects.exclude(pk=stored.pk).delete()
                FileKeyCredential.objects.all().delete()
                Session.objects.all().delete()
            state.credential_epoch += 1
            state.save(update_fields=["credential_epoch"])
            EnrollmentGrant.objects.filter(consumed_at__isnull=True).exclude(pk=grant.pk).update(
                invalidated_at=timezone.now(), active_slot=None)
            PasskeyChallenge.objects.exclude(pk=challenge.pk).update(state="failed")
        if grant:
            grant.consumed_at, grant.active_slot = timezone.now(), None
            grant.save(update_fields=["consumed_at", "active_slot"])
        current.state, current.completed_at = "completed", timezone.now()
        current.result_credential_id, current.credential_epoch = stored.id, state.credential_epoch
        current.save(update_fields=["state", "completed_at", "result_credential_id", "credential_epoch"])
        return stored, state.credential_epoch


def _login_verify(challenge, credential):
    stored = PasskeyCredential.objects.select_related("user").filter(credential_id=credential.get("id"),
        rp_id=challenge.rp_id, user__username=settings.BOOKMARK_OWNER, user__is_active=True).first()
    if stored is None:
        raise ApiError("PASSKEY_INVALID", "此 Passkey 不属于当前收藏库", 401)
    user_handle = credential.get("response", {}).get("userHandle")
    if user_handle is not None and base64url_to_bytes(user_handle) != bytes(get_auth_state().webauthn_user_handle):
        raise ValueError("Wrong user handle")
    verification = verify_authentication_response(credential=credential,
        expected_challenge=base64url_to_bytes(challenge.challenge), expected_rp_id=challenge.rp_id,
        expected_origin=challenge.origin, credential_public_key=bytes(stored.public_key),
        credential_current_sign_count=stored.sign_count, require_user_verification=True)
    with transaction.atomic(using="default"):
        state = get_auth_state()
        fresh = PasskeyCredential.objects.filter(pk=stored.pk).first()
        pending = PasskeyChallenge.objects.filter(pk=challenge.pk, state="processing", expires_at__gt=timezone.now()).exists()
        if not pending or state.credential_epoch != challenge.credential_epoch or fresh is None:
            raise ApiError("AUTH_REVOKED", "登录凭据已失效，请重新操作", 401)
        if fresh.sign_count != stored.sign_count:
            raise ApiError("PASSKEY_INVALID", "认证计数已变化，请重新登录", 400)
        fresh.sign_count, fresh.last_used_at = verification.new_sign_count, timezone.now()
        fresh.save(update_fields=["sign_count", "last_used_at"])
        PasskeyChallenge.objects.filter(pk=challenge.pk).delete()
        return stored, state.credential_epoch


def passkey_request(request, action, data):
    if action in ("register/options", "login/options"):
        return _options(request, "register" if action.startswith("register/") else "login", data)
    if action not in ("register/verify", "login/verify"):
        raise ApiError("NOT_FOUND", "操作不存在", 404)
    purpose = "register" if action.startswith("register/") else "login"
    challenge, credential, completed = _claim_challenge(request, purpose, data)
    if completed:
        return {"registered": True, "authenticated": validate_session(request, clear_invalid=False),
            "credential_id": str(challenge.result_credential_id), "already_completed": True,
            "registration_mode": challenge.registration_mode}
    try:
        _check_client_data(credential)
        if purpose == "register":
            verification = verify_registration_response(credential=credential,
                expected_challenge=base64url_to_bytes(challenge.challenge), expected_rp_id=challenge.rp_id,
                expected_origin=challenge.origin, require_user_verification=True)
            stored, epoch = _registration_commit(request, challenge, credential, verification)
        else:
            stored, epoch = _login_verify(challenge, credential)
    except Exception as exc:
        if purpose == "register":
            PasskeyChallenge.objects.filter(pk=challenge.pk, state="processing").update(state="failed")
        else:
            PasskeyChallenge.objects.filter(pk=challenge.pk).delete()
        if isinstance(exc, ApiError):
            raise
        if isinstance(exc, IntegrityError):
            raise ApiError("PASSKEY_DUPLICATE", "此 Passkey 已登记，请使用它登录", 409) from None
        # WebAuthn exceptions may contain credential material; never echo or log them.
        raise ApiError("PASSKEY_INVALID", "Passkey 验证未通过，请重新操作", 400) from None
    if purpose != "register" or challenge.registration_mode != "local":
        if challenge.registration_mode == "reset":
            request.session.flush()
        establish_session(request, stored, epoch)
    return {"registered": purpose == "register", "authenticated": True,
        "credential_id": str(stored.id), "already_completed": False,
        "registration_mode": challenge.registration_mode if purpose == "register" else None}


def file_key_request(request, action, data):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    origin, rp_id = request_origin(request, allow_ip=True)
    if action == "options":
        if not FileKeyCredential.objects.filter(user__username=settings.BOOKMARK_OWNER, user__is_active=True).exists():
            raise ApiError("KEY_NOT_CONFIGURED", "尚未配置私钥文件登录", 409)
        now = timezone.now()
        session_hash = _session_hash(request)
        with transaction.atomic(using="default"):
            _cleanup_challenges()
            challenge = PasskeyChallenge.objects.create(session_hash=session_hash, purpose="file_key", origin=origin,
                rp_id=rp_id, credential_epoch=get_auth_state().credential_epoch,
                challenge=secrets.token_urlsafe(32), expires_at=now + timedelta(minutes=3))
        message = f"website-file-key-login-v1\n{origin}\n{challenge.id}\n{challenge.challenge}"
        return {"challenge_id": str(challenge.id), "message": message}
    if action != "verify":
        raise ApiError("NOT_FOUND", "操作不存在", 404)
    session_hash = _session_hash(request)
    with transaction.atomic(using="default"):
        challenge = PasskeyChallenge.objects.filter(pk=_id(data.get("challenge_id")), purpose="file_key", state="pending",
            session_hash=session_hash, origin=origin, expires_at__gt=timezone.now()).first()
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
    matched = None
    for stored in FileKeyCredential.objects.select_related("user").filter(user__username=settings.BOOKMARK_OWNER, user__is_active=True):
        try:
            Ed25519PublicKey.from_public_bytes(bytes(stored.public_key)).verify(signature, message)
        except InvalidSignature:
            continue
        matched = stored
        break
    if matched is None:
        raise ApiError("KEY_INVALID", "此私钥不属于当前收藏库", 401)
    with transaction.atomic(using="default"):
        state = get_auth_state()
        if (challenge.credential_epoch != state.credential_epoch or challenge.expires_at <= timezone.now()
                or not credential_exists("file_key", matched.pk, matched.user_id)):
            raise ApiError("AUTH_REVOKED", "登录凭据已失效，请重新操作", 401)
        FileKeyCredential.objects.filter(pk=matched.pk).update(last_used_at=timezone.now())
        epoch = state.credential_epoch
    establish_session(request, matched, epoch)
    return {"authenticated": True}
