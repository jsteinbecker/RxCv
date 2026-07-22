from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('rxocrpl', '0016_alter_listedingredient_unit'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='auth_user',
            field=models.OneToOneField(
                blank=True,
                help_text='Linked Django auth account',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='facility_profile',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name='rolegrant',
            name='reason',
            field=models.TextField(
                blank=True,
                help_text="Optional. Must be 'system_bootstrap' when granted_by is null.",
            ),
        ),
    ]
