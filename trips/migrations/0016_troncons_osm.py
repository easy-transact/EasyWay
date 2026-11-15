from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('trips', '0015_aretes_stables'),
    ]

    operations = [
        migrations.AddField(
            model_name='echantillonvitesse', name='noeud_debut_osm',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='echantillonvitesse', name='noeud_fin_osm',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='echantillonvitesse',
            index=models.Index(
                fields=['jour_semaine', 'heure_jour', 'noeud_debut_osm', 'noeud_fin_osm'],
                name='echantillon_troncon_idx',
            ),
        ),
        migrations.AddField(
            model_name='correspondancearete', name='noeud_debut_osm',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='correspondancearete', name='noeud_fin_osm',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='correspondancearete',
            index=models.Index(fields=['noeud_debut_osm', 'noeud_fin_osm'], name='correspondance_troncon_idx'),
        ),
    ]
