from django.core.management.base import BaseCommand, CommandError
from runtime.authentication import issue_setup_token


class Command(BaseCommand):
    help = "Issue a local-admin-authorized, one-use Passkey enrollment link (10 minutes)"

    def add_arguments(self, parser):
        parser.add_argument("--origin", required=True, help="Exact browser origin, e.g. http://localhost:8001")
        parser.add_argument("--name", default="Passkey")

    def handle(self, *args, **options):
        try:
            token = issue_setup_token(options["origin"], options["name"])
            from runtime.authentication import normalize_origin
            origin, _ = normalize_origin(options["origin"])
        except Exception as error:
            raise CommandError("Run initialize first, and verify the HTTPS/origin configuration.") from error
        self.stdout.write("Open this one-use link within 10 minutes to bind Windows Hello / phone:")
        self.stdout.write(f"{origin}/login#passkey-setup={token}")
