from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rxocrpl', '0016_alter_listedingredient_unit'),
    ]

    operations = [
        migrations.AlterField(
            model_name='rolegrant',
            name='reason',
            field=models.TextField(
                blank=True,
                help_text="Optional. Must be 'system_bootstrap' when granted_by is null.",
            ),
        ),
    ]
