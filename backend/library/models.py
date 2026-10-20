import uuid
from django.db import models
from django.db.models import Q


class Entity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.PositiveBigIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class LibrarySettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    default_refresh_seconds = models.PositiveIntegerField(default=604800)
    tree_revision = models.PositiveBigIntegerField(default=1)
    revision = models.PositiveBigIntegerField(default=1)
    ui_defaults = models.JSONField(default=dict)
    active_ai_profile_id = models.UUIDField(null=True)


class Folder(Entity):
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="children")
    name = models.CharField(max_length=100)
    name_key = models.CharField(max_length=200)
    position = models.IntegerField(default=0)
    created_by_ai = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name_key"], condition=Q(parent__isnull=True), name="folder_root_name_unique"), models.UniqueConstraint(fields=["parent", "name_key"], name="folder_child_name_unique")]


class Tag(Entity):
    name = models.CharField(max_length=100)
    name_key = models.CharField(max_length=200, unique=True)


class Bookmark(Entity):
    original_url = models.TextField()
    canonical_url = models.TextField(unique=True)
    final_url = models.TextField(blank=True)
    domain = models.CharField(max_length=253, db_index=True)
    source_title = models.CharField(max_length=500, blank=True)
    title_override = models.CharField(max_length=500, blank=True)
    notes = models.TextField(blank=True)
    folder = models.ForeignKey(Folder, null=True, blank=True, on_delete=models.CASCADE, related_name="bookmarks")
    tags = models.ManyToManyField(Tag, through="BookmarkTag", related_name="bookmarks")
    is_starred = models.BooleanField(default=False)
    snapshot_enabled = models.BooleanField(default=True)
    auto_refresh_paused = models.BooleanField(default=False)
    refresh_interval_seconds = models.PositiveIntegerField(null=True, blank=True)
    classification_revision = models.PositiveBigIntegerField(default=1)
    capture_epoch = models.PositiveBigIntegerField(default=1)

    @property
    def title(self):
        return self.title_override or self.source_title or self.domain

    class Meta:
        indexes = [models.Index(fields=["folder", "created_at"]), models.Index(fields=["is_starred", "created_at"])]


class BookmarkTag(models.Model):
    bookmark = models.ForeignKey(Bookmark, on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["bookmark", "tag"], name="bookmark_tag_unique")]


class AIProfile(Entity):
    name = models.CharField(max_length=100, default="AI")
    base_url = models.TextField()
    model = models.CharField(max_length=200)
    protocol = models.CharField(max_length=64, default="chat-completions")
    output_mode = models.CharField(max_length=32, default="json_object")
    enabled = models.BooleanField(default=True)


class JobGroup(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    kind = models.CharField(max_length=24)
    source = models.CharField(max_length=100, blank=True)
    requested_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class Job(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    kind = models.CharField(max_length=32)
    target_id = models.UUIDField(null=True)
    variant = models.CharField(max_length=10, blank=True)
    state = models.CharField(max_length=24, default="queued", db_index=True)
    priority = models.IntegerField(default=40)
    available_at = models.DateTimeField()
    attempt_count = models.PositiveIntegerField(default=0)
    active_attempt_id = models.UUIDField(null=True)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    active_key = models.CharField(max_length=200, null=True, unique=True)
    generation = models.CharField(max_length=64)
    library_epoch = models.PositiveBigIntegerField()
    input_revision = models.PositiveBigIntegerField(default=1)
    payload = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class JobAttempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="attempts")
    attempt_no = models.PositiveIntegerField()
    state = models.CharField(max_length=24, default="running")
    lease_token = models.UUIDField(default=uuid.uuid4)
    lease_expires_at = models.DateTimeField()
    heartbeat_at = models.DateTimeField()
    generation = models.CharField(max_length=64)
    library_epoch = models.PositiveBigIntegerField()
    capture_epoch = models.PositiveBigIntegerField(default=1)
    input_revision = models.PositiveBigIntegerField(default=1)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "attempt_no"], name="attempt_number_unique")]


class JobGroupMember(models.Model):
    group = models.ForeignKey(JobGroup, on_delete=models.CASCADE, related_name="members")
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="memberships")
    bookmark_id = models.UUIDField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["group", "job"], name="group_job_unique")]


class Artifact(models.Model):
    sha256 = models.CharField(primary_key=True, max_length=64)
    relative_path = models.CharField(max_length=200)
    byte_size = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class SnapshotVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    bookmark = models.ForeignKey(Bookmark, on_delete=models.CASCADE, related_name="snapshots")
    variant = models.CharField(max_length=10)
    artifact = models.ForeignKey(Artifact, on_delete=models.PROTECT, related_name="versions")
    capture_attempt = models.OneToOneField(JobAttempt, on_delete=models.PROTECT, related_name="snapshot")
    captured_at = models.DateTimeField()
    source_url = models.TextField()
    final_url = models.TextField()
    viewport_width = models.PositiveIntegerField()
    viewport_height = models.PositiveIntegerField()
    recipe_version = models.CharField(max_length=32, default="1")
    capture_epoch = models.PositiveBigIntegerField()
    content_warnings = models.JSONField(default=list)

    class Meta:
        indexes = [models.Index(fields=["bookmark", "variant", "captured_at"])]


class CaptureState(models.Model):
    bookmark = models.ForeignKey(Bookmark, on_delete=models.CASCADE, related_name="capture_states")
    variant = models.CharField(max_length=10)
    current_version = models.ForeignKey(SnapshotVersion, null=True, on_delete=models.SET_NULL, related_name="current_for")
    last_attempt_at = models.DateTimeField(null=True)
    last_success_at = models.DateTimeField(null=True)
    next_due_at = models.DateTimeField(null=True)
    failure_count = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["bookmark", "variant"], name="capture_variant_unique")]


class LibraryIdempotency(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    owner_key = models.CharField(max_length=32, default="owner")
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=200)
    key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    generation = models.CharField(max_length=64)
    library_epoch = models.PositiveBigIntegerField()
    job_id = models.UUIDField(null=True)
    group_id = models.UUIDField(null=True)
    state = models.CharField(max_length=24, default="completed")
    response_status = models.PositiveSmallIntegerField(default=200)
    response_body = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner_key", "method", "path", "key"], name="library_idempotency_unique")]


class PageMetadata(models.Model):
    bookmark = models.OneToOneField(Bookmark, on_delete=models.CASCADE, primary_key=True)
    title = models.CharField(max_length=500, blank=True)
    description = models.TextField(blank=True)
    text_excerpt = models.TextField(blank=True)
    fetched_at = models.DateTimeField()
    source_url = models.TextField()
    content_hash = models.CharField(max_length=64)


class ClassificationBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    job_group = models.ForeignKey(JobGroup, null=True, on_delete=models.SET_NULL)
    profile = models.ForeignKey(AIProfile, null=True, on_delete=models.SET_NULL)
    tree_revision = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class ClassificationTaxonomy(models.Model):
    """One durable vocabulary shared by every classification batch in a library."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    categories = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)


class ClassificationProposal(Entity):
    batch = models.ForeignKey(ClassificationBatch, on_delete=models.CASCADE, related_name="proposals")
    bookmark = models.ForeignKey(Bookmark, on_delete=models.CASCADE, related_name="proposals")
    expected_classification_revision = models.PositiveBigIntegerField()
    expected_tree_revision = models.PositiveBigIntegerField()
    target_folder = models.ForeignKey(Folder, null=True, on_delete=models.SET_NULL)
    target_path = models.JSONField(default=dict)
    reason = models.CharField(max_length=1000, blank=True)
    status = models.CharField(max_length=24, default="pending", db_index=True)


class ImportBatch(models.Model):
    confirmation_digest = models.CharField(max_length=64, blank=True)
    execution_authorization = models.TextField(blank=True)
    capture_budget = models.PositiveIntegerField(default=0)
    ai_budget = models.PositiveIntegerField(default=0)
    ai_requests_used = models.PositiveIntegerField(default=0)
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    state = models.CharField(max_length=24, default="preview")
    source_type = models.CharField(max_length=24, default="browser")
    total = models.PositiveIntegerField(default=0)
    valid = models.PositiveIntegerField(default=0)
    duplicate = models.PositiveIntegerField(default=0)
    invalid = models.PositiveIntegerField(default=0)
    stage = models.CharField(max_length=24, default="records")
    capture_group = models.ForeignKey(JobGroup, null=True, on_delete=models.SET_NULL, related_name="capture_imports")
    classification_group = models.ForeignKey(JobGroup, null=True, on_delete=models.SET_NULL, related_name="classification_imports")
    ai_status = models.CharField(max_length=24, default="pending")
    ai_error_code = models.CharField(max_length=64, blank=True)
    review_notified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class ImportEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="entries")
    url = models.TextField()
    title = models.CharField(max_length=500, blank=True)
    original_folder_path = models.JSONField(default=list)
    status = models.CharField(max_length=24, default="pending")
    bookmark = models.ForeignKey(Bookmark, null=True, on_delete=models.SET_NULL)
    error_code = models.CharField(max_length=64, blank=True)


class BookmarkSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    bookmark = models.ForeignKey(Bookmark, on_delete=models.CASCADE, related_name="sources")
    import_batch = models.ForeignKey(ImportBatch, null=True, on_delete=models.SET_NULL)
    source_folder_path = models.JSONField(default=list)
    source_title = models.CharField(max_length=500, blank=True)


class ExtensionSubmission(Entity):
    """Durable authority to automatically file one newly submitted bookmark."""
    bookmark = models.ForeignKey(Bookmark, null=True, on_delete=models.SET_NULL)
    created = models.BooleanField(default=True)
    source = models.CharField(max_length=24, default="firefox-extension")
    state = models.CharField(max_length=24, default="queued", db_index=True)
    generation = models.CharField(max_length=64)
    library_epoch = models.PositiveBigIntegerField()
    expected_bookmark_revision = models.PositiveBigIntegerField(default=1)
    expected_classification_revision = models.PositiveBigIntegerField(default=1)
    expected_tree_revision = models.PositiveBigIntegerField(default=1)
    group = models.ForeignKey(JobGroup, null=True, on_delete=models.SET_NULL)
    metadata_job = models.ForeignKey(Job, null=True, on_delete=models.SET_NULL, related_name="metadata_submissions")
    classification_job = models.ForeignKey(Job, null=True, on_delete=models.SET_NULL, related_name="classification_submissions")
    proposal = models.ForeignKey(ClassificationProposal, null=True, on_delete=models.SET_NULL)
    error_code = models.CharField(max_length=64, blank=True)
    metadata_error_code = models.CharField(max_length=64, blank=True)
    applied_at = models.DateTimeField(null=True)
