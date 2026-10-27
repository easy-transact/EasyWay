"""Services a proximite (GET /api/places/nearby/), horaires et etoiles."""

from datetime import datetime
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.tests import connecter, creer_utilisateur

from .models import AvisGarage, Etablissement, TypeEtablissement
from .services import garages, proximite
from .tests import creer_lieu

# 1 deg de latitude ~ 111 km : 0.005 ~ 555 m, 0.03 ~ 3,3 km.
CENTRE = (4.05, 9.70)
SEMAINE = {j: [['08:00', '20:00']] for j in proximite.JOURS}
LUNDI_10H = datetime(2026, 10, 26, 10, 0, tzinfo=proximite.maintenant_local().tzinfo)
LUNDI_22H = LUNDI_10H.replace(hour=22)


class ProximiteTests(TestCase):
    def setUp(self):
        self.pharmacie = creer_lieu('Pharmacie du Port', 4.055, 9.70, categorie='Pharmacie', horaires=SEMAINE)
        self.station = creer_lieu('Total Akwa', 4.051, 9.70, categorie='Station-service')
        self.garage = creer_lieu('Garage Ndokoti', 4.052, 9.70, categorie='Garage / mécanique')
        self.clinique = creer_lieu('Clinique Mere', 4.053, 9.70, categorie='Clinique / centre de santé')
        self.taxi = creer_lieu('Station taxi', 4.0505, 9.70, categorie='Station de taxi')
        self.loin = creer_lieu('Pharmacie lointaine', 4.08, 9.70, categorie='Pharmacie')
        self.hopital = Etablissement.objects.create(
            nom='Hopital Laquintinie', type=TypeEtablissement.HOPITAL, position=Point(9.70, 4.054, srid=4326),
        )

    def chercher(self, maintenant=LUNDI_10H, **params):
        with patch('places.services.proximite.maintenant_local', return_value=maintenant):
            reponse = self.client.get(reverse('places:proximite'), {'lat': CENTRE[0], 'lon': CENTRE[1], **params})
        self.assertEqual(reponse.status_code, 200, reponse.content)
        return reponse.json()['results']

    def test_tries_par_distance_dans_le_rayon(self):
        resultats = self.chercher(radius_m=1000)
        self.assertEqual(
            [r['name'] for r in resultats],
            ['Total Akwa', 'Garage Ndokoti', 'Clinique Mere', 'Hopital Laquintinie', 'Pharmacie du Port'],
        )
        self.assertEqual([r['type'] for r in resultats], ['station', 'garage', 'hopital', 'hopital', 'pharmacie'])
        # Rayon plus grand : la pharmacie lointaine (~3,3 km) apparait.
        self.assertIn('Pharmacie lointaine', [r['name'] for r in self.chercher(radius_m=5000)])

    def test_filtre_par_type(self):
        resultats = self.chercher(types='pharmacie,station', radius_m=1000)
        self.assertEqual({r['type'] for r in resultats}, {'pharmacie', 'station'})

    def test_type_inconnu_refuse(self):
        reponse = self.client.get(reverse('places:proximite'), {'lat': 4.05, 'lon': 9.7, 'types': 'boulangerie'})
        self.assertEqual(reponse.status_code, 400)

    def test_horaires_et_ouvert_maintenant(self):
        pharmacie = next(r for r in self.chercher(radius_m=1000) if r['type'] == 'pharmacie')
        self.assertEqual((pharmacie['is_open_now'], pharmacie['opening_hours']['mon']), (True, [['08:00', '20:00']]))
        station = next(r for r in self.chercher(radius_m=1000) if r['type'] == 'station')
        self.assertEqual((station['is_open_now'], station['opening_hours']), (None, None))

        soir = self.chercher(maintenant=LUNDI_22H, radius_m=1000, open_now='true')
        self.assertNotIn('Pharmacie du Port', [r['name'] for r in soir])  # fermee a 22 h
        self.assertIn('Total Akwa', [r['name'] for r in soir])  # horaires inconnus : gardee

    def test_note_des_garages(self):
        auteur = creer_utilisateur()
        AvisGarage.objects.create(utilisateur=auteur, lieu=self.garage, note=5)
        garage = next(r for r in self.chercher(types='garage') if r['id'] == str(self.garage.id))
        self.assertEqual((garage['rating_average'], garage['rating_count'], garage['stars']), (5.0, 1, 3.5))


class HorairesTests(SimpleTestCase):
    def test_validation(self):
        self.assertEqual(proximite.valider_horaires({}), {})
        self.assertEqual(proximite.valider_horaires({'mon': [['08:00', '12:00']]})['tue'], [])
        for invalide in ({'lundi': []}, {'mon': [['8h', '12h']]}, {'mon': [['08:00', '08:00']]}, {'mon': ['08:00']}):
            with self.assertRaises(proximite.HorairesInvalides):
                proximite.valider_horaires(invalide)

    def test_plage_qui_passe_minuit(self):
        horaires = {'mon': [['20:00', '02:00']]}
        self.assertTrue(proximite.est_ouvert(horaires, LUNDI_22H))
        mardi_1h = LUNDI_22H.replace(day=27, hour=1)
        self.assertTrue(proximite.est_ouvert(horaires, mardi_1h))
        self.assertFalse(proximite.est_ouvert(horaires, mardi_1h.replace(hour=3)))

    def test_24h_sur_24(self):
        horaires = {j: [['00:00', '24:00']] for j in proximite.JOURS}
        self.assertTrue(proximite.est_ouvert(horaires, LUNDI_22H.replace(hour=23, minute=59)))


class EtoilesTests(SimpleTestCase):
    def test_plus_de_votes_plus_d_etoiles(self):
        self.assertIsNone(garages.etoiles(None, 0))
        self.assertEqual(garages.etoiles(5, 1), 3.5)    # un seul avis parfait
        self.assertEqual(garages.etoiles(5, 20), 4.5)   # vingt avis parfaits
        self.assertEqual(garages.etoiles(5, 100), 5.0)
        self.assertEqual(garages.etoiles(1, 1), 2.5)    # un seul mauvais avis ne coule pas le garage
        self.assertEqual(garages.etoiles(1, 30), 1.5)


class HorairesStaffTests(TestCase):
    def test_staff_saisit_les_horaires(self):
        staff = creer_utilisateur('staff@easyway.local', is_staff=True)
        jetons = connecter(self.client, staff.telephone)
        pharmacie = creer_lieu('Pharmacie', 4.05, 9.70, categorie='Pharmacie')
        url = reverse('places:staff-lieu-supprimer', kwargs={'id': pharmacie.id})
        reponse = self.client.patch(url, {'opening_hours': SEMAINE}, content_type='application/json', **jetons)
        self.assertEqual(reponse.status_code, 200, reponse.content)
        pharmacie.refresh_from_db()
        self.assertEqual(pharmacie.horaires['sat'], [['08:00', '20:00']])
        mauvais = self.client.patch(url, {'opening_hours': {'mon': [['9h', '18h']]}}, content_type='application/json', **jetons)
        self.assertEqual(mauvais.status_code, 400)
