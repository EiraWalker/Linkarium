import os
from datetime import timedelta
from .paths import PROJECT_ROOT, DATA_ROOT, prepare_paths, generation_root, secret_key

prepare_paths()
SECRET_KEY = secret_key()
DEBUG = os.environ.get("BOOKMARK_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get("BOOKMARK_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver").split(",")
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "axes", "runtime", "library"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "axes.middleware.AxesMiddleware",
    "config.middleware.ResponsePolicyMiddleware",
]
if os.environ.get("BOOKMARK_PERF", "0") == "1":
    MIDDLEWARE.insert(0, "config.middleware.PerformanceMiddleware")
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": DATA_ROOT / "state.sqlite3", "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"}},
    "library": {"ENGINE": "django.db.backends.sqlite3", "NAME": generation_root() / "library.sqlite3", "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"}},
}
DATABASE_ROUTERS = ["config.router.LibraryRouter"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "zh-hans"
BOOKMARK_AUTH_DEBUG = os.environ.get("BOOKMARK_AUTH_DEBUG", "0") == "1"
BOOKMARK_PASSKEY_ORIGIN = os.environ.get("BOOKMARK_PASSKEY_ORIGIN", "")
AUTHENTICATION_BACKENDS = (["axes.backends.AxesStandaloneBackend", "django.contrib.auth.backends.ModelBackend", "runtime.authentication.PublicKeyBackend"]
    if BOOKMARK_AUTH_DEBUG else ["axes.backends.AxesStandaloneBackend", "runtime.authentication.PublicKeyBackend"])
AUTH_PASSWORD_VALIDATORS = ([{"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 6}}]
    if BOOKMARK_AUTH_DEBUG else [{"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}}, {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"}, {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"}])
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=15)
AXES_LOCKOUT_PARAMETERS = ["ip_address"]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_CALLABLE = "config.middleware.axes_lockout"
AXES_ENABLE_ACCESS_FAILURE_LOG = False
AXES_CLIENT_IP_CALLABLE = "config.middleware.client_ip"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 604800
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_MODE = os.environ.get("BOOKMARK_HTTPS", "0") == "1"
SESSION_COOKIE_SECURE = SECURE_MODE
CSRF_COOKIE_SECURE = SECURE_MODE
SECURE_SSL_REDIRECT = SECURE_MODE and os.environ.get("BOOKMARK_REDIRECT_HTTPS", "0") == "1"
SECURE_HSTS_SECONDS = 31536000 if SECURE_MODE else 0
CSRF_TRUSTED_ORIGINS = [v for v in os.environ.get("BOOKMARK_CSRF_ORIGINS", "").split(",") if v]
CSRF_FAILURE_VIEW = "config.middleware.csrf_failure"
if os.environ.get("BOOKMARK_TRUST_PROXY", "0") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
FILE_UPLOAD_TEMP_DIR = DATA_ROOT / "work" / "http"
APPEND_SLASH = False
STATIC_URL = "/assets/"
STATIC_ROOT = PROJECT_ROOT / "frontend" / "dist" / "assets"
WHITENOISE_AUTOREFRESH = DEBUG
BOOKMARK_CAPTURE_MODE = os.environ.get("BOOKMARK_CAPTURE_MODE", "isolated")
BOOKMARK_CAPTURE_SOCKET = os.environ.get("BOOKMARK_CAPTURE_SOCKET", "/run/bookmark-capture/launcher.sock")
BOOKMARK_OWNER = "owner"
BASE_DIR = PROJECT_ROOT
