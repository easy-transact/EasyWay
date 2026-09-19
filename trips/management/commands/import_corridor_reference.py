"""
Importe un CorridorReference depuis un export GeoJSON externe de confiance
(ex. Google Maps Directions, cf. discussion Douala-Bafoussam) -- utilise par
ClientCorridorReference pour corriger la portion longue-distance d'un trajet
reel dont l'origine et la destination correspondent a un corridor connu.

Le fichier attendu est une FeatureCollection de LineString (une par
alternative de route, cf. export Google) ; --feature-index choisit laquelle
utiliser -- prendre celle dont le trace correspond a ce que Valhalla calcule
reellement pour ce trajet (verifie au prealable), pas forcement la premiere.

Usage :
    python manage.py import_corridor_reference chemin/vers/route.geojson \
        --nom "Douala - Bafoussam" --ville-depart Douala --ville-arrivee Bafoussam \
        --feature-index 1 --voies N3,N5
"""

import json

from django.contrib.gis.geos import LineString, Point
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from trips.models import CorridorReference
from trips.services.client_valhalla import ClientValhalla

OPTIONS_CAPTURE = {'costing': 'auto', 'costing_options': {'auto': {}}}


class Command(BaseCommand):
    help = "Importe un CorridorReference depuis un export GeoJSON externe de confiance."

    def add_arguments(self, parser):
        parser.add_argument('geojson', type=str, help='Chemin vers le fichier GeoJSON.')
        parser.add_argument('--nom', type=str, required=True, help='Identifiant unique du corridor.')
        parser.add_argument('--ville-depart', type=str, required=True)
        parser.add_argument('--ville-arrivee', type=str, required=True)
        parser.add_argument(
            '--feature-index', type=int, default=0,
            help='Index de la Feature (LineString) a utiliser dans la FeatureCollection.',
        )
        parser.add_argument(
            '--rayon-m', type=int, default=25000,
            help='Rayon de matching (m) autour de chaque ancrage (defaut 25000).',
        )
        parser.add_argument(
            '--voies', type=str, default='',
            help='Libelles de voies separes par des virgules (ex. N3,N5), pour la classification road_class.',
        )
        parser.add_argument('--source', type=str, default='google_maps')
        parser.add_argument(
            '--sans-capture', action='store_true',
            help="N'appelle pas Valhalla pour capturer les manoeuvres reelles (corridor sans turn-by-turn).",
        )

    def handle(self, *args, **options):
        with open(options['geojson'], encoding='utf-8') as f:
            donnees = json.load(f)

        try:
            feature = donnees['features'][options['feature_index']]
            coordonnees = feature['geometry']['coordinates']
            proprietes = feature.get('properties', {})
        except (KeyError, IndexError, TypeError) as exc:
            raise CommandError(f"Feature GeoJSON invalide ou introuvable : {exc}") from exc

        if len(coordonnees) < 2:
            raise CommandError('La geometrie doit contenir au moins 2 points.')

        try:
            distance_m = int(proprietes['distance_m'])
            duree_s = int(proprietes['duration_s'])
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError(
                f"Proprietes 'distance_m'/'duration_s' manquantes ou invalides sur la feature : {exc}"
            ) from exc

        ligne = LineString(coordonnees, srid=4326)
        ancrage_depart = Point(coordonnees[0], srid=4326)
        ancrage_arrivee = Point(coordonnees[-1], srid=4326)

        # Precalcule une fois la position normalisee de chaque sommet le long
        # de la ligne -- evite de rappeler GEOS a chaque requete pour trouver
        # la sous-portion utilisee par un trajet partiel (cf. ClientCorridorReference).
        fractions_sommets = [ligne.project_normalized(Point(c, srid=4326)) for c in coordonnees]

        manoeuvres_reference = []
        if not options['sans_capture']:
            manoeuvres_reference = self._capturer_manoeuvres(ancrage_depart, ancrage_arrivee)

        libelles_voies = [v.strip() for v in options['voies'].split(',') if v.strip()]

        with transaction.atomic():
            corridor, cree = CorridorReference.objects.update_or_create(
                nom=options['nom'],
                defaults={
                    'ville_depart': options['ville_depart'],
                    'ville_arrivee': options['ville_arrivee'],
                    'ancrage_depart': ancrage_depart,
                    'ancrage_arrivee': ancrage_arrivee,
                    'rayon_ancrage_m': options['rayon_m'],
                    'geometrie': ligne,
                    'fractions_sommets': fractions_sommets,
                    'distance_m': distance_m,
                    'duree_s': duree_s,
                    'libelles_voies': libelles_voies,
                    'manoeuvres_reference': manoeuvres_reference,
                    'source': options['source'],
                    'actif': True,
                },
            )

        verbe = 'cree' if cree else 'mis a jour'
        avertissement = '' if manoeuvres_reference else ' (SANS manoeuvres reelles -- Valhalla injoignable ou --sans-capture)'
        self.stdout.write(self.style.SUCCESS(
            f"Corridor '{corridor.nom}' {verbe} : {distance_m/1000:.1f}km, "
            f"{duree_s/60:.0f}min, {len(coordonnees)} sommets{avertissement}."
        ))

    def _capturer_manoeuvres(self, ancrage_depart, ancrage_arrivee):
        """Best-effort : capture les manoeuvres Valhalla reelles entre les
        deux ancrages pour le turn-by-turn du cas 'usage complet, sens aller'
        (cf. ClientCorridorReference). Bypass ServiceItineraire (pas de
        contexte requete/utilisateur.parametres dans une commande) --
        appelle ClientValhalla directement avec un costing par defaut."""
        try:
            trips = ClientValhalla().calculer_itineraires(
                (ancrage_depart.y, ancrage_depart.x), (ancrage_arrivee.y, ancrage_arrivee.x),
                OPTIONS_CAPTURE, alternatives=False,
            )
        except Exception as exc:
            self.stderr.write(self.style.WARNING(f"Capture des manoeuvres Valhalla echouee : {exc}"))
            return []

        if not trips or trips[0].get('degrade'):
            self.stderr.write(self.style.WARNING('Valhalla indisponible (repli degrade) -- pas de manoeuvres capturees.'))
            return []

        return [m for leg in trips[0]['legs'] for m in leg['maneuvers']]
