import uuid
from django.conf import settings
from django.db import models


class PasskeyCredential(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    credential_id = models.CharField(max_length=1400, unique=True)
    public_key = models.BinaryField()
    sign_count = models.PositiveBigIntegerField(default=0)
    rp_id = models.CharField(max_length=253)
    name = models.CharField(max_length=100, default="Passkey")
    transports = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True)


class FileKeyCredential(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    fingerprint = models.CharField(max_length=64, unique=True)
    public_key = models.BinaryField()
    name = models.CharField(max_length=100, default="私钥文件")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True)


class PasskeySetupToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=64, unique=True)
    origin = models.CharField(max_length=300)
    name = models.CharField(max_length=100, default="Passkey")
    expires_at = models.DateTimeField()


class AuthState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    credential_epoch = models.PositiveBigIntegerField(default=1)
    webauthn_user_handle = models.BinaryField()


class EnrollmentGrant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purpose = models.CharField(max_length=16)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE)
    origin = models.CharField(max_length=300)
    rp_id = models.CharField(max_length=253)
    credential_epoch = models.PositiveBigIntegerField()
    issuer_kind = models.CharField(max_length=16)
    issuer_credential_id = models.UUIDField(null=True)
    token_hash = models.CharField(max_length=64, unique=True)
    code_hash = models.CharField(max_length=64)
    name = models.CharField(max_length=100, default="Passkey")
    active_slot = models.CharField(max_length=16, null=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True)
    consumed_at = models.DateTimeField(null=True)
    invalidated_at = models.DateTimeField(null=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)


class PasskeyChallenge(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session_hash = models.CharField(max_length=64, db_index=True)
    challenge = models.TextField()
    purpose = models.CharField(max_length=16)
    origin = models.CharField(max_length=300)
    rp_id = models.CharField(max_length=253)
    setup = models.ForeignKey(PasskeySetupToken, on_delete=models.CASCADE, null=True)
    expires_at = models.DateTimeField()
    grant = models.ForeignKey(EnrollmentGrant, null=True, on_delete=models.SET_NULL)
    registration_mode = models.CharField(max_length=16, blank=True)
    credential_epoch = models.PositiveBigIntegerField(default=1)
    name = models.CharField(max_length=100, default="Passkey")
    issuer_kind = models.CharField(max_length=16, blank=True)
    issuer_credential_id = models.UUIDField(null=True)
    state = models.CharField(max_length=16, default="pending")
    request_hash = models.CharField(max_length=64, blank=True)
    processing_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    result_credential_id = models.UUIDField(null=True)


class RuntimeState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    active_generation = models.CharField(max_length=64, default="initial")
    library_epoch = models.PositiveBigIntegerField(default=1)
    maintenance_mode = models.BooleanField(default=False)
    active_maintenance_id = models.UUIDField(null=True, blank=True)
    schema_version = models.PositiveIntegerField(default=1)
    worker_heartbeat = models.DateTimeField(null=True)


class ProviderSecret(models.Model):
    profile_id = models.UUIDField(primary_key=True)
    api_key = models.TextField(blank=True)
    bound_endpoint = models.TextField()
    bound_protocol = models.CharField(max_length=64, default="chat-completions")
    endpoint_fingerprint = models.CharField(max_length=64)
    verified = models.BooleanField(default=False)
    verified_library_epoch = models.PositiveBigIntegerField(null=True)
    updated_at = models.DateTimeField(auto_now=True)


class MaintenanceOperation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=20)
    state = models.CharField(max_length=32, default="queued", db_index=True)
    stage = models.CharField(max_length=64, default="registered")
    source_package = models.TextField(blank=True)
    package_sha256 = models.CharField(max_length=64, blank=True)
    old_generation = models.CharField(max_length=64, blank=True)
    target_generation = models.CharField(max_length=64, blank=True)
    expected_library_epoch = models.PositiveBigIntegerField(default=1)
    claim_token = models.UUIDField(null=True)
    active_key = models.CharField(max_length=32, null=True, unique=True)
    validation_token_hash = models.CharField(max_length=64, blank=True)
    validation_expires_at = models.DateTimeField(null=True)
    result = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class RuntimeIdempotency(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    owner_key = models.CharField(max_length=32, default="owner")
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=200)
    key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    operation = models.ForeignKey(MaintenanceOperation, null=True, on_delete=models.SET_NULL)
    state = models.CharField(max_length=24, default="completed")
    response_status = models.PositiveSmallIntegerField(default=202)
    response_body = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner_key", "method", "path", "key"], name="runtime_idempotency_unique")]
