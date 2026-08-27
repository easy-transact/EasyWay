"""
Backfill de Incident.ville/ville_normalisee pour les signalements crees avant
l'ajout du champ (cf. GET /api/incidents/city/, ServiceIncident._geocoder_inverse) --
repasse chaque incident sans ville par ClientNominatim().inverser(), le meme
appel qu'a la creation. Volontairement pas limite aux incidents encore actifs/
non expires : au moment d'ecrire ceci, aucun des incidents sans ville ne l'est
(tous crees avant ce champ, donc plus vieux) -- se limiter aux "visibles"
laisserait le backfill ne rien faire.

Usage :
    python manage.py backfill_ville_incidents
    python manage.py backfill_ville_incidents --dry-run
"""

import time

from django.core.management.base import BaseCommand

from places.services.client_nominatim import ClientNominatim
from places.utils import normaliser

from ...models import Incident

DELAI_ENTRE_APPELS_S = 1  # courtoisie envers Nominatim -- un backfill n'est pas urgent.


class Command(BaseCommand):
    help = "Remplit ville/ville_normalisee des incidents crees avant l'ajout du champ (reverse-geocodage Nominatim)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true', help="Affiche ce qui serait fait sans ecrire en base."
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        incidents = list(Incident.objects.filter(ville='').order_by('cree_le'))
        total = len(incidents)
        if total == 0:
            self.stdout.write(self.style.SUCCESS("Aucun incident sans ville -- rien a faire."))
            return

        self.stdout.write(f"{total} incident(s) sans ville a traiter...")
        remplis, ignores = 0, 0

        for i, incident in enumerate(incidents, start=1):
            resultat = ClientNominatim().inverser(incident.position.y, incident.position.x)
            ville = resultat['city'] if resultat else ''

            if not ville:
                ignores += 1
                self.stdout.write(f"  [{i}/{total}] {incident.id} -- pas de ville trouvee, ignore.")
            else:
                if not dry_run:
                    incident.ville = ville
                    incident.ville_normalisee = normaliser(ville)
                    incident.save(update_fields=['ville', 'ville_normalisee'])
                remplis += 1
                self.stdout.write(f"  [{i}/{total}] {incident.id} -- ville={ville!r}")

            if i < total:
                time.sleep(DELAI_ENTRE_APPELS_S)

        prefixe = '[dry-run] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f"{prefixe}{remplis} rempli(s), {ignores} sans ville trouvee/Nominatim indisponible, sur {total}."
        ))
