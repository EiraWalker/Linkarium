"""Credential-bound, absolute fourteen-day sessions shared by API and static guards."""
from datetime import datetime, timedelta
import uuid

from django.conf import settings
from django.contrib.auth import login, logout
from django.utils import timezone

from library.errors import ApiError
from .auth_grants import credential_exists, get_auth_state

AUTH_VERSION = 1
PUBLIC_BACKEND = "runtime.authentication.PublicKeyBackend"


def _record_session(request, kind, credential_id, epoch):
    now = timezone.now()
    expires = now + timedelta(days=14)
    request.session.update({"auth_version": AUTH_VERSION, "auth_mode": kind,
        "credential_id": str(credential_id) if credential_id else None, "credential_epoch": epoch,
        "authenticated_at": now.isoformat(), "auth_expires_at": expires.isoformat()})
    # Store an ISO deadline because the project's JSON session serializer cannot encode datetime.
    request.session.set_expiry(expires.isoformat())


def establish_session(request, credential, epoch):
    from .models import PasskeyCredential
    login(request, credential.user, backend=PUBLIC_BACKEND)
    _record_session(request, "passkey" if isinstance(credential, PasskeyCredential) else "file_key", credential.id, epoch)


def establish_debug_session(request):
    _record_session(request, "password", None, get_auth_state().credential_epoch)


def validate_session(request, *, clear_invalid=True):
    if not request.user.is_authenticated:
        return False
    valid = False
    try:
        session = request.session
        expires = datetime.fromisoformat(session["auth_expires_at"])
        valid = (session.get("auth_version") == AUTH_VERSION and expires > timezone.now()
            and request.user.is_active and request.user.username == settings.BOOKMARK_OWNER
            and session.get("credential_epoch") == get_auth_state().credential_epoch)
        kind = session.get("auth_mode")
        if valid and kind == "password":
            valid = settings.BOOKMARK_AUTH_DEBUG and session.get("_auth_user_backend") == "django.contrib.auth.backends.ModelBackend"
        elif valid:
            valid = (session.get("_auth_user_backend") == PUBLIC_BACKEND
                and credential_exists(kind, uuid.UUID(session.get("credential_id", "")), request.user.pk))
    except (ValueError, TypeError, KeyError, AttributeError):
        valid = False
    if not valid and clear_invalid:
        logout(request)
    return bool(valid)


def require_credential_session(request):
    if not validate_session(request, clear_invalid=False) or request.session.get("auth_mode") not in ("passkey", "file_key"):
        raise ApiError("AUTH_REVOKED", "请使用有效的 Passkey 或私钥文件登录", 401)
    return request.session["auth_mode"], uuid.UUID(request.session["credential_id"]), request.session["credential_epoch"]


def session_details(request):
    credential_id = request.session.get("credential_id") if request.user.is_authenticated else None
    expires_at = request.session.get("auth_expires_at") if request.user.is_authenticated else None
    return {"current_credential_id": credential_id, "expires_at": expires_at,
        "credential_id": credential_id, "auth_expires_at": expires_at}
