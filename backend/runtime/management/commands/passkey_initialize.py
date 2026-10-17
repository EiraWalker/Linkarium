from .passkey_setup import Command as GrantCommand


class Command(GrantCommand):
    help = "Issue a first-Passkey code valid until used; retains an existing owner without a Passkey"
    purpose = "bootstrap"

    def grant_purpose(self):
        from runtime.auth_grants import owner_exists
        return "first_passkey" if owner_exists() else "bootstrap"
