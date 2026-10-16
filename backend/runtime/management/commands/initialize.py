import getpass
from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.contrib.sessions.models import Session
from runtime.models import RuntimeState
from library.models import LibrarySettings
from django.conf import settings
from django.db import transaction
from runtime.file_keys import read_private_key, register_public_key


class Command(BaseCommand):
    help = "Initialize with --private-key in normal mode; --debug permits an interactive password"

    def add_arguments(self, parser):
        parser.add_argument("--reset-password", action="store_true")
        parser.add_argument("--no-password", action="store_true", help="Skip debug password setup; normal mode still requires --private-key")
        parser.add_argument("--private-key", help="Existing Ed25519 PKCS8 PEM private key file (required without --debug)")

    def handle(self, *args, **options):
        if not settings.BOOKMARK_AUTH_DEBUG:
            if options["reset_password"]:
                raise CommandError("Password authentication requires --debug.")
            if not options["private_key"]:
                raise CommandError("Normal initialization requires --private-key <Ed25519 PKCS8 PEM file>.")
        public = None
        if options["private_key"]:
            try:
                public = read_private_key(options["private_key"]).public_key()
            except (OSError, ValueError, TypeError):
                raise CommandError("Private key invalid: provide an existing, unencrypted Ed25519 PKCS8 PEM file.") from None
        call_command("migrate", database="default", verbosity=0)
        call_command("migrate", database="library", verbosity=0)
        RuntimeState.objects.get_or_create(pk=1)
        LibrarySettings.objects.get_or_create(pk=1)
        User = get_user_model()
        existing = User.objects.filter(username="owner").first()
        if not settings.BOOKMARK_AUTH_DEBUG:
            with transaction.atomic(using="default"):
                user = existing or User(username="owner", is_active=True)
                if not existing:
                    user.set_unusable_password()
                    user.save()
                register_public_key(user, public)
            self.stdout.write("Initialized. Sign in with the private key file, then add Passkey in Settings.")
            return
        if options["no_password"] or (existing and not options["reset_password"]):
            if public is not None and existing:
                register_public_key(existing, public)
            self.stdout.write("Databases ready. Existing owner retained." if existing else "Databases ready. Run initialize without --no-password to set the owner password.")
            return
        password = getpass.getpass("Debug password (6+ characters): ")
        if password != getpass.getpass("Confirm password: "):
            raise CommandError("Passwords do not match")
        try:
            password_validation.validate_password(password)
        except ValidationError as error:
            raise CommandError("; ".join(error.messages))
        user = existing or User(username="owner")
        user.set_password(password)
        user.is_active = True
        user.save()
        if public is not None:
            register_public_key(user, public)
        Session.objects.all().delete()
        self.stdout.write(self.style.SUCCESS("Owner password configured; sessions revoked."))
