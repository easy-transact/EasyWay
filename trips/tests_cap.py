"""Cap d'origine peu fiable (telephone a l'arret) : pas de boucle au depart
(cf. ServiceItineraire, VITESSE_MIN_CAP_KMH / GAIN_MIN_SANS_CAP_S)."""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.tests import connecter, creer_utilisateur

from .tests_retards import DIRECT, NORD, trip_valhalla


def valhalla(depart, arrivee, options, cap_origine=None, alternatives=True, etapes=None):
    """Avec un cap : Valhalla part dans ce sens puis fait demi-tour (lent)."""
    if cap_origine is not None:
        return [trip_valhalla(NORD, 900)]
    return [trip_valhalla(DIRECT, 400)]


class CapOrigineTests(TestCase):
    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.telephone)
        patcheur = patch(
            'trips.services.service_itineraire.service_trafic.evaluer_route',
            return_value={'niveau_trafic': 'NORMAL', 'duree_avec_trafic': None},
        )
        patcheur.start()
        self.addCleanup(patcheur.stop)

    def calculer(self, **corps):
        with patch('trips.services.service_itineraire.ClientValhalla') as Client:
            client = Client.return_value
            client.calculer_itineraires.side_effect = valhalla
            reponse = self.client.post(
                reverse('trips:calculer-itineraire'),
                {'origin_lat': 4.05, 'origin_lon': 9.70, 'destination_lat': 4.05, 'destination_lon': 9.73, **corps},
                content_type='application/json', **self.jetons,
            )
            self.caps = [appel.kwargs.get('cap_origine') for appel in client.calculer_itineraires.call_args_list]
        self.assertEqual(reponse.status_code, 200, reponse.content)
        return reponse.json()

    def test_vitesse_inconnue_cap_qui_force_un_detour_abandonne(self):
        routes = self.calculer(origin_heading=225)
        self.assertEqual(routes[0]['duration'], 400)
        self.assertIn(None, self.caps)

    def test_a_l_arret_cap_ignore(self):
        routes = self.calculer(origin_heading=225, origin_speed_kmh=2)
        self.assertEqual(routes[0]['duration'], 400)
        self.assertEqual(set(self.caps), {None})

    def test_en_roulant_cap_respecte(self):
        routes = self.calculer(origin_heading=225, origin_speed_kmh=40)
        self.assertEqual(routes[0]['duration'], 900)
        self.assertEqual(set(self.caps), {225})

    @override_settings(CONTROLE_CAP_ORIGINE=False)
    def test_controle_desactive_cap_transmis_tel_quel(self):
        routes = self.calculer(origin_heading=225, origin_speed_kmh=2)
        self.assertEqual(routes[0]['duration'], 900)
        self.assertEqual(set(self.caps), {225})
