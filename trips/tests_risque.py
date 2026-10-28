"""Choix de l'itineraire le plus sur (risque_itineraire) : routes/calculate
et better-route."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.gis.geos import LineString, Point
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.tests import connecter, creer_utilisateur
from community.models import Incident, SousTypeIncident as S, StatutIncident, TypeIncident as T

from .tests import _trajet_actif_pour
from .tests_meilleur_itineraire import ROUTE_SUIVIE, VARIANTE, candidat
from .tests_retards import DIRECT, NORD, trip_valhalla


def signaler(auteur, type_, sous_type, lon=9.715, lat=4.05, cap=90, **extra):
    valeurs = dict(
        auteur=auteur, type=type_, sous_type=sous_type, position=Point(lon, lat, srid=4326), cap=cap,
        statut=StatutIncident.ACTIF, score_confiance=3, expire_le=timezone.now() + timedelta(hours=2),
    )
    valeurs.update(extra)
    return Incident.objects.create(**valeurs)


@patch('trips.services.risque_itineraire.de_nuit', return_value=False)
class RisqueCalculTests(TestCase):
    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.telephone)
        self.auteur = creer_utilisateur(email='auteur@test.cm')
        patcheur_trafic = patch(
            'trips.services.service_itineraire.service_trafic.evaluer_route',
            return_value={'niveau_trafic': 'NORMAL', 'duree_avec_trafic': None},
        )
        patcheur_trafic.start()
        self.addCleanup(patcheur_trafic.stop)

    def calculer(self, trips=None, contournement=None):
        """trips : reponse Valhalla normale ; contournement : celle d'un appel
        avec exclude_locations (dangers a contourner)."""
        trips = trips or [trip_valhalla(DIRECT, 400), trip_valhalla(NORD, 520)]

        def valhalla(depart, arrivee, options, **kwargs):
            if contournement is not None and options.get('exclude_locations'):
                return contournement
            return trips

        with patch('trips.services.service_itineraire.ClientValhalla') as Client:
            Client.return_value.calculer_itineraires.side_effect = valhalla
            reponse = self.client.post(
                reverse('trips:calculer-itineraire'),
                {'origin_lat': 4.05, 'origin_lon': 9.70, 'destination_lat': 4.05, 'destination_lon': 9.73},
                content_type='application/json', **self.jetons,
            )
            self.appels = Client.return_value.calculer_itineraires.call_count
        self.assertEqual(reponse.status_code, 200, reponse.content)
        return reponse.json()

    def test_sans_danger_la_plus_rapide(self, _nuit):
        routes = self.calculer()
        self.assertEqual(routes[0]['duration'], 400)
        self.assertEqual(routes[0]['risk_level'], 'LOW')
        self.assertEqual(routes[0]['selection_reason']['code'], 'FASTEST')
        self.assertEqual(self.appels, 1)

    def test_danger_sans_retard_fait_choisir_la_route_sure(self, _nuit):
        # Conducteur a contresens : aucun retard simule, mais gravite 8.
        signaler(self.auteur, T.POLICE, S.SENS_INVERSE)
        routes = self.calculer()
        self.assertEqual(routes[0]['duration'], 520)
        self.assertEqual(routes[0]['selection_reason']['code'], 'SAFER')
        self.assertEqual(routes[0]['selection_reason']['extra_time_s'], 120)
        self.assertEqual(routes[0]['selection_reason']['avoided'][0]['subtype'], S.SENS_INVERSE)
        direct = routes[1]
        self.assertEqual((direct['risk_level'], direct['risks'][0]['severity']), ('HIGH', 8))

    def test_signalement_tout_neuf_ne_suffit_pas(self, _nuit):
        # Non confirme : 8 x 1/4 = 2 points = 2 min, egalite -> la plus rapide reste devant.
        signaler(self.auteur, T.POLICE, S.SENS_INVERSE, score_confiance=0)
        routes = self.calculer()
        self.assertEqual(routes[0]['duration'], 400)
        self.assertEqual(routes[0]['risk_level'], 'MEDIUM')

    def test_detour_trop_long_jamais_recommande(self, _nuit):
        signaler(self.auteur, T.POLICE, S.SENS_INVERSE)
        routes = self.calculer([trip_valhalla(DIRECT, 400), trip_valhalla(NORD, 1200)])
        self.assertEqual(routes[0]['duration'], 400)

    def test_doublons_au_meme_endroit_comptent_moins(self, _nuit):
        for _ in range(3):
            signaler(self.auteur, T.DANGER, S.NID_DE_POULE)
        routes = self.calculer()
        direct = next(r for r in routes if r['duration'] == 400)
        self.assertAlmostEqual(direct['risk_score'], 2 + 2 * 0.3 * 2)

    def test_danger_expire_avant_d_y_arriver(self, _nuit):
        signaler(self.auteur, T.POLICE, S.SENS_INVERSE, expire_le=timezone.now() + timedelta(seconds=30))
        self.assertEqual(self.calculer()[0]['duration'], 400)

    def test_contournement_demande_a_valhalla(self, _nuit):
        # Valhalla ne propose que le direct ; le contournement est demande en excluant le danger.
        signaler(self.auteur, T.POLICE, S.SENS_INVERSE)
        routes = self.calculer([trip_valhalla(DIRECT, 400)], contournement=[trip_valhalla(NORD, 520)])
        self.assertEqual(self.appels, 2)
        self.assertEqual(routes[0]['duration'], 520)
        self.assertEqual(routes[0]['selection_reason']['code'], 'SAFER')

    def test_nuit_compte_plus(self, _nuit):
        signaler(self.auteur, T.MAUVAIS_TEMPS, S.ROUTE_GLISSANTE)
        jour = next(r for r in self.calculer() if r['duration'] == 400)['risk_score']
        _nuit.return_value = True
        cache.clear()
        nuit = next(r for r in self.calculer() if r['duration'] == 400)['risk_score']
        self.assertAlmostEqual(nuit, jour * 1.5, places=1)


class RisqueEnRouteTests(TestCase):
    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.telephone)
        self.trajet = _trajet_actif_pour(
            self.utilisateur, geometrie=LineString(ROUTE_SUIVIE, srid=4326),
            position_destination='POINT(9.73 4.05)',
        )
        self.url = reverse('trips:trajet-meilleur-itineraire', kwargs={'id': self.trajet.id})

    def demander(self, candidats):
        with patch('trips.services.meilleur_itineraire.ServiceItineraire') as Service:
            Service.return_value.calculer.return_value = candidats
            reponse = self.client.post(
                self.url, {'lat': 4.05, 'lon': 9.705, 'heading': 90}, content_type='application/json', **self.jetons,
            )
        self.assertEqual(reponse.status_code, 200, reponse.content)
        return reponse.json()

    def avec_risque(self, route, points=0, distance=1500, gravite=8, ident='11111111-1111-1111-1111-111111111111'):
        route['risque'] = points
        route['risques'] = [{
            'id': ident, 'type': T.ACCIDENT, 'subtype': S.ACCIDENT, 'distance_from_start_m': distance,
            'severity': gravite, 'confidence': 1.0, 'points': points,
        }] if points else []
        return route

    def test_route_plus_sure_proposee_une_seule_fois(self):
        candidats = [self.avec_risque(candidat(ROUTE_SUIVIE, 600, 'suivie'), 8),
                     self.avec_risque(candidat(VARIANTE, 700, 'nord'))]
        donnees = self.demander(candidats)
        self.assertEqual(donnees['reason'], 'SAFER_FOUND')
        self.assertEqual(donnees['better_route']['route_id'], 'nord')
        self.assertEqual((donnees['time_saved_s'], donnees['risk_before'], donnees['risk_after']), (-100, 8, 0))
        self.assertEqual(donnees['avoided_hazards'][0]['subtype'], S.ACCIDENT)
        # Ignoree par le conducteur : on ne repropose pas pour le meme danger.
        self.assertEqual(self.demander(candidats)['reason'], 'HAZARD_AHEAD')

    def test_danger_trop_proche_alerte_seulement(self):
        candidats = [self.avec_risque(candidat(ROUTE_SUIVIE, 600, 'suivie'), 8, distance=150),
                     self.avec_risque(candidat(VARIANTE, 700, 'nord'))]
        donnees = self.demander(candidats)
        self.assertEqual(donnees['reason'], 'HAZARD_AHEAD')
        self.assertIsNone(donnees['better_route'])
        self.assertEqual(len(donnees['hazards_ahead']), 1)

    def test_plus_rapide_mais_plus_risquee_pas_proposee(self):
        candidats = [self.avec_risque(candidat(ROUTE_SUIVIE, 600, 'suivie', 1200)),
                     self.avec_risque(candidat(VARIANTE, 540, 'nord'), 8)]
        self.assertEqual(self.demander(candidats)['reason'], 'NO_BETTER')

    def test_route_barree_confirmee_devant(self):
        auteur = creer_utilisateur(email='auteur@test.cm')
        signaler(auteur, T.VOIE_BLOQUEE, S.ROUTE_BARREE, lon=9.72, lat=4.05, confirmations=1)
        donnees = self.demander([candidat(VARIANTE, 900, 'nord'), candidat(ROUTE_SUIVIE, 600, 'suivie')])
        self.assertEqual(donnees['reason'], 'REROUTE_REQUIRED')
        self.assertEqual(donnees['better_route']['route_id'], 'nord')
        self.assertEqual(donnees['hazards_ahead'][0]['subtype'], S.ROUTE_BARREE)

    def test_route_barree_deja_passee_ignoree(self):
        auteur = creer_utilisateur(email='auteur@test.cm')
        signaler(auteur, T.VOIE_BLOQUEE, S.ROUTE_BARREE, lon=9.701, lat=4.05, confirmations=1)
        donnees = self.demander([candidat(ROUTE_SUIVIE, 600, 'suivie'), candidat(VARIANTE, 900, 'nord')])
        self.assertEqual(donnees['reason'], 'NO_BETTER')
