from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ads_admin', '0002_preferencesnotificationstaff_notificationstaff'),
    ]

    operations = [
        migrations.AlterField(
            model_name='notificationstaff',
            name='type',
            field=models.CharField(choices=[('LIEU', 'Lieu propose'), ('INCIDENT', 'Incident conteste'), ('ABUS', 'Comportement suspect'), ('ATTENTE', "Liste d'attente"), ('IMPORT', 'Import'), ('PUB', 'Publicite'), ('TRAFIC', 'Trafic temps reel')], max_length=20),
        ),
    ]
