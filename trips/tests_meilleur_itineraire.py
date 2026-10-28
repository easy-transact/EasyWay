"""Meilleur itineraire en cours de route (better-route / switch-route)."""

from unittest.mock import patch

from django.contrib.gis.geos import LineString
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.tests import connecter, creer_utilisateur

from .models import StatutTrajet
from .polyline import encoder_polyline6
from .tests import _trajet_actif_pour

# Trajet suivi : tout droit vers l'est sur la latitude 4.05.
ROUTE_SUIVIE = [(9.70, 4.05), (9.71, 4.05), (9.72, 4.05), (9.73, 4.05)]
# Variante : passe 2 km plus au nord.
VARIANTE = [(9.70, 4.05), (9.71, 4.07), (9.72, 4.07), (9.73, 4.05)]


def candidat(points, duree, identifiant, avec_trafic=None):
    return {
        'identifiant': identifiant, 'libelle': f'Via {identifiant}', 'distance': 3500, 'duree': duree,
        'duree_avec_trafic': avec_trafic, 'niveau_trafic': 'NORMAL', 'geometrie': encoder_polyline6(points),
        'est_recommande': False, 'degrade': False,
        'manoeuvres': [{'type': 'depart', 'instruction': 'Partez', 'instruction_vocale': 'Partez', 'distance': 3500,
                        'duree': duree, 'nom_voie': '', 'road_class': 'URBAIN', 'arrivee_etape_index': None}],
    }


class MeilleurItineraireTests(TestCase):
    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.telephone)
        self.trajet = _trajet_actif_pour(
            self.utilisateur, geometrie=LineString(ROUTE_SUIVIE, srid=4326),
            position_destination='POINT(9.73 4.05)',
        )
        self.url = reverse('trips:trajet-meilleur-itineraire', kwargs={'id': self.trajet.id})

    def demander(self, candidats, position=(4.05, 9.705)):
        with patch('trips.services.meilleur_itineraire.ServiceItineraire') as Service:
            Service.return_value.calculer.return_value = candidats
            return self.client.post(
                self.url, {'lat': position[0], 'lon': position[1], 'heading': 90},
                content_type='application/json', **self.jetons,
            )

    def test_route_plus_rapide_proposee(self):
        # Bouchon sur la route suivie : 20 min avec le trafic, la variante en fait 9.
        reponse = self.demander([candidat(ROUTE_SUIVIE, 600, 'suivie', 1200), candidat(VARIANTE, 540, 'nord')])
        donnees = reponse.json()
        self.assertEqual(donnees['reason'], 'BETTER_FOUND')
        self.assertEqual(donnees['better_route']['route_id'], 'nord')
        self.assertEqual((donnees['current_duration_s'], donnees['time_saved_s']), (1200, 660))

    def test_gain_trop_faible(self):
        reponse = self.demander([candidat(ROUTE_SUIVIE, 600, 'suivie'), candidat(VARIANTE, 520, 'nord')])
        self.assertEqual(reponse.json()['reason'], 'NO_BETTER')
        self.assertIsNone(reponse.json()['better_route'])

    def test_route_suivie_deja_la_meilleure(self):
        reponse = self.demander([candidat(ROUTE_SUIVIE, 300, 'suivie'), candidat(VARIANTE, 900, 'nord')])
        self.assertEqual(reponse.json()['reason'], 'NO_BETTER')

    def test_conducteur_hors_de_sa_route(self):
        loin = [(9.70, 4.10), (9.73, 4.10)]
        reponse = self.demander([candidat(loin, 600, 'a'), candidat(loin, 300, 'b')])
        self.assertEqual(reponse.json()['reason'], 'OFF_ROUTE')

    def test_une_seule_route(self):
        self.assertEqual(self.demander([candidat(ROUTE_SUIVIE, 600, 'suivie')]).json()['reason'], 'NO_ALTERNATIVE')

    def test_trajet_d_un_autre_ou_termine(self):
        autre = creer_utilisateur(email='autre@test.cm')
        trajet_autre = _trajet_actif_pour(autre)
        url = reverse('trips:trajet-meilleur-itineraire', kwargs={'id': trajet_autre.id})
        self.assertEqual(self.client.post(url, {'lat': 4.05, 'lon': 9.7}, content_type='application/json', **self.jetons).status_code, 404)
        self.trajet.statut = StatutTrajet.TERMINE
        self.trajet.save(update_fields=['statut'])
        self.assertEqual(self.demander([]).status_code, 400)

    def test_accepter_la_nouvelle_route_garde_le_trace_parcouru(self):
        nouvelle = candidat(VARIANTE[1:], 540, 'nord')
        nouvelle['geometrie'] = encoder_polyline6([(9.71, 4.05), (9.71, 4.07), (9.73, 4.05)])
        from .serializers import ItineraireCandidatSerializer

        reponse = self.client.post(
            reverse('trips:trajet-changer-itineraire', kwargs={'id': self.trajet.id}),
            {'lat': 4.05, 'lon': 9.71, 'route': ItineraireCandidatSerializer(nouvelle).data},
            content_type='application/json', **self.jetons,
        )
        self.assertEqual(reponse.status_code, 200)
        self.trajet.refresh_from_db()
        self.assertEqual(self.trajet.itineraire_choisi, 'nord')
        coords = [tuple(round(v, 5) for v in c) for c in self.trajet.geometrie.coords]
        # Debut deja parcouru (9.70 -> 9.71 sur 4.05) puis la variante par le nord.
        self.assertEqual(coords, [(9.70, 4.05), (9.71, 4.05), (9.71, 4.07), (9.73, 4.05)])
        self.assertEqual(self.trajet.itineraires.get(identifiant='nord').manoeuvres.count(), 1)


class AutresItinerairesTests(TestCase):
    """Le conducteur demande une autre route : proposee meme plus lente."""

    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.telephone)
        self.trajet = _trajet_actif_pour(
            self.utilisateur, geometrie=LineString(ROUTE_SUIVIE, srid=4326), position_destination='POINT(9.73 4.05)',
        )
        self.url = reverse('trips:trajet-autres-itineraires', kwargs={'id': self.trajet.id})

    def demander(self, candidats):
        with patch('trips.services.meilleur_itineraire.ServiceItineraire') as Service:
            Service.return_value.calculer.return_value = candidats
            return self.client.post(
                self.url, {'lat': 4.05, 'lon': 9.705, 'heading': 90}, content_type='application/json', **self.jetons,
            ).json()

    def test_route_plus_lente_proposee_quand_meme(self):
        # La ou better-route repond NO_BETTER (variante plus lente de 5 min).
        donnees = self.demander([candidat(ROUTE_SUIVIE, 300, 'suivie'), candidat(VARIANTE, 600, 'nord')])
        self.assertEqual(donnees['reason'], 'ALTERNATIVES_FOUND')
        self.assertEqual([r['route_id'] for r in donnees['routes']], ['nord'])  # jamais la route suivie
        self.assertEqual((donnees['current_duration_s'], donnees['routes'][0]['time_difference_s']), (300, 300))

    def test_pas_d_autre_route(self):
        donnees = self.demander([candidat(ROUTE_SUIVIE, 300, 'suivie')])
        self.assertEqual((donnees['reason'], donnees['routes']), ('NO_ALTERNATIVE', []))

    def test_hors_de_sa_route_toutes_les_routes(self):
        loin = [(9.70, 4.10), (9.73, 4.10)]
        donnees = self.demander([candidat(loin, 600, 'a'), candidat(loin, 300, 'b')])
        self.assertEqual([r['route_id'] for r in donnees['routes']], ['b', 'a'])
        self.assertIsNone(donnees['routes'][0]['time_difference_s'])
