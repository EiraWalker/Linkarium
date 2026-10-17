from django.core.management.base import BaseCommand
import os
import ipaddress

class Command(BaseCommand):
    help = "Serve built frontend and Django API with Waitress"

    def add_arguments(self, parser):
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=8000)

    def handle(self, *args, **options):
        import tempfile
        from config.paths import DATA_ROOT
        tempfile.tempdir = str(DATA_ROOT / "work" / "http")
        from config.wsgi import application
        from waitress import serve
        from runtime.network_access import web_addresses
        proxy_options = {}
        if os.environ.get("BOOKMARK_TRUST_PROXY", "0") == "1":
            peer = os.environ.get("BOOKMARK_PROXY_PEER", "127.0.0.1")
            ipaddress.ip_address(peer)  # Reject a wildcard or ambiguous hostname.
            proxy_options = {"trusted_proxy": peer, "trusted_proxy_count": 1, "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-proto"}}
        listeners = " ".join(f"{'[' + host + ']' if ':' in host else host}:{options['port']}" for host in web_addresses(options["host"]))
        serve(application, listen=listeners, threads=4, clear_untrusted_proxy_headers=True, max_request_body_size=16*1024*1024*1024, **proxy_options)
