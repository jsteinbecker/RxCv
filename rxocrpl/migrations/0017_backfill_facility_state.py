from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Backfill fix: `Facility` was added to models.py without ever being
    committed via `CreateModel` (checked-in migration history jumps straight
    to AddField/AlterField on it in 0012). This retroactively adds the
    missing `CreateModel`.

    On a fresh database this creates the table normally. On a pre-existing
    dev database where `rxocrpl_facility` was already created out-of-band,
    apply this one migration with `--fake` before running the rest:
        manage.py migrate rxocrpl 0017 --fake
        manage.py migrate
    """

    dependencies = [
        ('rxocrpl', '0007_labeler_product_labeler'),
    ]

    operations = [
        migrations.CreateModel(
            name='Facility',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('facility_type', models.CharField(blank=True, max_length=100, null=True)),
                ('org', models.CharField(blank=True, max_length=255, null=True)),
                ('admin_id', models.CharField(blank=True, max_length=100, null=True)),
            ],
            options={
                'verbose_name_plural': 'Facilities',
            },
        ),
    ]
