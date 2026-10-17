"""Enrollment authority. Only issuance returns plaintext authorization secrets."""
from datetime import timedelta
import hashlib
import hmac
import secrets
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from library.errors import ApiError
from .models import AuthState, EnrollmentGrant, FileKeyCredential, PasskeyCredential


INITIAL_PASSKEY_PURPOSES = ("bootstrap", "first_passkey")


def get_auth_state():
    state = AuthState.objects.filter(pk=1).first()
    if state is None:
        owner = get_user_model().objects.filter(username=settings.BOOKMARK_OWNER).first()
        handle = f"website-owner-{owner.pk}".encode() if owner else secrets.token_bytes(32)
        state, _ = AuthState.objects.get_or_create(pk=1, defaults={"webauthn_user_handle": handle})
    return state


def owner_exists():
    # A disabled or damaged owner must never reopen anonymous initialization.
    return get_user_model().objects.filter(username=settings.BOOKMARK_OWNER).exists()


def close_bootstrap_grants():
    """Call in the same transaction that creates the first owner and credential."""
    state = get_auth_state()
    state.credential_epoch += 1
    state.save(update_fields=["credential_epoch"])
    EnrollmentGrant.objects.filter(purpose="bootstrap", consumed_at__isnull=True).update(
        invalidated_at=timezone.now(), active_slot=None)
    return state.credential_epoch


def credential_exists(kind, credential_id, user_id=None):
    model = {"passkey": PasskeyCredential, "file_key": FileKeyCredential}.get(kind)
    if model is None or credential_id is None:
        return False
    query = model.objects.filter(pk=credential_id, user__username=settings.BOOKMARK_OWNER, user__is_active=True)
    return query.filter(user_id=user_id).exists() if user_id is not None else query.exists()


def _digest_code(grant, code):
    material = f"enrollment-v1:{grant.id}:{grant.purpose}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), material, hashlib.sha256).hexdigest()


def passkey_exists(rp_id):
    return PasskeyCredential.objects.filter(user__username=settings.BOOKMARK_OWNER, rp_id=rp_id).exists()


def invalidate_initialization_grants():
    """Close old first-Passkey codes before startup or recovery exposes a Web entry."""
    EnrollmentGrant.objects.filter(purpose__in=INITIAL_PASSKEY_PURPOSES,
        active_slot__isnull=False).update(active_slot=None, invalidated_at=timezone.now())


def _issue(purpose, origin, rp_id, user, issuer_kind, issuer_id, name):
    now = timezone.now()
    state = get_auth_state()
    if purpose == "bootstrap":
        if owner_exists():
            raise ApiError("AUTH_ALREADY_INITIALIZED", "收藏库已初始化，请登录或从终端重置", 409)
        EnrollmentGrant.objects.filter(active_slot="bootstrap").update(active_slot=None, invalidated_at=now)
    elif user is None:
        raise ApiError("OWNER_NOT_INITIALIZED", "收藏库尚未初始化", 409)
    if purpose == "first_passkey":
        if passkey_exists(rp_id):
            raise ApiError("AUTH_ALREADY_INITIALIZED", "已有 Passkey，请登录后添加其他设备", 409)
        EnrollmentGrant.objects.filter(active_slot="first_passkey").update(active_slot=None, invalidated_at=now)
    token, code = secrets.token_urlsafe(32), f"{secrets.randbelow(100000000):08d}"
    if purpose in INITIAL_PASSKEY_PURPOSES:
        # These codes have no URL token: a repeated number would revive an old code.
        previous_grants = list(EnrollmentGrant.objects.filter(purpose__in=INITIAL_PASSKEY_PURPOSES).only("id", "purpose", "code_hash"))
        while any(hmac.compare_digest(previous.code_hash, _digest_code(previous, code)) for previous in previous_grants):
            code = f"{secrets.randbelow(100000000):08d}"
    grant = EnrollmentGrant(id=uuid.uuid4(), purpose=purpose, user=user, origin=origin, rp_id=rp_id,
        credential_epoch=state.credential_epoch, issuer_kind=issuer_kind, issuer_credential_id=issuer_id,
        token_hash=hashlib.sha256(token.encode()).hexdigest(), name=name,
        active_slot=purpose if purpose in INITIAL_PASSKEY_PURPOSES else None,
        expires_at=None if purpose in INITIAL_PASSKEY_PURPOSES else now + timedelta(minutes=5 if purpose == "add_device" else 10))
    grant.code_hash = _digest_code(grant, code)
    grant.save()
    fragment = "reset" if purpose == "reset" else "enroll"
    result = {"enrollment_url": f"{origin}/login" + ("" if purpose in INITIAL_PASSKEY_PURPOSES else f"#{fragment}={token}"),
        "authorization_code": code, "expires_at": grant.expires_at.isoformat() if grant.expires_at else None}
    if purpose == "bootstrap":
        result["initialization_code"] = code
    return result


def issue_console_grant(purpose, origin, name="Passkey"):
    from .authentication import normalize_origin
    if purpose not in ("bootstrap", "first_passkey", "add_device", "reset"):
        raise ValueError("Unknown enrollment purpose")
    origin, rp_id = normalize_origin(origin)
    if settings.BOOKMARK_PASSKEY_ORIGIN:
        if normalize_origin(settings.BOOKMARK_PASSKEY_ORIGIN)[0] != origin:
            raise ValueError("Origin differs from BOOKMARK_PASSKEY_ORIGIN")
    elif rp_id != "localhost":
        raise ValueError("Configure BOOKMARK_PASSKEY_ORIGIN for a public deployment")
    with transaction.atomic(using="default"):
        user = get_user_model().objects.filter(username=settings.BOOKMARK_OWNER, is_active=True).first()
        return _issue(purpose, origin, rp_id, user, "console", None, name[:100])


def issue_device_grant(request):
    from .authentication import request_origin
    from .auth_sessions import require_credential_session
    from .network_access import connection_status
    connection = connection_status()
    if connection is not None and not connection["device_enrollment"]:
        raise ApiError("DEVICE_HTTPS_REQUIRED", "启用 HTTPS 后可添加其他设备", 403)
    origin, rp_id = request_origin(request)
    with transaction.atomic(using="default"):
        kind, credential_id, _ = require_credential_session(request)
        return _issue("add_device", origin, rp_id, request.user, kind, credential_id, "Passkey")


def validate_grant(grant, origin, epoch=None):
    """Recheck inside the final write transaction, including its issuing credential."""
    now = timezone.now()
    if grant is None or grant.origin != origin:
        raise ApiError("AUTH_GRANT_INVALID", "授权无效，请重新获取", 403)
    if grant.failed_attempts >= 5:
        raise ApiError("AUTH_GRANT_LOCKED", "授权码错误次数已达上限，请重新获取", 429)
    if grant.consumed_at:
        raise ApiError("AUTH_GRANT_USED", "授权已使用，请使用刚创建的 Passkey 登录", 409)
    if grant.expires_at is None and grant.purpose not in INITIAL_PASSKEY_PURPOSES:
        raise ApiError("AUTH_GRANT_INVALID", "授权无效，请重新获取", 403)
    if grant.expires_at is not None and grant.expires_at <= now:
        raise ApiError("AUTH_GRANT_EXPIRED", "授权已过期，请重新获取", 410)
    if grant.invalidated_at or grant.credential_epoch != (epoch if epoch is not None else get_auth_state().credential_epoch):
        raise ApiError("AUTH_REVOKED", "授权已失效，请重新获取", 403)
    if grant.purpose == "bootstrap":
        if owner_exists():
            raise ApiError("AUTH_ALREADY_INITIALIZED", "收藏库已初始化，请登录", 409)
    elif not get_user_model().objects.filter(pk=grant.user_id, username=settings.BOOKMARK_OWNER, is_active=True).exists():
        raise ApiError("AUTH_REVOKED", "授权已失效，请重新获取", 403)
    if grant.purpose == "first_passkey" and passkey_exists(grant.rp_id):
        raise ApiError("AUTH_ALREADY_INITIALIZED", "已有 Passkey，请使用它登录", 409)
    if grant.issuer_kind != "console" and not credential_exists(grant.issuer_kind, grant.issuer_credential_id, grant.user_id):
        raise ApiError("AUTH_REVOKED", "签发凭据已撤销，请重新获取授权", 403)
    return grant


def authorize_registration(data, origin):
    """Returns a grant or None for local enrollment; failed guesses commit before raising."""
    if "setup_token" in data:
        raise ApiError("AUTH_GRANT_INVALID", "旧绑定链接已停用，请重新获取链接和授权码", 403)
    bootstrap = "bootstrap_code" in data
    first_passkey = "first_passkey_code" in data
    linked = "enrollment_token" in data or "authorization_code" in data
    if sum((bootstrap, first_passkey, linked)) > 1:
        raise ApiError("AUTH_GRANT_INVALID", "请只使用一种注册授权", 400)
    if not bootstrap and not first_passkey and not linked:
        return None
    code = data.get("bootstrap_code") if bootstrap else data.get("first_passkey_code") if first_passkey else data.get("authorization_code")
    token = data.get("enrollment_token")
    failure = None
    with transaction.atomic(using="default"):
        if bootstrap:
            if owner_exists():
                raise ApiError("AUTH_ALREADY_INITIALIZED", "收藏库已初始化，请登录", 409)
            grant = EnrollmentGrant.objects.filter(active_slot="bootstrap").first()
        elif first_passkey:
            grant = EnrollmentGrant.objects.filter(active_slot="first_passkey").first()
        else:
            if not isinstance(token, str) or not 32 <= len(token) <= 100:
                raise ApiError("AUTH_GRANT_INVALID", "授权无效，请重新获取", 403)
            grant = EnrollmentGrant.objects.filter(token_hash=hashlib.sha256(token.encode()).hexdigest(),
                purpose__in=("add_device", "reset")).first()
        validate_grant(grant, origin)
        if not isinstance(code, str) or not hmac.compare_digest(_digest_code(grant, code), grant.code_hash):
            grant.failed_attempts += 1
            grant.save(update_fields=["failed_attempts"])
            failure = ApiError("AUTH_GRANT_LOCKED" if grant.failed_attempts >= 5 else "AUTH_GRANT_INVALID",
                "授权码错误次数已达上限，请重新获取" if grant.failed_attempts >= 5 else "授权码不正确", 429 if grant.failed_attempts >= 5 else 403)
    if failure:
        raise failure
    return grant
