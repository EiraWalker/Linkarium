from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from runtime.models import FileKeyCredential
from django.db import connection
from runtime.file_keys import generate_private_key, register_public_key


class Command(BaseCommand):
    help = "Register an Ed25519 public key, or generate a PKCS8 PEM login key in a chosen private file"

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--public-key", help="PEM or OpenSSH Ed25519 public key file")
        group.add_argument("--generate", help="New private PKCS8 PEM output path (never overwritten)")
        parser.add_argument("--name", default="私钥文件")

    def handle(self, *args, **options):
        User = get_user_model()
        tables = connection.introspection.table_names()
        user = User.objects.filter(username=settings.BOOKMARK_OWNER, is_active=True).first() if User._meta.db_table in tables else None
        if (not user or FileKeyCredential._meta.db_table not in tables) and not options["generate"]:
            raise CommandError("Run initialize first")
        if options["generate"]:
            output = Path(options["generate"]).absolute()
            try:
                private = generate_private_key(output)
            except OSError as error:
                raise CommandError("Private key output must be a new file in an existing directory") from error
            public = private.public_key()
        else:
            try:
                path = Path(options["public_key"])
                if not path.is_file() or path.stat().st_size > 16384:
                    raise ValueError("Invalid key size")
                data = path.read_bytes()
                public = (serialization.load_ssh_public_key(data) if data.startswith(b"ssh-")
                    else serialization.load_pem_public_key(data))
                if not isinstance(public, Ed25519PublicKey):
                    raise ValueError("Only Ed25519 keys are supported")
            except (OSError, ValueError, TypeError) as error:
                raise CommandError("Provide an Ed25519 public key (PEM or OpenSSH)") from error
        if user and FileKeyCredential._meta.db_table in tables:
            stored = register_public_key(user, public, options["name"])
            self.stdout.write("Public key configured: " + stored.fingerprint)
        else:
            self.stdout.write("Key generated. Next run: initialize --private-key " + str(output))
        if options["generate"]:
            self.stdout.write("Choose this PRIVATE KEY file when logging in: " + str(output))
