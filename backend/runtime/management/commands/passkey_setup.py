from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from library.errors import ApiError
from runtime.auth_grants import issue_console_grant


class Command(BaseCommand):
    help = "Add a Passkey using a one-use link and separate authorization code (5 minutes)"
    purpose = "add_device"

    def grant_purpose(self):
        return self.purpose

    def add_arguments(self, parser):
        parser.add_argument("--origin", help="Exact configured browser origin; defaults to BOOKMARK_PASSKEY_ORIGIN")
        parser.add_argument("--name", default="Passkey")

    def handle(self, *args, **options):
        origin = options["origin"] or settings.BOOKMARK_PASSKEY_ORIGIN
        if not origin:
            raise CommandError("Supply --origin http://localhost:<port> for local development, or configure BOOKMARK_PASSKEY_ORIGIN for HTTPS.")
        try:
            purpose = self.grant_purpose()
            grant = issue_console_grant(purpose, origin, name=options["name"])
        except ApiError as error:
            raise CommandError(f"{error.code}: {error.message}") from None
        except (ValueError, TypeError):
            raise CommandError("Use the configured HTTPS origin, or http://localhost:<port> for local development; IP addresses are not valid Passkey RP IDs.") from None
        except DatabaseError:
            raise CommandError("Databases are not ready. Stop services, back up existing data and run manage.py initialize offline.") from None
        if purpose == "bootstrap":
            self.stdout.write("First Passkey initialization (valid until used). Previous initialization codes are now invalid.")
        elif purpose == "first_passkey":
            self.stdout.write("First Passkey for the existing owner (valid until used). Existing owner, file keys and library data are retained.")
        elif purpose == "reset":
            self.stdout.write("Passkey reset (10 minutes). Completing registration revokes every old Passkey, file-key credential and session; library data is retained.")
        else:
            self.stdout.write("Add a Passkey (5 minutes). Existing credentials are retained.")
        self.stdout.write(f"Open: {grant['enrollment_url']}")
        self.stdout.write(f"Authorization code: {grant['authorization_code']}")
        self.stdout.write(f"Expires at: {grant['expires_at']}" if grant['expires_at'] else "Expires at: never (single use)")
