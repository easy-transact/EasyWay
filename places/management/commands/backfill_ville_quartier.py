"""
Renseigne ville/quartier pour les Lieu importes sans addr:city/addr:suburb
(cf. seed_places.py -- beaucoup de POI OSM au Cameroun n'ont pas ces tags),
via une reverse-geocode Nominatim par position (cf. ClientNominatim.inverser_ville_quartier).

Usage :
    python manage.py backfill_ville_quartier
    python manage.py backfill_ville_quartier --dry-run
    python manage.py backfill_ville_quartier --limit 50
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from places.models import Lieu
from places.services.client_nominatim import ClientNominatim

TAILLE_LOT = 200


class Command(BaseCommand):
    help = "Renseigne ville/quartier des Lieu vides via reverse-geocode Nominatim (position)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Ne rien ecrire, juste afficher.')
        parser.add_argument('--limit', type=int, default=None, help='Limiter le nombre de lieux traites.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        limite = options['limit']

        lieux = Lieu.objects.filter(
            Q(ville='') | Q(ville__isnull=True) | Q(quartier__isnull=True) | Q(quartier='')
        ).order_by('nom')
        if limite:
            lieux = lieux[:limite]

        total = lieux.count()
        self.stdout.write(f"{total} lieu(x) avec ville/quartier manquant(s).")
        if dry_run:
            self.stdout.write('--dry-run : aucune ecriture.')

        client = ClientNominatim()
        traites = 0
        maj = 0
        sans_resultat = 0

        for lieu in lieux.iterator():
            traites += 1
            resultat = client.inverser_ville_quartier(lieu.position.y, lieu.position.x)
            if resultat is None:
                sans_resultat += 1
                self.stdout.write(f"  [{traites}/{total}] {lieu.nom} : pas de resultat Nominatim.")
                continue

            champs_maj = []
            if not lieu.ville and resultat['ville']:
                lieu.ville = resultat['ville']
                champs_maj.append('ville')
            if not lieu.quartier and resultat['quartier']:
                lieu.quartier = resultat['quartier']
                champs_maj.append('quartier')

            if champs_maj:
                maj += 1
                self.stdout.write(
                    f"  [{traites}/{total}] {lieu.nom} : {', '.join(f'{c}={getattr(lieu, c)}' for c in champs_maj)}"
                )
                if not dry_run:
                    lieu.save(update_fields=champs_maj)
            else:
                self.stdout.write(f"  [{traites}/{total}] {lieu.nom} : rien de nouveau depuis Nominatim.")

        self.stdout.write(
            self.style.SUCCESS(
                f"Termine : {traites} traites, {maj} mis a jour, {sans_resultat} sans resultat Nominatim."
            )
        )
