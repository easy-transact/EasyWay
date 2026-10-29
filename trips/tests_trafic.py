"""Trafic dans Valhalla (spec TRAFIC_VALHALLA) : embouteillage signale etendu
en amont, filtres des vitesses avant agregation."""

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from accounts.tests import creer_utilisateur
from community.models import Incident, StatutIncident

from .models import StatutTrajet, Trajet
from .services import trafic_temps_reel
from .services.consommateur_positions import ConsommateurPositions
from .services.producteur_evenements import FLUX_POSITIONS


class EmbouteillageAmontTests(TestCase):
    def setUp(self):
        cache.clear()
        self.auteur = creer_utilisateur()

    def signaler(self, cap):
        return Incident.objects.create(
            auteur=self.auteur, type='EMBOUTEILLAGE', sous_type='A_L_ARRET', position=Point(9.70, 4.05, srid=4326),
            cap=cap, statut=StatutIncident.ACTIF, expire_le=timezone.now() + timedelta(minutes=30),
        )

    def test_aretes_des_200_m_en_amont_ralenties(self):
        self.signaler(cap=90)  # vers l'est : la file est a l'ouest
        with patch('trips.services.client_meili.tracer', return_value=([{'id': 1}, {'id': 2}, {'id': 3}], [])) as tracer:
            vitesses = trafic_temps_reel.vitesses_signalements()
        self.assertEqual(vitesses, {1: 5, 2: 5, 3: 5})
        points = tracer.call_args.args[0]
        self.assertEqual(len(points), 6)  # 200, 160, ..., 0 m
        self.assertLess(points[0]['lon'], points[-1]['lon'])  # on part de l'ouest (amont)
        self.assertAlmostEqual(points[-1]['lon'], 9.70, places=6)

    def test_sans_cap_seule_l_arete_du_signalement(self):
        self.signaler(cap=None)
        with patch('trips.services.client_meili.tracer') as tracer, \
                patch('trips.services.client_locate.localiser', return_value={'graph_id': 7}):
            vitesses = trafic_temps_reel.vitesses_signalements()
        tracer.assert_not_called()
        self.assertEqual(vitesses, {7: 5})

    def test_recalage_impossible_repli_sur_l_arete(self):
        from trips.services.client_meili import ErreurMeili

        self.signaler(cap=90)
        with patch('trips.services.client_meili.tracer', side_effect=ErreurMeili('panne')), \
                patch('trips.services.client_locate.localiser', return_value={'graph_id': 7}):
            self.assertEqual(trafic_temps_reel.vitesses_signalements(), {7: 5})


def _message(trajet_id, lat, lon, horodatage):
    return {'donnees': json.dumps({
        'trajet_id': trajet_id, 'lat': lat, 'lon': lon, 'vitesse_kmh': None, 'cap': None, 'horodatage': horodatage,
    })}


class FiltresVitessesTests(TestCase):
    def consommer(self, aretes, trajet_id='t1', positions=((4.05, 9.70), (4.06, 9.71))):
        connexion = MagicMock()
        connexion.xgroup_create.return_value = True
        connexion.xautoclaim.return_value = ('0-0', [], [])
        connexion.xreadgroup.return_value = [(FLUX_POSITIONS, [
            ('1-1', _message(trajet_id, *positions[0], '2026-01-01T10:00:00+00:00')),
            ('1-2', _message(trajet_id, *positions[1], '2026-01-01T10:01:00+00:00')),
        ])]
        consommateur = ConsommateurPositions(connexion=connexion, nom_consommateur='test')
        points = [{'edge_index': 0, 'distance_along_edge': 0.0}, {'edge_index': 0, 'distance_along_edge': 1.0}]
        with patch('trips.services.consommateur_positions.tracer', return_value=(aretes, points)), \
                patch('trips.services.consommateur_positions.enregistrer_passages'):
            consommateur.consommer()
        pipe = connexion.pipeline.return_value.__enter__.return_value
        return pipe.hincrbyfloat.call_count > 0

    def test_vitesse_aberrante_selon_la_classe_de_route(self):
        # 1,6 km en 60 s = 96 km/h : aberrant en ville (> 90), plausible sur une nationale (< 135).
        self.assertFalse(self.consommer([{'id': 1, 'length': 1.6, 'road_class': 'residential'}]))
        self.assertTrue(self.consommer([{'id': 1, 'length': 1.6, 'road_class': 'trunk'}]))
        self.assertTrue(self.consommer([{'id': 1, 'length': 1.6}]))  # classe inconnue : plafond 180

    def test_depart_et_arrivee_ignores(self):
        trajet = Trajet.objects.create(
            utilisateur=creer_utilisateur(), position_origine='POINT(9.70 4.05)', libelle_origine='A',
            position_destination='POINT(9.90 4.20)', libelle_destination='B', itineraire_choisi='x',
            distance_prevue=1000, duree_prevue=100, statut=StatutTrajet.ACTIF,
        )
        arete = [{'id': 1, 'length': 0.5, 'road_class': 'primary'}]
        self.assertFalse(self.consommer(arete, str(trajet.id)))  # premiere position au point de depart
        self.assertTrue(self.consommer(arete, str(trajet.id), positions=((4.10, 9.80), (4.11, 9.81))))

    def test_motos_exclues_des_vitesses(self):
        trajet = Trajet.objects.create(
            utilisateur=creer_utilisateur(), position_origine='POINT(9.70 4.05)', libelle_origine='A',
            position_destination='POINT(9.90 4.20)', libelle_destination='B', itineraire_choisi='x',
            distance_prevue=1000, duree_prevue=100, statut=StatutTrajet.ACTIF, type_vehicule='MOTO',
        )
        arete = [{'id': 1, 'length': 0.5, 'road_class': 'primary'}]
        self.assertFalse(self.consommer(arete, str(trajet.id), positions=((4.10, 9.80), (4.11, 9.81))))
