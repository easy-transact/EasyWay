from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Formule, Parametres, Utilisateur
from accounts.tests import connecter

from .exceptions import TransitionInvalide
from .models import StatutTrajet, Trajet
from .polyline import decoder_polyline6, encoder_polyline6
from .services.client_routage import ClientRoutage
from .services.client_valhalla import (
    CLE_ECHECS,
    CLE_OUVERT_JUSQU_A,
    SEUIL_ECHECS,
    ClientValhalla,
)
from .services.service_itineraire import ServiceItineraire

MOT_DE_PASSE = 'CorrectHorse9!'
GEOMETRIE_TEST = encoder_polyline6([(9.7043, 4.0483), (9.6970, 4.0469)])


def creer_utilisateur(email='user@easyway.local', **extra):
    utilisateur = Utilisateur.objects.create_user(
        email=email, password=MOT_DE_PASSE, nom_complet='Test User', **extra
    )
    Parametres.objects.create(utilisateur=utilisateur)
    return utilisateur


def trip_factice(distance_km=1.28, duree_s=108.0, avec_alternate=False):
    trip = {
        'summary': {'length': distance_km, 'time': duree_s},
        'legs': [{
            'shape': GEOMETRIE_TEST,
            'maneuvers': [
                {
                    'type': 3, 'instruction': 'Conduisez vers le nord.',
                    'verbal_post_transition_instruction': 'Continuez pendant 100 metres.',
                    'length': 0.1, 'time': 10, 'street_names': ['Avenue Test'],
                },
            ],
        }],
    }
    corps = {'trip': trip}
    if avec_alternate:
        corps['alternates'] = [{'trip': {**trip, 'summary': {'length': distance_km + 0.5, 'time': duree_s + 30}}}]
    return corps


class PolylineTests(TestCase):
    def test_aller_retour_encode_decode(self):
        points = [(9.7043, 4.0483), (9.6970, 4.0469), (9.700, 4.050)]
        encode = encoder_polyline6(points)
        decode = decoder_polyline6(encode)
        for (lon_a, lat_a), (lon_b, lat_b) in zip(points, decode):
            self.assertAlmostEqual(lon_a, lon_b, places=5)
            self.assertAlmostEqual(lat_a, lat_b, places=5)


class MachineAEtatsTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()
        self.trajet = Trajet.objects.create(
            utilisateur=self.utilisateur,
            position_origine='POINT(9.7043 4.0483)',
            libelle_origine='Marche Central',
            position_destination='POINT(9.6970 4.0469)',
            libelle_destination='Hopital General',
            itineraire_choisi='abc123',
            distance_prevue=1280,
            duree_prevue=108,
        )

    def test_planifie_vers_actif_autorise(self):
        self.trajet.changer_statut(StatutTrajet.ACTIF)
        self.assertEqual(self.trajet.statut, StatutTrajet.ACTIF)
        self.assertIsNotNone(self.trajet.demarre_le)

    def test_actif_vers_termine_autorise(self):
        self.trajet.changer_statut(StatutTrajet.ACTIF)
        self.trajet.changer_statut(StatutTrajet.TERMINE)
        self.assertEqual(self.trajet.statut, StatutTrajet.TERMINE)
        self.assertIsNotNone(self.trajet.termine_le)

    def test_termine_vers_actif_refuse(self):
        self.trajet.changer_statut(StatutTrajet.ACTIF)
        self.trajet.changer_statut(StatutTrajet.TERMINE)
        with self.assertRaises(TransitionInvalide):
            self.trajet.changer_statut(StatutTrajet.ACTIF)

    def test_annule_est_terminal(self):
        self.trajet.changer_statut(StatutTrajet.ANNULE)
        with self.assertRaises(TransitionInvalide):
            self.trajet.changer_statut(StatutTrajet.ACTIF)

    def test_meme_statut_est_un_noop(self):
        self.trajet.changer_statut(StatutTrajet.ACTIF)
        self.trajet.changer_statut(StatutTrajet.ACTIF)  # ne doit pas lever
        self.assertEqual(self.trajet.statut, StatutTrajet.ACTIF)


class ClientFactice(ClientRoutage):
    def __init__(self):
        self.appels = 0

    def calculer_itineraires(self, depart, arrivee, options):
        self.appels += 1
        return [trip_factice()['trip']]

    def replier(self, depart, arrivee):
        return [{'summary': {'length': 0, 'time': 0}, 'legs': [{'shape': '', 'maneuvers': []}], 'degrade': True}]


class ServiceItineraireTests(TestCase):
    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()

    def test_traduction_costing_selon_type_vehicule(self):
        self.utilisateur.type_vehicule = 'MOTO'
        service = ServiceItineraire(client=ClientFactice())
        options = service._options_depuis_parametres(self.utilisateur)
        self.assertEqual(options['costing'], 'motorcycle')

    def test_eviter_peages_change_les_options_de_cout(self):
        service = ServiceItineraire(client=ClientFactice())

        self.utilisateur.parametres.eviter_peages = True
        options_evite = service._options_depuis_parametres(self.utilisateur)
        self.utilisateur.parametres.eviter_peages = False
        options_autorise = service._options_depuis_parametres(self.utilisateur)

        costing = options_evite['costing']
        self.assertEqual(options_evite['costing_options'][costing]['use_tolls'], 0.0)
        self.assertEqual(options_autorise['costing_options'][costing]['use_tolls'], 1.0)

    def test_resultat_est_mis_en_cache(self):
        client = ClientFactice()
        service = ServiceItineraire(client=client)
        depart, arrivee = (4.0483, 9.7043), (4.0469, 9.6970)

        service.calculer(depart, arrivee, self.utilisateur)
        service.calculer(depart, arrivee, self.utilisateur)

        self.assertEqual(client.appels, 1)

    def test_candidats_normalises_ont_la_forme_attendue(self):
        service = ServiceItineraire(client=ClientFactice())
        candidats = service.calculer((4.0483, 9.7043), (4.0469, 9.6970), self.utilisateur)
        self.assertEqual(len(candidats), 1)
        candidat = candidats[0]
        self.assertTrue(candidat['est_recommande'])
        self.assertEqual(candidat['distance'], 1280)
        self.assertEqual(len(candidat['manoeuvres']), 1)


class DisjoncteurValhallaTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_bascule_sur_repli_apres_echecs_repetes(self):
        client = ClientValhalla()
        with patch('trips.services.client_valhalla.requests.post', side_effect=requests.exceptions.ConnectionError('down')):
            for _ in range(SEUIL_ECHECS):
                resultat = client.calculer_itineraires((4.0, 9.7), (4.01, 9.71), {'costing': 'auto'})
                self.assertTrue(resultat[0]['degrade'])

        self.assertIsNotNone(cache.get(CLE_OUVERT_JUSQU_A))

        # Disjoncteur ouvert : replier() sans meme tenter l'appel reseau.
        with patch('trips.services.client_valhalla.requests.post') as post_simule:
            resultat = client.calculer_itineraires((4.0, 9.7), (4.01, 9.71), {'costing': 'auto'})
            post_simule.assert_not_called()
            self.assertTrue(resultat[0]['degrade'])

    def test_succes_reinitialise_le_compteur_dechecs(self):
        client = ClientValhalla()
        cache.set(CLE_ECHECS, SEUIL_ECHECS - 1, timeout=60)

        reponse_simulee = Mock(status_code=200)
        reponse_simulee.json.return_value = trip_factice()
        reponse_simulee.raise_for_status = lambda: None
        with patch('trips.services.client_valhalla.requests.post', return_value=reponse_simulee):
            resultat = client.calculer_itineraires((4.0, 9.7), (4.01, 9.71), {'costing': 'auto'})

        self.assertFalse(resultat[0].get('degrade', False))
        self.assertIsNone(cache.get(CLE_ECHECS))

    def test_replier_produit_une_forme_compatible_avec_normalisation(self):
        service = ServiceItineraire(client=ClientValhalla())
        with patch('trips.services.client_valhalla.requests.post', side_effect=requests.exceptions.ConnectionError('down')):
            candidats = service.calculer((4.0483, 9.7043), (4.0469, 9.6970), creer_utilisateur('repli@easyway.local'))
        self.assertEqual(len(candidats), 1)
        self.assertTrue(candidats[0]['degrade'])
        self.assertGreater(candidats[0]['distance'], 0)


def _trajet_actif_pour(utilisateur, **overrides):
    valeurs = dict(
        utilisateur=utilisateur,
        position_origine='POINT(9.7043 4.0483)',
        libelle_origine='Marche Central',
        position_destination='POINT(9.6970 4.0469)',
        libelle_destination='Hopital General',
        itineraire_choisi='abc123',
        distance_prevue=1280,
        duree_prevue=108,
        statut=StatutTrajet.ACTIF,
        demarre_le=timezone.now(),
    )
    valeurs.update(overrides)
    return Trajet.objects.create(**valeurs)


class TrajetApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.email)

    def test_calcul_itineraire_authentifie(self):
        with patch(
            'trips.services.client_valhalla.ClientValhalla.calculer_itineraires',
            return_value=[trip_factice()['trip']],
        ):
            reponse = self.client.post(
                reverse('trips:calculer-itineraire'),
                {'origine_lat': 4.0483, 'origine_lon': 9.7043, 'destination_lat': 4.0469, 'destination_lon': 9.6970},
                content_type='application/json',
                **self.jetons,
            )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(len(reponse.json()), 1)
        self.assertTrue(reponse.json()[0]['est_recommande'])

    def test_calcul_itineraire_non_authentifie_rejete(self):
        reponse = self.client.post(
            reverse('trips:calculer-itineraire'),
            {'origine_lat': 4.0, 'origine_lon': 9.7, 'destination_lat': 4.01, 'destination_lon': 9.71},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 401)

    def _payload_creation(self):
        candidat = ServiceItineraire()._normaliser_trip(trip_factice()['trip'], 0)
        return {
            'libelle_origine': 'Marche Central',
            'origine_lat': 4.0483, 'origine_lon': 9.7043,
            'libelle_destination': 'Hopital General',
            'destination_lat': 4.0469, 'destination_lon': 9.6970,
            'itineraire': candidat,
        }

    def test_creation_trajet_demarre_actif(self):
        reponse = self.client.post(
            reverse('trips:trajets'), self._payload_creation(), content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 201)
        corps = reponse.json()
        self.assertEqual(corps['statut'], StatutTrajet.ACTIF)
        self.assertEqual(len(corps['itineraires']), 1)
        self.assertEqual(len(corps['itineraires'][0]['manoeuvres']), 1)

    def test_patch_transition_illegale_rejetee(self):
        trajet = _trajet_actif_pour(self.utilisateur)
        trajet.changer_statut(StatutTrajet.TERMINE)

        reponse = self.client.patch(
            reverse('trips:trajet-detail', kwargs={'id': trajet.id}),
            {'statut': StatutTrajet.ACTIF},
            content_type='application/json',
            **self.jetons,
        )
        self.assertEqual(reponse.status_code, 400)

    def test_patch_transition_legale_acceptee(self):
        trajet = _trajet_actif_pour(self.utilisateur)
        reponse = self.client.patch(
            reverse('trips:trajet-detail', kwargs={'id': trajet.id}),
            {'statut': StatutTrajet.TERMINE, 'distance_reelle': 1300, 'duree_reelle': 130},
            content_type='application/json',
            **self.jetons,
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['statut'], StatutTrajet.TERMINE)
        self.assertEqual(reponse.json()['distance_reelle'], 1300)

    def test_noter_trajet_termine(self):
        trajet = _trajet_actif_pour(self.utilisateur)
        trajet.changer_statut(StatutTrajet.TERMINE)
        reponse = self.client.post(
            reverse('trips:trajet-note', kwargs={'id': trajet.id}),
            {'note': 5, 'commentaire': 'Parfait'},
            content_type='application/json',
            **self.jetons,
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['note'], 5)

    def test_noter_trajet_actif_refuse(self):
        trajet = _trajet_actif_pour(self.utilisateur)
        reponse = self.client.post(
            reverse('trips:trajet-note', kwargs={'id': trajet.id}),
            {'note': 5},
            content_type='application/json',
            **self.jetons,
        )
        self.assertEqual(reponse.status_code, 400)

    def test_isolation_entre_utilisateurs(self):
        autre = creer_utilisateur('autre@easyway.local')
        trajet = _trajet_actif_pour(autre)
        reponse = self.client.get(reverse('trips:trajet-detail', kwargs={'id': trajet.id}), **self.jetons)
        self.assertEqual(reponse.status_code, 404)

    def test_delete_trajet(self):
        trajet = _trajet_actif_pour(self.utilisateur)
        reponse = self.client.delete(reverse('trips:trajet-detail', kwargs={'id': trajet.id}), **self.jetons)
        self.assertEqual(reponse.status_code, 204)
        self.assertFalse(Trajet.objects.filter(id=trajet.id).exists())


class RetentionTrajetsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.email)

    def _lister(self, periode=None):
        params = {'periode': periode} if periode else {}
        return self.client.get(reverse('trips:trajets'), params, **self.jetons)

    def test_gratuite_tronque_a_30_jours(self):
        _trajet_actif_pour(self.utilisateur, demarre_le=timezone.now() - timezone.timedelta(days=5))
        _trajet_actif_pour(self.utilisateur, demarre_le=timezone.now() - timezone.timedelta(days=45))

        reponse = self._lister('tout')
        corps = reponse.json()
        self.assertEqual(len(corps['resultats']), 1)
        self.assertIsNotNone(corps['tronque_le'])

    def test_premium_conserve_plus_longtemps(self):
        self.utilisateur.formule = Formule.PREMIUM
        self.utilisateur.save(update_fields=['formule'])
        _trajet_actif_pour(self.utilisateur, demarre_le=timezone.now() - timezone.timedelta(days=45))

        reponse = self._lister('tout')
        corps = reponse.json()
        self.assertEqual(len(corps['resultats']), 1)
        self.assertIsNone(corps['tronque_le'])

    def test_periode_semaine_filtre(self):
        _trajet_actif_pour(self.utilisateur, demarre_le=timezone.now() - timezone.timedelta(days=2))
        _trajet_actif_pour(self.utilisateur, demarre_le=timezone.now() - timezone.timedelta(days=10))

        reponse = self._lister('semaine')
        self.assertEqual(len(reponse.json()['resultats']), 1)

    def test_periode_invalide_rejetee(self):
        reponse = self._lister('annee')
        self.assertEqual(reponse.status_code, 400)
