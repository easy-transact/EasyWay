"""
Importe le referentiel de villes/villages du Cameroun (modele Ville) depuis
la couche gis_osm_places_free d'un export GeoPackage Geofabrik -- pas
l'extrait .osm.pbf utilise par seed_places/Valhalla, cette couche
pre-agregee n'existe que dans l'export GIS
(download.geofabrik.de/africa/cameroon-latest-free.gpkg.zip).

Passe par ogr2ogr (GDAL, deja present sur la machine) pour convertir la
couche en CSV avec X/Y en colonnes plutot que de decoder le binaire
GeoPackage (GPB) a la main -- ogr2ogr est deja l'outil standard pour ca,
pas de raison de reimplementer un parseur.

Usage :
    python manage.py import_villes_gpkg chemin/vers/cameroon.gpkg
"""

import csv
import subprocess
import tempfile

from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from places.models import Ville
from places.utils import normaliser

COUCHE = 'gis_osm_places_free'


class Command(BaseCommand):
    help = "Importe le referentiel de villes/villages depuis la couche gis_osm_places_free d'un .gpkg Geofabrik."

    def add_arguments(self, parser):
        parser.add_argument('gpkg', type=str, help='Chemin vers le fichier .gpkg')

    def handle(self, *args, **options):
        chemin = options['gpkg']

        with tempfile.NamedTemporaryFile(suffix='.csv') as tmp:
            resultat = subprocess.run(
                ['ogr2ogr', '-f', 'CSV', tmp.name, chemin, COUCHE, '-lco', 'GEOMETRY=AS_XY'],
                capture_output=True, text=True,
            )
            if resultat.returncode != 0:
                raise CommandError(f"ogr2ogr a echoue : {resultat.stderr}")

            with open(tmp.name, newline='', encoding='utf-8') as f:
                lignes = list(csv.DictReader(f))

        importees = 0
        with transaction.atomic():
            for ligne in lignes:
                nom = ligne['name'].strip()
                if not nom:
                    continue
                Ville.objects.update_or_create(
                    osm_id=int(ligne['osm_id']),
                    defaults={
                        'nom': nom,
                        'nom_normalise': normaliser(nom),
                        'type': ligne['fclass'],
                        'population': int(ligne['population'] or 0),
                        'position': Point(float(ligne['X']), float(ligne['Y']), srid=4326),
                    },
                )
                importees += 1

        self.stdout.write(self.style.SUCCESS(
            f"{importees} villes importees/mises a jour sur {len(lignes)} lignes (sans nom ignorees)."
        ))
