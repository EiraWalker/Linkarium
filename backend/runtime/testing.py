"""Credential sessions for business tests; authentication tests exercise HTTP login."""
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.test import RequestFactory

from .auth_grants import get_auth_state
from .auth_sessions import establish_session
from .file_keys import register_public_key
from .models import FileKeyCredential


def force_credential_login(client, user):
    credential = FileKeyCredential.objects.filter(user=user).first()
    if credential is None:
        credential = register_public_key(user, Ed25519PrivateKey.generate().public_key(), "Test credential")
    request = RequestFactory().get("/")
    request.session = client.session
    establish_session(request, credential, get_auth_state().credential_epoch)
    request.session.save()
    client.cookies[settings.SESSION_COOKIE_NAME] = request.session.session_key
    return credential
