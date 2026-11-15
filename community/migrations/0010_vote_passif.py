from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('community', '0009_incident_note'),
    ]

    operations = [
        migrations.AddField(
            model_name='vote',
            name='passif',
            field=models.BooleanField(default=False),
        ),
    ]
