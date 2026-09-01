from unittest.mock import Mock, patch

import requests
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from accounts.models import Droits, Formule, Parametres, Utilisateur
from accounts.tests import connecter
from .models import AdresseEnregistree, Lieu, RechercheRecente, SourceLieu, StatutLieu, Ville
from .views import LIMITE_VILLES
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
    telephone = extra.pop('telephone', None)
    if not telephone:
        telephone = email
    utilisateur = Utilisateur.objects.create_user(
        telephone=telephone, email=email, password=MOT_DE_PASSE, nom_complet='Test User', **extra
    )
    Parametres.objects.create(utilisateur=utilisateur)
    return utilisateur


def creer_lieu(nom, lat, lon, ville='Douala', statut=StatutLieu.APPROUVE, **extra):
    return Lieu.objects.create(
        nom=nom, nom_normalise=normaliser(nom), categorie=extra.pop('categorie', 'restaurant'),
        ville=ville, position=Point(lon, lat, srid=4326),
        source=SourceLieu.OPENSTREETMAP, statut=statut, **extra,
    )


def creer_ville(nom, lat, lon, osm_id=None, type='city', population=0):
    return Ville.objects.create(
        osm_id=osm_id or hash(nom) % 2_000_000_000,
        nom=nom, nom_normalise=normaliser(nom), type=type, population=population,
        position=Point(lon, lat, srid=4326),
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
        noms = [r['label'] for r in reponse.json()]
        self.assertIn('Palais des Congres', noms)

    def test_recherche_ignore_les_lieux_non_approuves(self):
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Boutique'})
        self.assertEqual(reponse.json(), [])

    def test_recherche_tolere_les_accents(self):
        reponse = self.client.get(reverse('places:recherche'), {'q': 'marche'})
        noms = [r['label'] for r in reponse.json()]
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
        self.assertEqual(reponse.json()['label'], 'Palais des Congres')

    def test_position_loin_retourne_position_generique(self):
        reponse = self.client.get(reverse('places:inverse'), {'lat': '0.0', 'lon': '0.0'})
        self.assertEqual(reponse.json()['label'], 'Current position')
        self.assertIsNone(reponse.json()['place'])

    def test_parametres_manquants_rejetes(self):
        reponse = self.client.get(reverse('places:inverse'), {'lat': '0.0'})
        self.assertEqual(reponse.status_code, 400)


class LieuDetailTests(TestCase):
    def test_detail_lieu_approuve(self):
        lieu = creer_lieu('Palais des Congres', 3.8690, 11.5174, ville='Yaounde')
        reponse = self.client.get(reverse('places:lieu-detail', kwargs={'id': lieu.id}))
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['name'], 'Palais des Congres')

    def test_detail_lieu_non_approuve_masque(self):
        lieu = creer_lieu('En attente', 4.05, 9.70, statut=StatutLieu.EN_ATTENTE)
        reponse = self.client.get(reverse('places:lieu-detail', kwargs={'id': lieu.id}))
        self.assertEqual(reponse.status_code, 404)


class ProposerLieuTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.email)

    def test_proposition_authentifiee_cree_lieu_en_attente(self):
        payload = {'name': 'Boutique Test', 'category': 'shop', 'city': 'Douala', 'lat': 4.05, 'lon': 9.70}
        reponse = self.client.post(
            reverse('places:proposer'), payload, content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 201)
        lieu = Lieu.objects.get(id=reponse.json()['id'])
        self.assertEqual(lieu.statut, StatutLieu.EN_ATTENTE)
        self.assertEqual(lieu.source, SourceLieu.UTILISATEUR)
        self.assertEqual(lieu.propose_par, self.utilisateur)

    def test_proposition_non_authentifiee_rejetee(self):
        payload = {'name': 'Boutique Test', 'category': 'shop', 'city': 'Douala', 'lat': 4.05, 'lon': 9.70}
        reponse = self.client.post(reverse('places:proposer'), payload, content_type='application/json')
        self.assertEqual(reponse.status_code, 401)


class AdresseEnregistreeTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.email)

    def _ajouter(self, label='PERSONNALISE', **extra):
        payload = {'label': label, 'address': 'Rue test', 'lat': 4.05, 'lon': 9.70, **extra}
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
            reponse = self._ajouter(custom_name=f'Spot {i}', lat=4.0 + i * 0.01, lon=9.7)
            self.assertEqual(reponse.status_code, 201)

        reponse = self._ajouter(custom_name='Spot en trop', lat=4.09, lon=9.7)
        self.assertEqual(reponse.status_code, 403)
        self.assertEqual(AdresseEnregistree.objects.filter(utilisateur=self.utilisateur).count(), 5)

    def test_formule_premium_pas_de_limite(self):
        self.utilisateur.formule = Formule.PREMIUM
        self.utilisateur.save(update_fields=['formule'])
        for i in range(6):
            reponse = self._ajouter(custom_name=f'Spot {i}', lat=4.0 + i * 0.01, lon=9.7)
            self.assertEqual(reponse.status_code, 201)

    def test_patch_renomme(self):
        adresse_id = self._ajouter(custom_name='Ancien nom').json()['id']
        reponse = self.client.patch(
            reverse('places:enregistres-detail', kwargs={'id': adresse_id}),
            {'custom_name': 'Nouveau nom'},
            content_type='application/json',
            **self.jetons,
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['custom_name'], 'Nouveau nom')

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

    def _ajouter(self, label):
        return self.client.post(
            reverse('places:recents'),
            {'label': label, 'lat': 4.05, 'lon': 9.70},
            content_type='application/json',
            **self.jetons,
        )

    def test_creation_et_liste_ordre_recent_dabord(self):
        self._ajouter('Premier')
        self._ajouter('Second')
        reponse = self.client.get(reverse('places:recents'), **self.jetons)
        labels = [r['label'] for r in reponse.json()]
        self.assertEqual(labels, ['Second', 'Premier'])

    def test_purge_au_dela_de_dix(self):
        for i in range(12):
            self._ajouter(f'Recherche {i}')
        self.assertEqual(RechercheRecente.objects.filter(utilisateur=self.utilisateur).count(), 10)

    def test_delete_vide_lhistorique(self):
        self._ajouter('Premier')
        reponse = self.client.delete(reverse('places:recents'), **self.jetons)
        self.assertEqual(reponse.status_code, 204)
        self.assertEqual(RechercheRecente.objects.filter(utilisateur=self.utilisateur).count(), 0)

    def test_liste_renvoie_les_coordonnees(self):
        # lat/lon sont write_only (entree) -- sans position_lat/position_lon
        # en sortie, un recent revient sans coordonnees et devient inutilisable
        # comme destination.
        self._ajouter('Premier')
        reponse = self.client.get(reverse('places:recents'), **self.jetons)
        resultat = reponse.json()[0]
        self.assertEqual(resultat['position_lat'], 4.05)
        self.assertEqual(resultat['position_lon'], 9.70)


def _resultat_photon_factice(label='Boulangerie Externe', lat=4.05, lon=9.70):
    return {
        'id': 'photon:N12345', 'label': label, 'sublabel': 'Douala',
        'category': 'bakery', 'lat': lat, 'lon': lon, 'distance_m': None, 'source': 'photon',
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
        sources = {(r['label'], r['source']) for r in reponse.json()}
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
        self.assertEqual(sum(1 for r in corps if r['label'] == 'Avenue Kennedy'), 1)

    def test_meilleure_correspondance_photon_passe_avant_un_local_faible(self):
        # Reproduit le bug observe en pratique ("Hopital Laquintinie") : un
        # match local faible ne doit pas passer devant un match externe fort
        # juste parce que "local d'abord" etait l'ordre de fusion naif.
        creer_lieu('Marche Improvise du Quartier', 4.20, 9.90, ville='Douala')
        patcher_photon(self, rechercher=[_resultat_photon_factice('Grand Marche Central de Douala')])
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Grand Marche Central de Douala'})
        self.assertEqual(reponse.json()[0]['label'], 'Grand Marche Central de Douala')
        self.assertEqual(reponse.json()[0]['source'], 'photon')


class RechercheFusionVilleTests(TestCase):
    """Ville (referentiel villes/villages, cf. import_villes_gpkg) est une
    troisieme source fusionnee dans /places/search/, au meme titre que Lieu
    et Photon -- dedupliquee et classee dans le meme pipeline."""

    def setUp(self):
        patcher_photon(self, rechercher=[])

    def test_ville_correspondante_apparait_avec_sa_source(self):
        creer_ville('Douala', 4.0483, 9.7043, type='city', population=2_000_000)
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Douala'})
        sources = {(r['label'], r['source']) for r in reponse.json()}
        self.assertIn(('Douala', 'ville'), sources)

    def test_ville_dedupliquee_avec_un_lieu_du_meme_nom(self):
        creer_lieu('Douala', 4.0483, 9.7043)
        creer_ville('Douala', 4.0483, 9.7043, type='city')
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Douala'})
        corps = reponse.json()
        self.assertEqual(sum(1 for r in corps if r['label'] == 'Douala'), 1)
        self.assertEqual(corps[0]['source'], 'local')  # Lieu passe en premier dans la fusion

    def test_ville_dedupliquee_avec_un_resultat_photon(self):
        patcher_photon(self, rechercher=[_resultat_photon_factice('Douala')])
        creer_ville('Douala', 4.0483, 9.7043, type='city')
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Douala'})
        corps = reponse.json()
        self.assertEqual(sum(1 for r in corps if r['label'] == 'Douala'), 1)
        self.assertEqual(corps[0]['source'], 'ville')  # Ville passe avant Photon dans la fusion

    def test_ville_avec_position_annote_la_distance(self):
        creer_ville('Douala', 4.0483, 9.7043, type='city')
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Douala', 'lat': '4.05', 'lon': '9.70'})
        resultats = reponse.json()
        self.assertIsInstance(resultats[0]['distance_m'], (int, float))

    def test_plafond_villes_applique(self):
        for i in range(LIMITE_VILLES + 3):
            creer_ville(f'Doualaville{i}', 4.05 + i * 0.01, 9.70, type='village')
        reponse = self.client.get(reverse('places:recherche'), {'q': 'Doualaville'})
        sources_ville = [r for r in reponse.json() if r['source'] == 'ville']
        self.assertEqual(len(sources_ville), LIMITE_VILLES)


class InverseNominatimTests(TestCase):
    def setUp(self):
        self.palais = creer_lieu('Palais des Congres', 3.8690, 11.5174, ville='Yaounde')

    def test_nominatim_reussit_est_priorise_sur_le_local(self):
        patcher_nominatim(self, inverser={
            'id': 'nominatim:99', 'label': 'Avenue Kennedy', 'sublabel': 'Yaounde',
            'category': 'road', 'lat': 3.8690, 'lon': 11.5174, 'distance_m': None, 'source': 'nominatim',
        })
        reponse = self.client.get(reverse('places:inverse'), {'lat': '3.8690', 'lon': '11.5174'})
        self.assertEqual(reponse.json()['label'], 'Avenue Kennedy')
        self.assertEqual(reponse.json()['place']['source'], 'nominatim')

    def test_nominatim_sans_resultat_retombe_sur_le_lieu_local(self):
        patcher_nominatim(self, inverser=None)  # aucun resultat externe (pas une panne)
        reponse = self.client.get(reverse('places:inverse'), {'lat': '3.8690', 'lon': '11.5174'})
        self.assertEqual(reponse.json()['label'], 'Palais des Congres')
        self.assertEqual(reponse.json()['place']['source'], 'local')


class NormalisationVilleNominatimTests(TestCase):
    """Cas reels observes en verifiant l'endpoint incidents/city/ : sur les
    donnees OSM du Cameroun, la granularite de `address.city` varie d'une
    ville a l'autre -- cf. PREFIXE_COMMUNAUTE_URBAINE dans client_nominatim.py."""

    def _inverser_avec_adresse(self, adresse):
        reponse_simulee = Mock(status_code=200)
        reponse_simulee.json.return_value = {
            'place_id': 1, 'name': 'Test', 'lat': '4.0', 'lon': '9.7', 'address': adresse,
        }
        reponse_simulee.raise_for_status = lambda: None
        with patch('places.services.client_nominatim.requests.get', return_value=reponse_simulee):
            return ClientNominatim().inverser(4.0, 9.7)

    def test_yaounde_city_directement_prefixe_communaute_urbaine(self):
        resultat = self._inverser_avec_adresse({'city_district': 'Yaounde I', 'city': 'Communauté urbaine de Yaoundé'})
        self.assertEqual(resultat['city'], 'Yaoundé')

    def test_douala_nom_usuel_dans_municipality_pas_city(self):
        resultat = self._inverser_avec_adresse({'city': 'Douala I', 'municipality': 'Communauté urbaine de Douala'})
        self.assertEqual(resultat['city'], 'Douala')

    def test_sans_municipality_ni_prefixe_city_garde_tel_quel(self):
        resultat = self._inverser_avec_adresse({'city': 'Bafoussam'})
        self.assertEqual(resultat['city'], 'Bafoussam')

    def test_aucun_champ_ville_renvoie_chaine_vide(self):
        resultat = self._inverser_avec_adresse({})
        self.assertEqual(resultat['city'], '')


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
        self.assertEqual(resultats[0]['label'], 'Marche Central')
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
        self.assertEqual(resultats[0]['label'], 'Marche Central')
        self.assertIsNone(cache.get(CLE_ECHECS_PHOTON))


class ImportVillesGpkgTests(TestCase):
    """La commande passe par ogr2ogr (subprocess) plutot que de decoder le
    binaire GeoPackage -- simule ici en interceptant l'appel et en ecrivant
    le CSV attendu au chemin temporaire que la commande lui passe, pas
    besoin d'un vrai fichier .gpkg pour ces tests."""

    def _simuler_ogr2ogr(self, contenu_csv):
        def _side_effect(args, **kwargs):
            chemin_tmp = args[3]  # ['ogr2ogr', '-f', 'CSV', <tmp.name>, chemin, couche, ...]
            with open(chemin_tmp, 'w', encoding='utf-8') as f:
                f.write(contenu_csv)
            return Mock(returncode=0, stderr='')
        return _side_effect

    def test_importe_les_villes_du_csv(self):
        contenu = (
            'X,Y,osm_id,code,fclass,population,name\n'
            '9.7043,4.0483,"111",1001,city,2000000,Douala\n'
            '11.5167,3.8667,"222",1001,city,1817524,Yaound\xe9\n'
        )
        with patch(
            'places.management.commands.import_villes_gpkg.subprocess.run',
            side_effect=self._simuler_ogr2ogr(contenu),
        ):
            call_command('import_villes_gpkg', 'fake.gpkg')

        self.assertEqual(Ville.objects.count(), 2)
        douala = Ville.objects.get(osm_id=111)
        self.assertEqual(douala.nom, 'Douala')
        self.assertEqual(douala.nom_normalise, 'douala')
        self.assertEqual(douala.type, 'city')
        self.assertEqual(douala.population, 2000000)
        self.assertAlmostEqual(douala.position.x, 9.7043)
        self.assertAlmostEqual(douala.position.y, 4.0483)

    def test_lignes_sans_nom_ignorees(self):
        contenu = 'X,Y,osm_id,code,fclass,population,name\n9.7043,4.0483,"111",1050,locality,0,\n'
        with patch(
            'places.management.commands.import_villes_gpkg.subprocess.run',
            side_effect=self._simuler_ogr2ogr(contenu),
        ):
            call_command('import_villes_gpkg', 'fake.gpkg')
        self.assertEqual(Ville.objects.count(), 0)

    def test_reimport_met_a_jour_plutot_que_dupliquer(self):
        contenu = 'X,Y,osm_id,code,fclass,population,name\n9.7043,4.0483,"111",1001,city,2000000,Douala\n'
        with patch(
            'places.management.commands.import_villes_gpkg.subprocess.run',
            side_effect=self._simuler_ogr2ogr(contenu),
        ):
            call_command('import_villes_gpkg', 'fake.gpkg')
            call_command('import_villes_gpkg', 'fake.gpkg')
        self.assertEqual(Ville.objects.count(), 1)

    def test_echec_ogr2ogr_leve_commanderror(self):
        with patch(
            'places.management.commands.import_villes_gpkg.subprocess.run',
            return_value=Mock(returncode=1, stderr='fichier introuvable'),
        ):
            with self.assertRaises(CommandError):
                call_command('import_villes_gpkg', 'fake.gpkg')
