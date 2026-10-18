from django.db import migrations, models
from django.db.models import Q
from django.utils import timezone


def require_legacy_confirmation(apps, schema_editor):
    alias = schema_editor.connection.alias
    Batch = apps.get_model('library', 'ImportBatch')
    Job = apps.get_model('library', 'Job')
    Attempt = apps.get_model('library', 'JobAttempt')
    for batch in Batch.objects.using(alias).all().iterator(chunk_size=100):
        groups = [value for value in (batch.capture_group_id, batch.classification_group_id) if value]
        # A partially imported batch can already be failed/cancelled while its
        # capture or classification children are still queued. Payload ownership
        # also covers planning jobs that have not acquired group membership yet.
        group_ids = [str(value) for value in groups]
        jobs = Job.objects.using(alias).filter(
            Q(kind='import', target_id=batch.id) | Q(memberships__group_id__in=groups) |
            Q(payload__import_id__in=(str(batch.id), batch.id.hex)) | Q(payload__group_id__in=group_ids),
            state__in=('queued', 'running', 'retry_wait'))
        had_active_work = jobs.exists()
        now = timezone.now()
        Attempt.objects.using(alias).filter(job_id__in=jobs.values('id'), state='running').update(
            state='cancelled', error_code='IMPORT_CONFIRMATION_REQUIRED', finished_at=now, lease_expires_at=now)
        jobs.update(state='cancelled', active_key=None, active_attempt_id=None,
            error_code='IMPORT_CONFIRMATION_REQUIRED', updated_at=now)
        if batch.state not in ('queued', 'running') and not (had_active_work and batch.state in (
                'failed', 'cancelled', 'waiting_tasks', 'classifying')):
            continue
        # Imported rows, saved pages and existing proposals remain intact. The
        # remaining work becomes a visible preview instead of silently failing.
        Batch.objects.using(alias).filter(pk=batch.pk).update(state='previewed', stage='records',
            capture_group_id=None, classification_group_id=None, ai_status='pending',
            ai_error_code='IMPORT_CONFIRMATION_REQUIRED')


class Migration(migrations.Migration):
    dependencies = [('library', '0003_classificationtaxonomy_folder_created_by_ai')]
    operations = [
        migrations.AddField('importbatch', 'confirmation_digest', models.CharField(blank=True, max_length=64)),
        migrations.AddField('importbatch', 'execution_authorization', models.TextField(blank=True)),
        migrations.AddField('importbatch', 'capture_budget', models.PositiveIntegerField(default=0)),
        migrations.AddField('importbatch', 'ai_budget', models.PositiveIntegerField(default=0)),
        migrations.AddField('importbatch', 'ai_requests_used', models.PositiveIntegerField(default=0)),
        migrations.RunPython(require_legacy_confirmation, migrations.RunPython.noop),
    ]
