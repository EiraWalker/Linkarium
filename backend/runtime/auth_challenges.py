"""Bound anonymous authentication work without creating durable login sessions.

Browser binding lives in a signed, short-lived cookie. Shared database budgets
cover every Web process; both the challenge set and source counter table are
bounded. Call cleanup_authentication_state at startup and periodically at idle.
"""
from datetime import timedelta
import hashlib
import hmac
import ipaddress
import secrets

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from library.errors import ApiError
from .models import AuthRequestBucket, PasskeyChallenge

NONCE_COOKIE = "linkarium_auth_nonce"
NONCE_SECONDS = 15 * 60
NONCE_SALT = "linkarium.authentication.browser.v1"
SOURCE_BUCKET_LIMIT = 1024
SOURCE_OPTIONS_PER_MINUTE = 60
SOURCE_VERIFY_PER_MINUTE = 120
GLOBAL_OPTIONS_PER_MINUTE = 600
GLOBAL_VERIFY_PER_MINUTE = 1200
CHALLENGE_LIMIT = 512
BROWSER_PENDING_LIMIT = 8


def _nonce(request):
    value = request.COOKIES.get(NONCE_COOKIE, "")
    if not isinstance(value, str) or len(value) > 256:
        return None
    try:
        nonce = signing.loads(value, salt=NONCE_SALT, max_age=NONCE_SECONDS)
    except (signing.BadSignature, ValueError, TypeError):
        return None
    return nonce if isinstance(nonce, str) and len(nonce) == 43 else None


def browser_binding(request, *, create=False):
    """Return a ceremony-only binding. Missing verify cookies never allocate state."""
    nonce = _nonce(request)
    if nonce is None:
        if not create:
            return None
        nonce = secrets.token_urlsafe(32)
    if create:
        # Refresh the cookie's signature lifetime while keeping existing
        # ceremonies bound to the same nonce. A new three-minute challenge
        # must never outlive a cookie issued fourteen minutes earlier.
        request._authentication_nonce = signing.dumps(nonce, salt=NONCE_SALT)
    return hashlib.sha256(nonce.encode()).hexdigest()


def set_browser_cookie(request, response):
    value = getattr(request, "_authentication_nonce", None)
    if value and response.status_code < 400:
        response.set_cookie(NONCE_COOKIE, value, max_age=NONCE_SECONDS, path="/",
            httponly=True, secure=settings.SESSION_COOKIE_SECURE or request.is_secure(), samesite="Strict")


def _source_key(request):
    # REMOTE_ADDR has already been resolved by Waitress's trusted-proxy policy.
    # Group IPv6 privacy addresses by /64 so rotating one host address cannot
    # bypass its source budget. Store only a server-keyed pseudonymous digest.
    source = request.META.get("REMOTE_ADDR", "unknown")
    try:
        address = ipaddress.ip_address(source)
        source = str(ipaddress.ip_network(f"{address}/64", strict=False)) if address.version == 6 else str(address)
    except ValueError:
        source = "unknown"
    return hmac.new(settings.SECRET_KEY.encode(), source.encode(), hashlib.sha256).hexdigest()


def limit_authentication_request(request, *, options):
    """Charge a shared one-minute budget, including rejected credential guesses."""
    now = timezone.now()
    window = now.replace(second=0, microsecond=0)
    field = "options_count" if options else "verify_count"
    source_limit = SOURCE_OPTIONS_PER_MINUTE if options else SOURCE_VERIFY_PER_MINUTE
    global_limit = GLOBAL_OPTIONS_PER_MINUTE if options else GLOBAL_VERIFY_PER_MINUTE
    limited = False
    with transaction.atomic(using="default"):
        AuthRequestBucket.objects.filter(window_start__lt=window).delete()
        global_bucket, _ = AuthRequestBucket.objects.get_or_create(key="global", defaults={"window_start": window})
        if getattr(global_bucket, field) >= global_limit:
            limited = True
        else:
            setattr(global_bucket, field, getattr(global_bucket, field) + 1)
            global_bucket.save(update_fields=[field])
            key = _source_key(request)
            source = AuthRequestBucket.objects.filter(key=key).first()
            if source is None:
                if AuthRequestBucket.objects.exclude(key="global").count() >= SOURCE_BUCKET_LIMIT:
                    limited = True
                else:
                    source = AuthRequestBucket.objects.create(key=key, window_start=window)
            if source is not None:
                if getattr(source, field) >= source_limit:
                    limited = True
                else:
                    setattr(source, field, getattr(source, field) + 1)
                    source.save(update_fields=[field])
    if limited:
        raise ApiError("AUTH_RATE_LIMITED", "认证请求过多，请稍后重试", 429)


def _cleanup_challenges(now):
    PasskeyChallenge.objects.filter(Q(completed_at__lt=now - timedelta(minutes=10)) |
        (Q(completed_at__isnull=True) & Q(expires_at__lte=now))).delete()


def reserve_challenge(binding):
    """Check inside the caller's IMMEDIATE creation transaction; never evict live work."""
    _cleanup_challenges(timezone.now())
    if (PasskeyChallenge.objects.count() >= CHALLENGE_LIMIT
            or PasskeyChallenge.objects.filter(session_hash=binding, state__in=("pending", "processing")).count() >= BROWSER_PENDING_LIMIT):
        raise ApiError("AUTH_RATE_LIMITED", "认证请求过多，请稍后重试", 429)


def cleanup_authentication_state():
    """Reclaim expired sessions, ceremonies and rate counters without traffic."""
    now = timezone.now()
    with transaction.atomic(using="default"):
        _cleanup_challenges(now)
        Session.objects.filter(expire_date__lte=now).delete()
        AuthRequestBucket.objects.filter(window_start__lt=now - timedelta(minutes=1)).delete()
