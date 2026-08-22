from unittest.mock import Mock, patch

import requests
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.models import Droits, Formule, Parametres, Utilisateur
from accounts.tests import connecter
from .models import AdresseEnregistree, Lieu, RechercheRecente, SourceLieu, StatutLieu
from .services.client_nominatim import (
    CLE_ECHECS as CLE_ECHECS_NOMINATIM,
    CLE_OUVERT_JUSQU_A as CLE_OUVERT_JUSQU_A_NOMINATIM,
    SEUIL_ECHECS as SEUIL_ECHECS_NOMINATIM,
    ClientNominatim,
)
from .services.client_photon import (
    CLE_ECHECS as CLE_ECHECS_PHOTON,
    CLE_OUVERT_JUSQU_A as CLE_OUVERT_JUSQU_A_PHOTON,
    SEUIL_ECHECS as SEUIL_ECHECS_PHOTON,
    ClientPhoton,
)
from .utils import normaliser

MOT_DE_PASSE = 'CorrectHorse9!'


def patcher_nominatim(test_case, inverser=None):
    """A appeler dans setUp() de tout test qui frappe InverseView : sans ca,
    ClientNominatim tente un vrai appel reseau vers NOMINATIM_URL et
    ralentit/flake la suite des qu'il n'est pas demarre (le cas normal en CI)."""
    patcheur = patch('places.views.ClientNominatim')
    classe_simulee = patcheur.start()
    classe_simulee.return_value.inverser.return_value = inverser
    test_case.addCleanup(patcheur.stop)


def patcher_photon(test_case, rechercher=None):
    """Idem pour RechercheView, qui utilise Photon (pas Nominatim) comme
    source de recherche externe -- cf. places/views.py."""
    patcheur = patch('places.views.ClientPhoton')
    classe_simulee = patcheur.start()
    classe_simulee.return_value.rechercher.return_value = rechercher or []
    test_case.addCleanup(patcheur.stop)


def creer_utilisateur(email='user@easyway.local', **extra):
    utilisateur = Utilisateur.objects.create_user(
        email=email, password=MOT_DE_PASSE, nom_complet='Test User', **extra
    )
    Parametres.objects.create(utilisateur=utilisateur)
    return utilisateur


def creer_lieu(nom, lat, lon, ville='Douala', statut=StatutLieu.APPROUVE, **extra):
    return Lieu.objects.create(
        nom=nom, nom_normalise=normaliser(nom), categorie=extra.pop('categorie', 'restaurant'),
        ville=ville, position=Point(lon, lat, srid=4326),
        source=SourceLieu.OPENSTREETMAP, statut=statut, **extra,
    )


class NormalisationTests(TestCase):
    def test_normaliser_retire_accents_et_minuscule(self):
        self.assertEqual(normaliser('Marché Général'), 'marche general')


class RechercheTests(TestCase):
    def setUp(self):
        patcher_photon(self)
        self.palais = creer_lieu('Palais des Congres', 3.8690, 11.5174, ville='Yaounde')
        self.marche = creer_lieu('Marche Central', 4.0483, 9.7043, ville='Douala')
        self.en_attente = creer_lieu('Boutique Non Approuvee', 4.05, 9.70, statut=StatutLieu.EN_ATTENTE)

    def test_q_trop_court_rejete(self):
        reponse = self.client.get(reverse('places:recherche'), {'q': 'P'})
        self.assertEqual(reponse.status_code, 400)

    def test_recherche_trouve_par_nom_partiel(self):
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Palais'})
        self.assertEqual(reponse.status_code, 200)
        noms = [r['libelle'] for r in reponse.json()]
        self.assertIn('Palais des Congres', noms)

    def test_recherche_ignore_les_lieux_non_approuves(self):
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Boutique'})
        self.assertEqual(reponse.json(), [])

    def test_recherche_tolere_les_accents(self):
        reponse = self.client.get(reverse('places:recherche'), {'q': 'marche'})
        noms = [r['libelle'] for r in reponse.json()]
        self.assertIn('Marche Central', noms)

    def test_recherche_avec_position_annote_la_distance(self):
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Marche', 'lat': '4.05', 'lon': '9.70'})
        resultats = reponse.json()
        self.assertEqual(len(resultats), 1)
        self.assertIsInstance(resultats[0]['distance_m'], int)

    def test_recherche_sans_position_pas_de_distance(self):
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Marche'})
        self.assertIsNone(reponse.json()[0]['distance_m'])


class InverseTests(TestCase):
    def setUp(self):
        patcher_nominatim(self)
        self.palais = creer_lieu('Palais des Congres', 3.8690, 11.5174, ville='Yaounde')

    def test_position_proche_retourne_le_lieu(self):
        reponse = self.client.get(reverse('places:inverse'), {'lat': '3.8690', 'lon': '11.5174'})
        self.assertEqual(reponse.json()['libelle'], 'Palais des Congres')

    def test_position_loin_retourne_position_generique(self):
        reponse = self.client.get(reverse('places:inverse'), {'lat': '0.0', 'lon': '0.0'})
        self.assertEqual(reponse.json()['libelle'], 'Position actuelle')
        self.assertIsNone(reponse.json()['lieu'])

    def test_parametres_manquants_rejetes(self):
        reponse = self.client.get(reverse('places:inverse'), {'lat': '0.0'})
        self.assertEqual(reponse.status_code, 400)


class LieuDetailTests(TestCase):
    def test_detail_lieu_approuve(self):
        lieu = creer_lieu('Palais des Congres', 3.8690, 11.5174, ville='Yaounde')
        reponse = self.client.get(reverse('places:lieu-detail', kwargs={'id': lieu.id}))
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['nom'], 'Palais des Congres')

    def test_detail_lieu_non_approuve_masque(self):
        lieu = creer_lieu('En attente', 4.05, 9.70, statut=StatutLieu.EN_ATTENTE)
        reponse = self.client.get(reverse('places:lieu-detail', kwargs={'id': lieu.id}))
        self.assertEqual(reponse.status_code, 404)


class ProposerLieuTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.email)

    def test_proposition_authentifiee_cree_lieu_en_attente(self):
        payload = {'nom': 'Boutique Test', 'categorie': 'shop', 'ville': 'Douala', 'lat': 4.05, 'lon': 9.70}
        reponse = self.client.post(
            reverse('places:proposer'), payload, content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 201)
        lieu = Lieu.objects.get(id=reponse.json()['id'])
        self.assertEqual(lieu.statut, StatutLieu.EN_ATTENTE)
        self.assertEqual(lieu.source, SourceLieu.UTILISATEUR)
        self.assertEqual(lieu.propose_par, self.utilisateur)

    def test_proposition_non_authentifiee_rejetee(self):
        payload = {'nom': 'Boutique Test', 'categorie': 'shop', 'ville': 'Douala', 'lat': 4.05, 'lon': 9.70}
        reponse = self.client.post(reverse('places:proposer'), payload, content_type='application/json')
        self.assertEqual(reponse.status_code, 401)


class AdresseEnregistreeTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.email)

    def _ajouter(self, libelle='PERSONNALISE', **extra):
        payload = {'libelle': libelle, 'adresse': 'Rue test', 'lat': 4.05, 'lon': 9.70, **extra}
        return self.client.post(
            reverse('places:enregistres'), payload, content_type='application/json', **self.jetons
        )

    def test_creation_et_liste(self):
        self.assertEqual(self._ajouter().status_code, 201)
        reponse = self.client.get(reverse('places:enregistres'), **self.jetons)
        self.assertEqual(len(reponse.json()), 1)

    def test_isolation_entre_utilisateurs(self):
        self._ajouter()
        autre = creer_utilisateur('autre@easyway.local')
        jetons_autre = connecter(self.client, autre.email)
        reponse = self.client.get(reverse('places:enregistres'), **jetons_autre)
        self.assertEqual(reponse.json(), [])

    def test_limite_formule_gratuite_appliquee_cote_serveur(self):
        for i in range(5):
            reponse = self._ajouter(nom_personnalise=f'Spot {i}', lat=4.0 + i * 0.01, lon=9.7)
            self.assertEqual(reponse.status_code, 201)

        reponse = self._ajouter(nom_personnalise='Spot en trop', lat=4.09, lon=9.7)
        self.assertEqual(reponse.status_code, 403)
        self.assertEqual(AdresseEnregistree.objects.filter(utilisateur=self.utilisateur).count(), 5)

    def test_formule_premium_pas_de_limite(self):
        self.utilisateur.formule = Formule.PREMIUM
        self.utilisateur.save(update_fields=['formule'])
        for i in range(6):
            reponse = self._ajouter(nom_personnalise=f'Spot {i}', lat=4.0 + i * 0.01, lon=9.7)
            self.assertEqual(reponse.status_code, 201)

    def test_patch_renomme(self):
        adresse_id = self._ajouter(nom_personnalise='Ancien nom').json()['id']
        reponse = self.client.patch(
            reverse('places:enregistres-detail', kwargs={'id': adresse_id}),
            {'nom_personnalise': 'Nouveau nom'},
            content_type='application/json',
            **self.jetons,
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['nom_personnalise'], 'Nouveau nom')

    def test_delete(self):
        adresse_id = self._ajouter().json()['id']
        reponse = self.client.delete(
            reverse('places:enregistres-detail', kwargs={'id': adresse_id}), **self.jetons
        )
        self.assertEqual(reponse.status_code, 204)
        self.assertFalse(AdresseEnregistree.objects.filter(id=adresse_id).exists())

    def test_delete_refuse_pour_autre_utilisateur(self):
        adresse_id = self._ajouter().json()['id']
        autre = creer_utilisateur('autre@easyway.local')
        jetons_autre = connecter(self.client, autre.email)
        reponse = self.client.delete(
            reverse('places:enregistres-detail', kwargs={'id': adresse_id}), **jetons_autre
        )
        self.assertEqual(reponse.status_code, 404)


class RechercheRecenteTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.email)

    def _ajouter(self, libelle):
        return self.client.post(
            reverse('places:recents'),
            {'libelle': libelle, 'lat': 4.05, 'lon': 9.70},
            content_type='application/json',
            **self.jetons,
        )

    def test_creation_et_liste_ordre_recent_dabord(self):
        self._ajouter('Premier')
        self._ajouter('Second')
        reponse = self.client.get(reverse('places:recents'), **self.jetons)
        libelles = [r['libelle'] for r in reponse.json()]
        self.assertEqual(libelles, ['Second', 'Premier'])

    def test_purge_au_dela_de_dix(self):
        for i in range(12):
            self._ajouter(f'Recherche {i}')
        self.assertEqual(RechercheRecente.objects.filter(utilisateur=self.utilisateur).count(), 10)

    def test_delete_vide_lhistorique(self):
        self._ajouter('Premier')
        reponse = self.client.delete(reverse('places:recents'), **self.jetons)
        self.assertEqual(reponse.status_code, 204)
        self.assertEqual(RechercheRecente.objects.filter(utilisateur=self.utilisateur).count(), 0)


def _resultat_photon_factice(libelle='Boulangerie Externe', lat=4.05, lon=9.70):
    return {
        'id': 'photon:N12345', 'libelle': libelle, 'sous_libelle': 'Douala',
        'categorie': 'bakery', 'lat': lat, 'lon': lon, 'distance_m': None, 'source': 'photon',
    }


class RechercheFusionPhotonTests(TestCase):
    """P2b : ClientPhoton vient completer -- jamais remplacer -- les
    resultats locaux, avec deduplication sur le nom normalise et un
    classement homogene (SequenceMatcher) sur les deux sources."""

    def setUp(self):
        self.marche = creer_lieu('Marche Central', 4.0483, 9.7043, ville='Douala')

    def test_resultat_photon_distinct_est_ajoute(self):
        patcher_photon(self, rechercher=[_resultat_photon_factice('Boulangerie du Marche')])
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Marche'})
        sources = {(r['libelle'], r['source']) for r in reponse.json()}
        self.assertIn(('Marche Central', 'local'), sources)
        self.assertIn(('Boulangerie du Marche', 'photon'), sources)

    def test_resultat_photon_doublon_du_local_est_ignore(self):
        patcher_photon(self, rechercher=[_resultat_photon_factice('Marche Central')])
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Marche'})
        self.assertEqual(len(reponse.json()), 1)
        self.assertEqual(reponse.json()[0]['source'], 'local')

    def test_photon_indisponible_degrade_sur_local_uniquement(self):
        patcher_photon(self, rechercher=[])  # replier_recherche() -> []
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Marche'})
        self.assertEqual(len(reponse.json()), 1)

    def test_doublons_entre_resultats_externes_sont_fusionnes(self):
        # Une rue en plusieurs troncons OSM (donc plusieurs osm_id) revient
        # sinon comme autant de resultats identiques a l'affichage.
        patcher_photon(self, rechercher=[
            _resultat_photon_factice('Avenue Kennedy', lat=3.865, lon=11.520),
            _resultat_photon_factice('Avenue Kennedy', lat=3.866, lon=11.521),
            _resultat_photon_factice('Avenue Kennedy', lat=3.867, lon=11.522),
        ])
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Avenue Kennedy'})
        corps = reponse.json()
        self.assertEqual(sum(1 for r in corps if r['libelle'] == 'Avenue Kennedy'), 1)

    def test_meilleure_correspondance_photon_passe_avant_un_local_faible(self):
        # Reproduit le bug observe en pratique ("Hopital Laquintinie") : un
        # match local faible ne doit pas passer devant un match externe fort
        # juste parce que "local d'abord" etait l'ordre de fusion naif.
        creer_lieu('Marche Improvise du Quartier', 4.20, 9.90, ville='Douala')
        patcher_photon(self, rechercher=[_resultat_photon_factice('Grand Marche Central de Douala')])
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Grand Marche Central de Douala'})
        self.assertEqual(reponse.json()[0]['libelle'], 'Grand Marche Central de Douala')
        self.assertEqual(reponse.json()[0]['source'], 'photon')


class InverseNominatimTests(TestCase):
    def setUp(self):
        self.palais = creer_lieu('Palais des Congres', 3.8690, 11.5174, ville='Yaounde')

    def test_nominatim_reussit_est_priorise_sur_le_local(self):
        patcher_nominatim(self, inverser={
            'id': 'nominatim:99', 'libelle': 'Avenue Kennedy', 'sous_libelle': 'Yaounde',
            'categorie': 'road', 'lat': 3.8690, 'lon': 11.5174, 'distance_m': None, 'source': 'nominatim',
        })
        reponse = self.client.get(reverse('places:inverse'), {'lat': '3.8690', 'lon': '11.5174'})
        self.assertEqual(reponse.json()['libelle'], 'Avenue Kennedy')
        self.assertEqual(reponse.json()['lieu']['source'], 'nominatim')

    def test_nominatim_sans_resultat_retombe_sur_le_lieu_local(self):
        patcher_nominatim(self, inverser=None)  # aucun resultat externe (pas une panne)
        reponse = self.client.get(reverse('places:inverse'), {'lat': '3.8690', 'lon': '11.5174'})
        self.assertEqual(reponse.json()['libelle'], 'Palais des Congres')
        self.assertEqual(reponse.json()['lieu']['source'], 'local')


class DisjoncteurNominatimTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_bascule_sur_repli_apres_echecs_repetes(self):
        client = ClientNominatim()
        with patch(
            'places.services.client_nominatim.requests.get',
            side_effect=requests.exceptions.ConnectionError('down'),
        ):
            for _ in range(SEUIL_ECHECS_NOMINATIM):
                self.assertEqual(client.rechercher('Marche', autour=None), [])

        self.assertIsNotNone(cache.get(CLE_OUVERT_JUSQU_A_NOMINATIM))

        with patch('places.services.client_nominatim.requests.get') as get_simule:
            self.assertIsNone(client.inverser(4.05, 9.70))
            get_simule.assert_not_called()

    def test_succes_reinitialise_le_compteur_dechecs(self):
        client = ClientNominatim()
        cache.set(CLE_ECHECS_NOMINATIM, SEUIL_ECHECS_NOMINATIM - 1, timeout=60)

        reponse_simulee = Mock(status_code=200)
        reponse_simulee.json.return_value = [
            {'place_id': 1, 'name': 'Marche Central', 'lat': '4.0483', 'lon': '9.7043', 'address': {}}
        ]
        reponse_simulee.raise_for_status = lambda: None
        with patch('places.services.client_nominatim.requests.get', return_value=reponse_simulee):
            resultats = client.rechercher('Marche', autour=None)

        self.assertEqual(len(resultats), 1)
        self.assertEqual(resultats[0]['libelle'], 'Marche Central')
        self.assertIsNone(cache.get(CLE_ECHECS_NOMINATIM))


class DisjoncteurPhotonTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_bascule_sur_repli_apres_echecs_repetes(self):
        client = ClientPhoton()
        with patch(
            'places.services.client_photon.requests.get',
            side_effect=requests.exceptions.ConnectionError('down'),
        ):
            for _ in range(SEUIL_ECHECS_PHOTON):
                self.assertEqual(client.rechercher('Marche', autour=None), [])

        self.assertIsNotNone(cache.get(CLE_OUVERT_JUSQU_A_PHOTON))

        with patch('places.services.client_photon.requests.get') as get_simule:
            self.assertEqual(client.rechercher('Marche', autour=None), [])
            get_simule.assert_not_called()

    def test_succes_reinitialise_le_compteur_dechecs(self):
        client = ClientPhoton()
        cache.set(CLE_ECHECS_PHOTON, SEUIL_ECHECS_PHOTON - 1, timeout=60)

        reponse_simulee = Mock(status_code=200)
        reponse_simulee.json.return_value = {
            'features': [{
                'geometry': {'coordinates': [9.7043, 4.0483]},
                'properties': {'osm_id': 1, 'osm_type': 'N', 'name': 'Marche Central'},
            }],
        }
        reponse_simulee.raise_for_status = lambda: None
        with patch('places.services.client_photon.requests.get', return_value=reponse_simulee):
            resultats = client.rechercher('Marche', autour=None)

        self.assertEqual(len(resultats), 1)
        self.assertEqual(resultats[0]['libelle'], 'Marche Central')
        self.assertIsNone(cache.get(CLE_ECHECS_PHOTON))
