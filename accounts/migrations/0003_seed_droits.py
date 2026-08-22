from django.db import migrations


def seed_droits(apps, schema_editor):
    Droits = apps.get_model('accounts', 'Droits')
    Droits.objects.update_or_create(
        formule='GRATUITE',
        defaults=dict(
            max_adresses_enregistrees=5,
            publicite_active=True,
            routage_avance=False,
            packs_hors_ligne=False,
            retention_historique_jours=30,
        ),
    )
    Droits.objects.update_or_create(
        formule='PREMIUM',
        defaults=dict(
            max_adresses_enregistrees=None,
            publicite_active=False,
            routage_avance=True,
            packs_hors_ligne=True,
            retention_historique_jours=365,
        ),
    )


def unseed_droits(apps, schema_editor):
    Droits = apps.get_model('accounts', 'Droits')
    Droits.objects.filter(formule__in=['GRATUITE', 'PREMIUM']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_remove_droits_id_remove_droits_utilisateur_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_droits, unseed_droits),
    ]
