from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("runtime", "0004_initial_passkey_no_expiry")]
    operations = [migrations.CreateModel(name="AuthRequestBucket", fields=[
        ("key", models.CharField(max_length=64, primary_key=True, serialize=False)),
        ("window_start", models.DateTimeField(db_index=True)),
        ("options_count", models.PositiveSmallIntegerField(default=0)),
        ("verify_count", models.PositiveSmallIntegerField(default=0)),
    ])]
