from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('trips', '0014_index_echantillon_jour_heure'),
    ]

    operations = [
        migrations.AddField(
            model_name='echantillonvitesse',
            name='way_id',
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='echantillonvitesse',
            name='sens_avant',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='echantillonvitesse',
            index=models.Index(fields=['jour_semaine', 'heure_jour', 'way_id', 'sens_avant'], name='echantillon_creneau_voie_idx'),
        ),
        migrations.CreateModel(
            name='CorrespondanceArete',
            fields=[
                ('graph_id', models.BigIntegerField(primary_key=True, serialize=False)),
                ('way_id', models.BigIntegerField()),
                ('sens_avant', models.BooleanField()),
                ('vu_le', models.DateTimeField()),
            ],
            options={
                'db_table': 'correspondance_arete',
                'indexes': [models.Index(fields=['way_id', 'sens_avant'], name='correspondance_voie_idx')],
            },
        ),
    ]
