"""Correction des durees en ville (trips/services/duree_ville.py)."""

from datetime import datetime
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.tests import connecter, creer_utilisateur

from .models import FUSEAU_TRAFIC
from .polyline import encoder_polyline6
from .services import duree_ville

LUNDI = datetime(2026, 10, 26, tzinfo=FUSEAU_TRAFIC)
# Akwa -> Bonateki (Douala) et un trajet en rase campagne (vers Edea).
VILLE = [(9.695, 4.050), (9.714, 4.070)]
CAMPAGNE = [(10.10, 3.80), (10.13, 3.80)]


def candidat(points, duree=200, duree_avec_trafic=None):
    return {
        'geometrie': encoder_polyline6(points), 'duree': duree, 'duree_avec_trafic': duree_avec_trafic,
        'degrade': False,
        'manoeuvres': [{'type': '1', 'distance': 1600, 'duree': duree // 2}, {'type': '4', 'distance': 1600, 'duree': duree // 2}],
    }


@override_settings(CORRECTION_DUREE_VILLE=True)
class FacteurTests(SimpleTestCase):
    def test_selon_l_heure(self):
        self.assertEqual(duree_ville.facteur(LUNDI.replace(hour=7, minute=30)), duree_ville.FACTEUR_POINTE)
        self.assertEqual(duree_ville.facteur(LUNDI.replace(hour=18)), duree_ville.FACTEUR_POINTE)
        self.assertEqual(duree_ville.facteur(LUNDI.replace(hour=12)), duree_ville.FACTEUR_JOURNEE)
        self.assertEqual(duree_ville.facteur(LUNDI.replace(hour=23)), duree_ville.FACTEUR_NUIT)
        dimanche_18h = LUNDI.replace(day=25, hour=18)
        self.assertEqual(duree_ville.facteur(dimanche_18h), duree_ville.FACTEUR_JOURNEE)  # pas de pointe le week-end

    def test_en_ville_seulement(self):
        c = duree_ville.corriger(candidat(VILLE), {}, LUNDI.replace(hour=12))
        self.assertEqual(c['duree'], 400)  # x 2 en journee
        self.assertEqual([m['duree'] for m in c['manoeuvres']], [200, 200])
        self.assertEqual(duree_ville.corriger(candidat(CAMPAGNE), {}, LUNDI.replace(hour=12))['duree'], 200)

    def test_partie_mesuree_pas_corrigee_deux_fois(self):
        # 300 s de trafic dont 100 s mesurees : seules les 200 s a vitesse Valhalla x 2.
        c = duree_ville.corriger(
            candidat(VILLE, duree_avec_trafic=300), {'duree_mesuree_s': 100, 'duree_valhalla_s': 200},
            LUNDI.replace(hour=12),
        )
        self.assertEqual(c['duree_avec_trafic'], 100 + 400)

    @override_settings(CORRECTION_DUREE_VILLE=False)
    def test_interrupteur(self):
        self.assertEqual(duree_ville.corriger(candidat(VILLE), {}, LUNDI.replace(hour=12))['duree'], 200)


@override_settings(CORRECTION_DUREE_VILLE=True)
class CorrectionDansLeCalculTests(TestCase):
    def calculer(self, trip):
        cache.clear()
        jetons = connecter(self.client, creer_utilisateur().telephone)
        with patch('trips.services.service_itineraire.ClientValhalla') as Client, \
                patch('trips.services.service_itineraire.service_trafic.evaluer_route',
                      return_value={'niveau_trafic': 'NORMAL', 'duree_avec_trafic': None}), \
                patch('django.utils.timezone.now', return_value=LUNDI.replace(hour=12)):
            Client.return_value.calculer_itineraires.return_value = [trip]
            return self.client.post(
                reverse('trips:calculer-itineraire'),
                {'origin_lat': 4.050, 'origin_lon': 9.695, 'destination_lat': 4.070, 'destination_lon': 9.714},
                content_type='application/json', **jetons,
            ).json()[0]

    def trip(self, **extra):
        return {
            'legs': [{'shape': encoder_polyline6(VILLE), 'maneuvers': [
                {'type': 1, 'instruction': 'Partez', 'length': 3.2, 'time': 180, 'street_names': []},
                {'type': 4, 'instruction': 'Arrivee', 'length': 0, 'time': 0, 'street_names': []},
            ]}],
            'summary': {'length': 3.2, 'time': 180}, **extra,
        }

    def test_akwa_bonateki_plus_trois_minutes(self):
        self.assertEqual(self.calculer(self.trip())['duration'], 360)

    def test_corridor_de_reference_non_corrige(self):
        self.assertEqual(self.calculer(self.trip(corridor=True))['duration'], 180)
