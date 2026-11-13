from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('community', '0010_vote_passif'),
    ]

    operations = [
        migrations.AddField(
            model_name='incident', name='noeud_debut_osm',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='incident', name='noeud_fin_osm',
            field=models.BigIntegerField(blank=True, null=True),
        ),
    ]
