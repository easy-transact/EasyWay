"""
Seede la table Lieu depuis un extrait OSM (.osm.pbf) : nourrit la recherche
PostGIS/trigram (P2a) en attendant Photon/Nominatim (P2b).

Limitation connue : ne traite que les NODES OSM. La plupart des POI nommes
(amenity/shop/tourism) sont mappes en nodes, mais certains (grands batiments,
zones) sont des ways/relations et ne sont pas encore importes - necessite un
cache de positions (osmium.NodeLocationsForWays) en suivi.

Usage :
    python manage.py seed_places valhalla/custom_files/cameroon-latest.osm.pbf
    python manage.py seed_places <fichier.pbf> --dry-run
"""

import osmium
from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from places.models import Lieu, SourceLieu, StatutLieu
from places.utils import normaliser

# amenity/shop/tourism : n'importe quelle valeur, tant que le node a un nom.
# highway/place : seulement les valeurs listees (evite d'importer des
# fragments de voirie ou des lieux-dits non pertinents pour la recherche).
CLES_TOUTES_VALEURS = ('amenity', 'shop', 'tourism')
CLES_VALEURS_RESTREINTES = {
    'highway': {'bus_stop'},
    'place': {'suburb', 'neighbourhood', 'quarter', 'village', 'town', 'city'},
}

TAILLE_LOT = 500


class ImporteurLieux(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.lot = []
        self.total_vus = 0
        self.total_retenus = 0

    def _categorie(self, tags):
        for cle in CLES_TOUTES_VALEURS:
            if cle in tags:
                return tags[cle]
        for cle, valeurs in CLES_VALEURS_RESTREINTES.items():
            if tags.get(cle) in valeurs:
                return tags[cle]
        return None

    def node(self, n):
        self.total_vus += 1
        nom = n.tags.get('name')
        if not nom:
            return
        categorie = self._categorie(n.tags)
        if categorie is None:
            return
        if not n.location.valid():
            return

        adresse = ' '.join(
            filter(None, [n.tags.get('addr:housenumber'), n.tags.get('addr:street')])
        )

        self.lot.append({
            'identifiant_osm': n.id,
            'nom': nom,
            'nom_normalise': normaliser(nom),
            'categorie': categorie,
            'adresse': adresse,
            'quartier': n.tags.get('addr:suburb') or n.tags.get('addr:neighbourhood') or None,
            'ville': n.tags.get('addr:city', ''),
            'lat': n.location.lat,
            'lon': n.location.lon,
        })
        self.total_retenus += 1

        if len(self.lot) >= TAILLE_LOT:
            self.ecrire_lot()

    def ecrire_lot(self):
        if not self.lot:
            return
        with transaction.atomic():
            for entree in self.lot:
                Lieu.objects.update_or_create(
                    type_osm='NODE',
                    identifiant_osm=entree['identifiant_osm'],
                    defaults={
                        'nom': entree['nom'],
                        'nom_normalise': entree['nom_normalise'],
                        'categorie': entree['categorie'],
                        'adresse': entree['adresse'],
                        'quartier': entree['quartier'],
                        'ville': entree['ville'],
                        'position': Point(entree['lon'], entree['lat'], srid=4326),
                        'source': SourceLieu.OPENSTREETMAP,
                        'statut': StatutLieu.APPROUVE,
                    },
                )
        self.lot = []


class Command(BaseCommand):
    help = "Importe les lieux nommes (POI/quartiers) d'un extrait .osm.pbf dans la table Lieu."

    def add_arguments(self, parser):
        parser.add_argument('pbf', type=str, help='Chemin vers le fichier .osm.pbf')
        parser.add_argument(
            '--dry-run', action='store_true',
            help="Parcourt et compte sans ecrire en base."
        )

    def handle(self, *args, **options):
        chemin = options['pbf']
        importeur = ImporteurLieux()

        try:
            importeur.apply_file(chemin)
        except RuntimeError as exc:
            raise CommandError(
                f"Lecture du fichier PBF interrompue ({exc}). "
                "L'extrait est probablement tronque/corrompu -- retelechargez-le."
            )

        if options['dry_run']:
            self.stdout.write(self.style.WARNING(
                f"[dry-run] {importeur.total_retenus} lieux retenus sur {importeur.total_vus} nodes vus "
                f"({len(importeur.lot)} en attente d'ecriture, non ecrits)."
            ))
            return

        importeur.ecrire_lot()
        self.stdout.write(self.style.SUCCESS(
            f"{importeur.total_retenus} lieux importes/mis a jour sur {importeur.total_vus} nodes vus."
        ))
