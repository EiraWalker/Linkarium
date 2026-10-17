from .passkey_setup import Command as GrantCommand


class Command(GrantCommand):
    help = "Issue a one-use reset link and code (10 minutes); replacement registration revokes all old credentials and sessions"
    purpose = "reset"
