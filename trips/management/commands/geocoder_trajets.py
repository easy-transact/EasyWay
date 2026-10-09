import time

from django.core.management.base import BaseCommand
from django.db.models import Q

from places.services.client_nominatim import ClientNominatim
from trips.geocodage import LIBELLES_GENERIQUES, geocoder_trajet
from trips.models import Trajet


class Command(BaseCommand):
    help = (
        'Remplace les libelles generiques ("Votre position"...) des trajets existants '
        'par le nom reel du lieu (geocodage inverse Nominatim).'
    )

    def add_arguments(self, parser):
        parser.add_argument('--limite', type=int, default=None, help='Nombre maximum de trajets a traiter.')
        parser.add_argument('--pause', type=float, default=0.2, help='Secondes entre deux trajets (menage Nominatim).')

    def handle(self, *args, limite=None, pause=0.2, **options):
        filtre = Q()
        for libelle in LIBELLES_GENERIQUES:
            filtre |= Q(libelle_origine__iexact=libelle) | Q(libelle_destination__iexact=libelle)
        trajets = Trajet.objects.filter(filtre).order_by('-demarre_le')
        if limite:
            trajets = trajets[:limite]

        client = ClientNominatim()
        traites = modifies = 0
        for trajet in trajets.iterator():
            traites += 1
            if geocoder_trajet(trajet, client):
                modifies += 1
            time.sleep(pause)
        self.stdout.write(f'{modifies} trajets renommes sur {traites} examines.')
