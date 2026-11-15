"""
python manage.py exporter_trafic_predit <dossier>

Ecrit les profils de trafic prevu (cf. trips/services/trafic_predit.py) au
format de `valhalla_add_predicted_traffic`. Procedure complete (serveur) :
voir docker/TRAFIC_PREDIT.md.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from trips.services import trafic_predit
from trips.services.tuiles_trafic import TraficIndisponible


class Command(BaseCommand):
    help = "Exporte les profils de trafic prevu pour valhalla_add_predicted_traffic."

    def add_arguments(self, parser):
        parser.add_argument('dossier', help='Dossier de sortie (un CSV par tuile).')
        parser.add_argument(
            '--traffic-tar', default=None,
            help='Chemin de traffic.tar (arborescence des tuiles). Defaut : TRAFIC_TAR_PATH.',
        )

    def handle(self, *args, dossier, traffic_tar=None, **options):
        chemin = traffic_tar or settings.TRAFIC_TAR_PATH
        if not chemin:
            raise CommandError('TRAFIC_TAR_PATH vide : passer --traffic-tar.')
        try:
            resultat = trafic_predit.exporter(chemin, dossier)
        except TraficIndisponible as exc:
            raise CommandError(f'traffic.tar illisible : {exc}') from exc
        self.stdout.write(self.style.SUCCESS(
            f"{resultat['aretes']} aretes exportees dans {resultat['tuiles']} tuiles ({dossier})."
        ))
