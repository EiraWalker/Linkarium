from django.db import migrations, models


def retain_unused_initial_codes(apps, schema_editor):
    if schema_editor.connection.alias != "default":
        return
    apps.get_model("runtime", "EnrollmentGrant").objects.using("default").filter(
        purpose__in=("bootstrap", "first_passkey"),
        active_slot__in=("bootstrap", "first_passkey"),
        consumed_at__isnull=True, invalidated_at__isnull=True,
    ).update(expires_at=None)


def restore_initial_deadlines(apps, schema_editor):
    if schema_editor.connection.alias != "default":
        return
    from datetime import timedelta
    for grant in apps.get_model("runtime", "EnrollmentGrant").objects.using("default").filter(expires_at__isnull=True):
        grant.expires_at = grant.created_at + timedelta(minutes=10)
        grant.save(using="default", update_fields=["expires_at"])


class Migration(migrations.Migration):
    dependencies = [("runtime", "0003_passkey_enrollment")]

    operations = [
        migrations.AlterField(model_name="enrollmentgrant", name="expires_at", field=models.DateTimeField(null=True)),
        migrations.RunPython(retain_unused_initial_codes, restore_initial_deadlines),
    ]
