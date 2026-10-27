"""Points de repere dans le guidage (places/services/reperes.py) -- les
verifications de la section 5 de la spec, plus les cas limites."""

from django.contrib.gis.geos import Point
from django.test import SimpleTestCase, TestCase

from trips.polyline import encoder_polyline6

from .models import Etablissement, Lieu, SourceLieu, StatutLieu, TypeEtablissement
from .services.reperes import ajouter_reperes, nature
from .utils import normaliser

# Trace plein est sur 1 km a lat 4.05 ; 1 m de longitude ~ 9.03e-6 deg.
M = 1 / (111_320 * 0.99750)
TRACE = [(9.70 + i * 100 * M, 4.05) for i in range(11)]


def lieu(nom, categorie, metres, ecart_m=20, **extra):
    """Lieu a `metres` du depart, `ecart_m` au nord du trace."""
    return Lieu.objects.create(
        nom=nom, nom_normalise=normaliser(nom), categorie=categorie, ville='Douala',
        position=Point(9.70 + metres * M, 4.05 + ecart_m / 111_320, srid=4326),
        source=SourceLieu.OPENSTREETMAP, statut=extra.pop('statut', StatutLieu.APPROUVE), **extra,
    )


def candidat(*manoeuvres):
    """manoeuvres : (type, distance_m) -- la manoeuvre i est au cumul des precedentes."""
    return {
        'geometrie': encoder_polyline6(TRACE),
        'manoeuvres': [{'type': str(t), 'distance': d} for t, d in manoeuvres],
    }


def reperes(c):
    return [m['repere'] for m in ajouter_reperes(c)['manoeuvres']]


class NatureTests(SimpleTestCase):
    def test_categories_du_fichier_des_lieux(self):
        self.assertEqual(nature('Carrefour / rond-point', 'Ndokoti'), 'CARREFOUR')
        self.assertEqual(nature('Autre', 'Rond-point Deido'), 'CARREFOUR')
        self.assertEqual(nature('Station-service', 'Total Bonateki'), 'STATION')
        self.assertEqual(nature('Église', 'Saint Pierre'), 'CULTE')
        self.assertEqual(nature('Clinique / centre de santé', 'Clinique X'), 'SANTE')
        self.assertEqual(nature('Marché', 'Marché Central'), 'MARCHE')
        self.assertEqual(nature('Banque', 'Afriland'), 'AUTRE')
        self.assertIsNone(nature('Station de taxi', 'Taxi Akwa'))
        self.assertIsNone(nature('Beauté / coiffure', 'Salon'))


class ReperesTests(TestCase):
    def test_virage_au_carrefour_nomme(self):
        lieu('Carrefour Ndokoti', 'Carrefour / rond-point', 300)
        lieu('Total Ndokoti', 'Station-service', 290)
        self.assertEqual(
            reperes(candidat((1, 300), (10, 400), (4, 0))),
            [None, {'name': 'Carrefour Ndokoti', 'kind': 'CARREFOUR'}, None],
        )

    def test_station_sans_carrefour(self):
        lieu('Total Bonateki', 'Station-service', 295)
        self.assertEqual(reperes(candidat((1, 300), (10, 400), (4, 0)))[1], {'name': 'Total Bonateki', 'kind': 'STATION'})

    def test_rien_a_60_m(self):
        lieu('Total loin', 'Station-service', 300, ecart_m=90)
        self.assertEqual(reperes(candidat((1, 300), (10, 400), (4, 0))), [None, None, None])

    def test_jamais_au_depart_ni_a_l_arrivee(self):
        lieu('Carrefour Depart', 'Carrefour / rond-point', 0)
        lieu('Carrefour Arrivee', 'Carrefour / rond-point', 700)
        self.assertEqual(reperes(candidat((1, 300), (10, 400), (4, 0))), [None, None, None])

    def test_position_approximative_ecartee(self):
        lieu('Kombi Agro Farm', 'Marché', 300, position_approximative=True)
        self.assertIsNone(reperes(candidat((1, 300), (10, 400), (4, 0)))[1])

    def test_lieu_apres_la_manoeuvre_ecarte(self):
        lieu('Eglise plus loin', 'Église', 350, ecart_m=5)  # 50 m apres le virage
        self.assertIsNone(reperes(candidat((1, 300), (10, 400), (4, 0)))[1])

    def test_meme_repere_deux_fois_de_suite(self):
        lieu('Rond-point Deido', 'Carrefour / rond-point', 310)
        # Deux manoeuvres a 300 et 330 m : le rond-point seulement sur la premiere.
        self.assertEqual(
            [r and r['name'] for r in reperes(candidat((1, 300), (10, 30), (10, 400), (4, 0)))],
            [None, 'Rond-point Deido', None, None],
        )

    def test_nom_illisible_ou_lieu_non_approuve_ecarte(self):
        lieu('Annexe Universite;Faculte des sciences juridiques', 'Université / grande école', 300)
        lieu('Carrefour propose', 'Carrefour / rond-point', 300, statut=StatutLieu.EN_ATTENTE)
        self.assertIsNone(reperes(candidat((1, 300), (10, 400), (4, 0)))[1])

    def test_hopital_du_back_office(self):
        Etablissement.objects.create(
            nom='Hopital Laquintinie', type=TypeEtablissement.HOPITAL, position=Point(9.70 + 300 * M, 4.0502, srid=4326),
        )
        self.assertEqual(reperes(candidat((1, 300), (10, 400), (4, 0)))[1], {'name': 'Hopital Laquintinie', 'kind': 'SANTE'})


class RepereDansLApiTests(TestCase):
    """Champ landmark de routes/calculate : present s'il y a un repere,
    absent sinon, et garde quand l'appli lance le trajet."""

    def test_calcul_puis_lancement_du_trajet(self):
        from unittest.mock import patch

        from django.core.cache import cache
        from django.urls import reverse

        from accounts.tests import connecter, creer_utilisateur
        from trips.models import Manoeuvre

        cache.clear()
        lieu('Carrefour Ndokoti', 'Carrefour / rond-point', 300)
        jetons = connecter(self.client, creer_utilisateur().telephone)
        trip = {
            'legs': [{'shape': encoder_polyline6(TRACE), 'maneuvers': [
                {'type': 1, 'instruction': 'Partez', 'length': 0.3, 'time': 30, 'street_names': []},
                {'type': 10, 'instruction': 'Tournez a droite', 'length': 0.7, 'time': 70, 'street_names': []},
                {'type': 4, 'instruction': 'Arrivee', 'length': 0, 'time': 0, 'street_names': []},
            ]}],
            'summary': {'length': 1.0, 'time': 100},
        }
        with patch('trips.services.service_itineraire.ClientValhalla') as Client, \
                patch('trips.services.service_itineraire.service_trafic.evaluer_route',
                      return_value={'niveau_trafic': 'NORMAL', 'duree_avec_trafic': None}):
            Client.return_value.calculer_itineraires.return_value = [trip]
            route = self.client.post(
                reverse('trips:calculer-itineraire'),
                {'origin_lat': 4.05, 'origin_lon': 9.70, 'destination_lat': 4.05, 'destination_lon': 9.709},
                content_type='application/json', **jetons,
            ).json()[0]
        manoeuvres = route['maneuvers']
        self.assertEqual(manoeuvres[1]['landmark'], {'name': 'Carrefour Ndokoti', 'kind': 'CARREFOUR'})
        self.assertNotIn('landmark', manoeuvres[0])  # absent, pas null

        reponse = self.client.post(
            reverse('trips:trajets'),
            {'origin_label': 'A', 'origin_lat': 4.05, 'origin_lon': 9.70, 'destination_label': 'B',
             'destination_lat': 4.05, 'destination_lon': 9.709, 'route': route},
            content_type='application/json', **jetons,
        )
        self.assertEqual(reponse.status_code, 201, reponse.content)
        self.assertEqual(Manoeuvre.objects.get(ordre=1).repere, {'name': 'Carrefour Ndokoti', 'kind': 'CARREFOUR'})
