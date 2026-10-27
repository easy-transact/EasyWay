"""Avis sur les garages (vues_avis.py, services/garages.py)."""

from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.tests import connecter, creer_utilisateur
from ads_admin.models import EntreeAudit

from .models import AvisGarage, Etablissement, SourceLieu, StatutAvis, StatutLieu, TypeEtablissement
from .tests import creer_lieu as creer_lieu_osm


def creer_lieu(nom, lat, lon, source=SourceLieu.OPENSTREETMAP, **extra):
    lieu = creer_lieu_osm(nom, lat, lon, **extra)
    lieu.source = source
    lieu.save(update_fields=['source'])
    return lieu


class AvisGarageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.telephone)
        self.garage = creer_lieu('Garage du Port', 4.05, 9.70, categorie='Garage / mécanique', source=SourceLieu.BACK_OFFICE)

    def url(self, garage=None):
        return reverse('places:avis-garage', kwargs={'id': (garage or self.garage).id})

    def noter(self, note, commentaire='', jetons=None, garage=None):
        return self.client.post(
            self.url(garage), {'rating': note, 'comment': commentaire}, content_type='application/json',
            **(jetons or self.jetons),
        )

    def test_noter_puis_modifier_un_seul_avis(self):
        reponse = self.noter(4, 'Bon accueil')
        self.assertEqual(reponse.status_code, 201, reponse.content)
        self.assertEqual((reponse.json()['average'], reponse.json()['count']), (4.0, 1))

        reponse = self.noter(2, 'Trop cher finalement')
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(AvisGarage.objects.count(), 1)
        self.assertEqual(reponse.json()['my_review']['rating'], 2)

    def test_moyenne_et_liste_publique(self):
        self.noter(5)
        autre = creer_utilisateur('autre@easyway.local')
        self.noter(2, 'Long', jetons=connecter(self.client, autre.telephone))
        reponse = self.client.get(self.url())  # sans connexion
        self.assertEqual(reponse.status_code, 200)
        corps = reponse.json()
        self.assertEqual((corps['average'], corps['count'], corps['my_review']), (3.5, 2, None))
        self.assertEqual(len(corps['results']), 2)

    def test_seuls_les_garages_sont_notables(self):
        restaurant = creer_lieu('Chez Mama', 4.06, 9.71, categorie='Restaurant', source=SourceLieu.BACK_OFFICE)
        self.assertEqual(self.noter(5, garage=restaurant).status_code, 404)
        en_attente = creer_lieu('Garage X', 4.06, 9.71, categorie='Garage', statut=StatutLieu.EN_ATTENTE)
        self.assertEqual(self.noter(5, garage=en_attente).status_code, 404)

    def test_etablissement_garage_notable(self):
        etablissement = Etablissement.objects.create(
            nom='Garage Central', type=TypeEtablissement.GARAGE, position=Point(9.7, 4.05, srid=4326),
        )
        self.assertEqual(self.noter(3, garage=etablissement).status_code, 201)
        hopital = Etablissement.objects.create(
            nom='Hopital', type=TypeEtablissement.HOPITAL, position=Point(9.7, 4.05, srid=4326),
        )
        self.assertEqual(self.noter(3, garage=hopital).status_code, 404)

    def test_note_hors_bornes_refusee(self):
        self.assertEqual(self.noter(6).status_code, 400)
        self.assertEqual(self.noter(0).status_code, 400)

    def test_noter_exige_une_connexion(self):
        reponse = self.client.post(self.url(), {'rating': 4}, content_type='application/json')
        self.assertEqual(reponse.status_code, 401)

    def test_supprimer_mon_avis(self):
        self.noter(4)
        reponse = self.client.delete(reverse('places:mon-avis-garage', kwargs={'id': self.garage.id}), **self.jetons)
        self.assertEqual(reponse.status_code, 204)
        self.assertFalse(AvisGarage.objects.exists())

    def test_note_sur_la_carte_et_le_detail(self):
        self.noter(4)
        carte = self.client.get(reverse('places:carte'), {'min_lat': 4.0, 'min_lon': 9.6, 'max_lat': 4.1, 'max_lon': 9.8}).json()
        point = next(p for p in carte['results'] if p['id'] == str(self.garage.id))
        self.assertEqual((point['is_garage'], point['rating_average'], point['rating_count']), (True, 4.0, 1))
        detail = self.client.get(reverse('places:lieu-detail', kwargs={'id': self.garage.id})).json()
        self.assertEqual((detail['is_garage'], detail['rating_average']), (True, 4.0))

    def test_aucun_point_de_recompense(self):
        points = self.utilisateur.points
        self.noter(5)
        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.points, points)


class AvisModerationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = creer_utilisateur('staff@easyway.local', is_staff=True)
        self.jetons_staff = connecter(self.client, self.staff.telephone)
        self.auteur = creer_utilisateur('auteur@easyway.local')
        self.garage = creer_lieu('Garage du Port', 4.05, 9.70, categorie='Garage / mécanique')
        self.avis = AvisGarage.objects.create(utilisateur=self.auteur, lieu=self.garage, note=1, commentaire='Insulte')

    def test_retirer_sort_de_la_moyenne_et_journalise(self):
        reponse = self.client.post(
            reverse('places:staff-avis-retirer', kwargs={'id': self.avis.id}), {'reason': 'Propos injurieux'},
            content_type='application/json', **self.jetons_staff,
        )
        self.assertEqual(reponse.status_code, 200, reponse.content)
        self.avis.refresh_from_db()
        self.assertEqual(self.avis.statut, StatutAvis.RETIRE)
        public = self.client.get(reverse('places:avis-garage', kwargs={'id': self.garage.id})).json()
        self.assertEqual((public['count'], public['results']), (0, []))
        self.assertTrue(EntreeAudit.objects.filter(action='reviews.remove').exists())

    def test_liste_staff_filtree_par_utilisateur(self):
        reponse = self.client.get(reverse('places:staff-avis'), {'user': self.auteur.id}, **self.jetons_staff)
        self.assertEqual(reponse.status_code, 200)
        resultat = reponse.json()['results'][0]
        self.assertEqual((resultat['garage']['name'], resultat['author_phone']), ('Garage du Port', self.auteur.telephone))

    def test_liste_reservee_au_staff(self):
        jetons = connecter(self.client, self.auteur.telephone)
        self.assertEqual(self.client.get(reverse('places:staff-avis'), **jetons).status_code, 403)
