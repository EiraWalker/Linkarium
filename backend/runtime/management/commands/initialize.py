import getpass

from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from config.paths import DATA_ROOT
from runtime.auth_grants import close_bootstrap_grants
from runtime.bootstrap import prepare_databases
from runtime.file_keys import read_private_key, register_public_key
from runtime.instance_lock import InstanceLock


class Command(BaseCommand):
    help = "Prepare databases offline; optionally register --private-key or configure a --debug password"

    def add_arguments(self, parser):
        parser.add_argument("--reset-password", action="store_true")
        parser.add_argument("--no-password", action="store_true", help="Skip interactive debug password setup")
        parser.add_argument("--private-key", help="Optional existing Ed25519 PKCS8 PEM private key file")

    def handle(self, *args, **options):
        if not settings.BOOKMARK_AUTH_DEBUG and options["reset_password"]:
            raise CommandError("Password authentication requires --debug.")
        public = None
        if options["private_key"]:
            try:
                public = read_private_key(options["private_key"]).public_key()
            except (OSError, ValueError, TypeError):
                raise CommandError("Private key invalid: provide an existing, unencrypted Ed25519 PKCS8 PEM file.") from None
        try:
            with InstanceLock(DATA_ROOT / "runtime" / "supervisor.lock"):
                prepare_databases()
                self._configure_identity(options, public)
        except RuntimeError as error:
            raise CommandError(str(error)) from None

    def _configure_identity(self, options, public):
        User = get_user_model()
        existing = User.objects.filter(username=settings.BOOKMARK_OWNER).first()
        if not settings.BOOKMARK_AUTH_DEBUG:
            if public is None:
                self.stdout.write("Databases ready. Existing owner retained." if existing else
                                  "Databases ready. Start the server and use its terminal initialization code to create your first Passkey.")
                return
            with transaction.atomic(using="default"):
                user = existing or User(username=settings.BOOKMARK_OWNER, is_active=True)
                if not existing:
                    user.set_unusable_password()
                    user.save()
                register_public_key(user, public)
                if not existing:
                    close_bootstrap_grants()
            self.stdout.write("Initialized. Sign in with the private key file, then add Passkey in Settings.")
            return
        if options["no_password"] or (existing and not options["reset_password"]):
            if public is not None and existing:
                register_public_key(existing, public)
            self.stdout.write("Databases ready. Existing owner retained." if existing else
                              "Databases ready. Run initialize without --no-password to set the owner password, or start the server for Passkey setup.")
            return
        password = getpass.getpass("Debug password (6+ characters): ")
        if password != getpass.getpass("Confirm password: "):
            raise CommandError("Passwords do not match")
        try:
            password_validation.validate_password(password)
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from None
        with transaction.atomic(using="default"):
            user = existing or User(username=settings.BOOKMARK_OWNER)
            user.set_password(password)
            user.is_active = True
            user.save()
            if public is not None:
                register_public_key(user, public)
            if not existing:
                close_bootstrap_grants()
            Session.objects.all().delete()
        self.stdout.write(self.style.SUCCESS("Owner password configured; sessions revoked."))
