"""
Recalcule Lieu.position_approximative (cf. places/services/positions.py) :
positions partagees par plusieurs lieux = centre du quartier, pas l'adresse.
Fait automatiquement par la migration 0010 et apres import_lieux_csv ; a
relancer si des lieux sont ajoutes autrement.

Usage :
    python manage.py marquer_positions_approximatives
"""

from django.core.management.base import BaseCommand

from places.services.positions import SEUIL_DOUBLONS, marquer_positions_approximatives


class Command(BaseCommand):
    help = 'Marque les lieux dont la position est partagee (centre du quartier).'

    def handle(self, *args, **options):
        nombre = marquer_positions_approximatives()
        self.stdout.write(self.style.SUCCESS(
            f'{nombre} lieu(x) a position approximative (au moins {SEUIL_DOUBLONS} lieux au meme point).'
        ))
