"""Lieux sur la carte de l'application : zone visible et le long d'un itineraire."""

from django.contrib.gis.geos import Point
from django.test import TestCase
from django.urls import reverse

from trips.polyline import encoder_polyline6

from .models import Etablissement, Lieu, SourceLieu, StatutLieu, TypeEtablissement
from .utils import normaliser


def lieu(nom, lat, lon, source=SourceLieu.UTILISATEUR, statut=StatutLieu.APPROUVE, categorie='Station-service'):
    return Lieu.objects.create(
        nom=nom, nom_normalise=normaliser(nom), categorie=categorie, ville='Douala',
        position=Point(lon, lat, srid=4326), source=source, statut=statut,
    )


ZONE = {'min_lat': 4.0, 'min_lon': 9.6, 'max_lat': 4.1, 'max_lon': 9.8}


class LieuxCarteTests(TestCase):
    def setUp(self):
        lieu('Tradex Akwa', 4.05, 9.70)
        lieu('Ajout EasyWay', 4.051, 9.701, source=SourceLieu.BACK_OFFICE, categorie='Pharmacie')
        lieu('Import OSM', 4.052, 9.702, source=SourceLieu.OPENSTREETMAP)
        lieu('En attente', 4.053, 9.703, statut=StatutLieu.EN_ATTENTE)
        lieu('Hors zone', 4.5, 9.70)
        Etablissement.objects.create(nom='Hopital Laquintinie', type=TypeEtablissement.HOPITAL, position=Point(9.70, 4.06, srid=4326))
        Etablissement.objects.create(nom='Garage cache', type=TypeEtablissement.GARAGE, position=Point(9.70, 4.06, srid=4326), visible_sur_carte=False)

    def test_zone_visible(self):
        donnees = self.client.get(reverse('places:carte'), ZONE).json()
        noms = sorted(p['name'] for p in donnees['results'])
        self.assertEqual(noms, ['Ajout EasyWay', 'Hopital Laquintinie', 'Tradex Akwa'])
        self.assertFalse(donnees['truncated'])

    def test_filtre_par_categorie(self):
        donnees = self.client.get(reverse('places:carte'), {**ZONE, 'categories': 'Pharmacie,HOPITAL'}).json()
        self.assertEqual(sorted(p['kind'] for p in donnees['results']), ['establishment', 'place'])

    def test_zone_trop_grande_ou_incomplete(self):
        self.assertEqual(self.client.get(reverse('places:carte'), {**ZONE, 'max_lat': 5.0}).status_code, 400)
        self.assertEqual(self.client.get(reverse('places:carte'), {'min_lat': 4.0}).status_code, 400)

    def test_le_long_de_l_itineraire(self):
        # Trajet nord-sud sur la longitude 9.70, de 4.04 a 4.07.
        geometrie = encoder_polyline6([(9.70, 4.04), (9.70, 4.07)])
        lieu('Loin du trace', 4.05, 9.72)
        reponse = self.client.post(
            reverse('places:sur-trajet'), {'geometry': geometrie, 'buffer_m': 200}, content_type='application/json'
        )
        self.assertEqual(reponse.status_code, 200)
        resultats = reponse.json()
        self.assertEqual([p['name'] for p in resultats], ['Tradex Akwa', 'Ajout EasyWay', 'Hopital Laquintinie'])
        self.assertAlmostEqual(resultats[0]['position_m'], 1112, delta=5)
        self.assertLess(resultats[1]['distance_m'], 200)

    def test_categories(self):
        categories = self.client.get(reverse('places:carte-categories')).json()
        self.assertIn({'category': 'HOPITAL', 'label': 'Hopital', 'count': 1, 'kind': 'establishment'}, categories)
        self.assertIn({'category': 'Station-service', 'label': 'Station-service', 'count': 2, 'kind': 'place'}, categories)
