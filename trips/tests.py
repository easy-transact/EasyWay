import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import redis
import requests
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Formule, Parametres, Utilisateur
from accounts.tests import connecter

from .exceptions import TransitionInvalide
from .models import FUSEAU_TRAFIC, EchantillonVitesse, NiveauTrafic, StatutTrajet, Trajet
from .polyline import decoder_polyline6, encoder_polyline6
from .services import service_trafic
from .services.client_meili import ErreurMeili
from .services.client_routage import ClientRoutage
from .services.client_valhalla import ClientValhalla
from .services.consommateur_positions import TAILLE_BUCKET_S, ConsommateurPositions
from .services.disjoncteur import CLE_ECHECS, CLE_OUVERT_JUSQU_A, SEUIL_ECHECS, DisjoncteurOuvert
from .services.service_itineraire import ServiceItineraire
from .tasks import MARGE_FLUSH_S, flusher_echantillons_vitesse
from .views import FLUX_POSITIONS

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
        # Le trafic n'est pas ce que ces tests verifient -- evite un appel
        # Meili reseau reel a chaque _normaliser_trip() (cf. ServiceTraficTests
        # pour les tests dedies a service_trafic.evaluer_route).
        patcher = patch(
            'trips.services.service_itineraire.service_trafic.evaluer_route',
            return_value={'niveau_trafic': 'NORMAL', 'duree_avec_trafic': None},
        )
        self.addCleanup(patcher.stop)
        patcher.start()

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

    def test_trafic_du_candidat_reflete_evaluer_route(self):
        with patch(
            'trips.services.service_itineraire.service_trafic.evaluer_route',
            return_value={'niveau_trafic': NiveauTrafic.DENSE, 'duree_avec_trafic': 999},
        ):
            service = ServiceItineraire(client=ClientFactice())
            candidats = service.calculer((4.0483, 9.7043), (4.0469, 9.6970), self.utilisateur)

        self.assertEqual(candidats[0]['niveau_trafic'], NiveauTrafic.DENSE)
        self.assertEqual(candidats[0]['duree_avec_trafic'], 999)

    def test_trajet_degrade_nappelle_pas_evaluer_route(self):
        trip_degrade = {
            'summary': {'length': 1, 'time': 10}, 'legs': [{'shape': '', 'maneuvers': []}], 'degrade': True,
        }
        with patch('trips.services.service_itineraire.service_trafic.evaluer_route') as evaluer:
            candidat = ServiceItineraire()._normaliser_trip(trip_degrade, 0)

        evaluer.assert_not_called()
        self.assertIsNone(candidat['duree_avec_trafic'])
        self.assertEqual(candidat['niveau_trafic'], NiveauTrafic.NORMAL)


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
        # Idem ServiceItineraireTests -- evite un appel Meili reseau reel.
        patcher = patch(
            'trips.services.service_itineraire.service_trafic.evaluer_route',
            return_value={'niveau_trafic': 'NORMAL', 'duree_avec_trafic': None},
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_calcul_itineraire_authentifie(self):
        with patch(
            'trips.services.client_valhalla.ClientValhalla.calculer_itineraires',
            return_value=[trip_factice()['trip']],
        ):
            reponse = self.client.post(
                reverse('trips:calculer-itineraire'),
                {'origin_lat': 4.0483, 'origin_lon': 9.7043, 'destination_lat': 4.0469, 'destination_lon': 9.6970},
                content_type='application/json',
                **self.jetons,
            )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(len(reponse.json()), 1)
        self.assertTrue(reponse.json()[0]['is_recommended'])

    def test_calcul_itineraire_non_authentifie_rejete(self):
        reponse = self.client.post(
            reverse('trips:calculer-itineraire'),
            {'origin_lat': 4.0, 'origin_lon': 9.7, 'destination_lat': 4.01, 'destination_lon': 9.71},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 401)

    def _route_depuis_candidat(self, candidat):
        """Le dict brut renvoye par _normaliser_trip() (cles francaises,
        jamais serialise a cette etape) doit etre traduit en forme anglaise
        pour etre resoumis tel qu'un client le ferait a POST /api/trips/."""
        return {
            'route_id': candidat['identifiant'],
            'label': candidat['libelle'],
            'distance': candidat['distance'],
            'duration': candidat['duree'],
            'duration_with_traffic': candidat['duree_avec_trafic'],
            'traffic_level': candidat['niveau_trafic'],
            'geometry': candidat['geometrie'],
            'is_recommended': candidat['est_recommande'],
            'maneuvers': [
                {
                    'type': m['type'], 'instruction': m['instruction'],
                    'voice_instruction': m['instruction_vocale'], 'distance': m['distance'],
                    'duration': m['duree'], 'street_name': m['nom_voie'],
                }
                for m in candidat['manoeuvres']
            ],
            'degraded': candidat.get('degrade', False),
        }

    def _payload_creation(self):
        candidat = ServiceItineraire()._normaliser_trip(trip_factice()['trip'], 0)
        return {
            'origin_label': 'Marche Central',
            'origin_lat': 4.0483, 'origin_lon': 9.7043,
            'destination_label': 'Hopital General',
            'destination_lat': 4.0469, 'destination_lon': 9.6970,
            'route': self._route_depuis_candidat(candidat),
        }

    def test_creation_trajet_demarre_actif(self):
        reponse = self.client.post(
            reverse('trips:trajets'), self._payload_creation(), content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 201)
        corps = reponse.json()
        self.assertEqual(corps['status'], StatutTrajet.ACTIF)
        self.assertEqual(len(corps['routes']), 1)
        self.assertEqual(len(corps['routes'][0]['maneuvers']), 1)

    def test_patch_transition_illegale_rejetee(self):
        trajet = _trajet_actif_pour(self.utilisateur)
        trajet.changer_statut(StatutTrajet.TERMINE)

        reponse = self.client.patch(
            reverse('trips:trajet-detail', kwargs={'id': trajet.id}),
            {'status': StatutTrajet.ACTIF},
            content_type='application/json',
            **self.jetons,
        )
        self.assertEqual(reponse.status_code, 400)

    def test_patch_transition_legale_acceptee(self):
        trajet = _trajet_actif_pour(self.utilisateur)
        reponse = self.client.patch(
            reverse('trips:trajet-detail', kwargs={'id': trajet.id}),
            {'status': StatutTrajet.TERMINE, 'actual_distance': 1300, 'actual_duration': 130},
            content_type='application/json',
            **self.jetons,
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['status'], StatutTrajet.TERMINE)
        self.assertEqual(reponse.json()['actual_distance'], 1300)

    def test_noter_trajet_termine(self):
        trajet = _trajet_actif_pour(self.utilisateur)
        trajet.changer_statut(StatutTrajet.TERMINE)
        reponse = self.client.post(
            reverse('trips:trajet-note', kwargs={'id': trajet.id}),
            {'rating': 5, 'comment': 'Parfait'},
            content_type='application/json',
            **self.jetons,
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['rating'], 5)

    def test_noter_trajet_actif_refuse(self):
        trajet = _trajet_actif_pour(self.utilisateur)
        reponse = self.client.post(
            reverse('trips:trajet-note', kwargs={'id': trajet.id}),
            {'rating': 5},
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
        params = {'period': periode} if periode else {}
        return self.client.get(reverse('trips:trajets'), params, **self.jetons)

    def test_gratuite_tronque_a_30_jours(self):
        _trajet_actif_pour(self.utilisateur, demarre_le=timezone.now() - timezone.timedelta(days=5))
        _trajet_actif_pour(self.utilisateur, demarre_le=timezone.now() - timezone.timedelta(days=45))

        reponse = self._lister('all')
        corps = reponse.json()
        self.assertEqual(len(corps['results']), 1)
        self.assertIsNotNone(corps['truncated_at'])

    def test_premium_conserve_plus_longtemps(self):
        self.utilisateur.formule = Formule.PREMIUM
        self.utilisateur.save(update_fields=['formule'])
        _trajet_actif_pour(self.utilisateur, demarre_le=timezone.now() - timezone.timedelta(days=45))

        reponse = self._lister('all')
        corps = reponse.json()
        self.assertEqual(len(corps['results']), 1)
        self.assertIsNone(corps['truncated_at'])

    def test_periode_semaine_filtre(self):
        _trajet_actif_pour(self.utilisateur, demarre_le=timezone.now() - timezone.timedelta(days=2))
        _trajet_actif_pour(self.utilisateur, demarre_le=timezone.now() - timezone.timedelta(days=10))

        reponse = self._lister('week')
        self.assertEqual(len(reponse.json()['results']), 1)

    def test_periode_invalide_rejetee(self):
        reponse = self._lister('year')
        self.assertEqual(reponse.status_code, 400)


class ProducteurRedisStreamsTests(TestCase):
    def test_publier_encode_en_json_avec_maxlen_approximatif(self):
        from .services.producteur_evenements import ProducteurRedisStreams

        connexion = Mock()
        producteur = ProducteurRedisStreams(connexion=connexion, longueur_max=1000)
        producteur.publier('un-flux', {'trajet_id': 'abc', 'lat': 4.05})

        connexion.xadd.assert_called_once()
        flux, champs = connexion.xadd.call_args.args
        self.assertEqual(flux, 'un-flux')
        self.assertEqual(json.loads(champs['donnees']), {'trajet_id': 'abc', 'lat': 4.05})
        self.assertEqual(connexion.xadd.call_args.kwargs, {'maxlen': 1000, 'approximate': True})


class TelemetriePositionsApiTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.email)
        self.trajet = _trajet_actif_pour(self.utilisateur)

    def _lot(self, **overrides):
        payload = {
            'trip': str(self.trajet.id),
            'positions': [
                {'lat': 4.0483, 'lon': 9.7043, 'speed_kmh': 32.5, 'heading': 180, 'timestamp': '2026-01-01T10:00:00Z'},
                {'lat': 4.0480, 'lon': 9.7040, 'timestamp': '2026-01-01T10:00:05Z'},
            ],
        }
        payload.update(overrides)
        return payload

    def test_non_authentifie_rejete(self):
        reponse = self.client.post(
            reverse('trips:telemetrie-positions'), self._lot(), content_type='application/json'
        )
        self.assertEqual(reponse.status_code, 401)

    def test_lot_publie_une_entree_par_position_sans_identifiant_utilisateur(self):
        with patch('trips.views.ProducteurRedisStreams') as ClasseProducteur:
            producteur = ClasseProducteur.return_value
            reponse = self.client.post(
                reverse('trips:telemetrie-positions'), self._lot(), content_type='application/json', **self.jetons
            )

        self.assertEqual(reponse.status_code, 202)
        self.assertEqual(producteur.publier.call_count, 2)
        for appel in producteur.publier.call_args_list:
            flux, evenement = appel.args
            self.assertEqual(flux, FLUX_POSITIONS)
            self.assertEqual(evenement['trajet_id'], str(self.trajet.id))
            self.assertNotIn('utilisateur_id', evenement)
            self.assertNotIn('user_id', evenement)

    def test_mode_invisible_ne_publie_rien(self):
        self.utilisateur.mode_invisible = True
        self.utilisateur.save(update_fields=['mode_invisible'])

        with patch('trips.views.ProducteurRedisStreams') as ClasseProducteur:
            reponse = self.client.post(
                reverse('trips:telemetrie-positions'), self._lot(), content_type='application/json', **self.jetons
            )

        self.assertEqual(reponse.status_code, 202)
        ClasseProducteur.return_value.publier.assert_not_called()

    def test_trajet_dun_autre_utilisateur_rejete(self):
        autre = creer_utilisateur('autre@easyway.local')
        trajet_autre = _trajet_actif_pour(autre)

        with patch('trips.views.ProducteurRedisStreams') as ClasseProducteur:
            reponse = self.client.post(
                reverse('trips:telemetrie-positions'),
                self._lot(trip=str(trajet_autre.id)),
                content_type='application/json',
                **self.jetons,
            )

        self.assertEqual(reponse.status_code, 400)
        ClasseProducteur.return_value.publier.assert_not_called()

    def test_trajet_non_actif_rejete(self):
        self.trajet.changer_statut(StatutTrajet.TERMINE)

        reponse = self.client.post(
            reverse('trips:telemetrie-positions'), self._lot(), content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 400)

    def test_lot_vide_rejete(self):
        reponse = self.client.post(
            reverse('trips:telemetrie-positions'),
            self._lot(positions=[]),
            content_type='application/json',
            **self.jetons,
        )
        self.assertEqual(reponse.status_code, 400)


def _evenement(trajet_id, lat, lon, horodatage):
    return {'trajet_id': trajet_id, 'lat': lat, 'lon': lon, 'vitesse_kmh': None, 'cap': None, 'horodatage': horodatage}


class ConsommateurPositionsTests(TestCase):
    def _consommateur(self, connexion):
        connexion.xgroup_create.return_value = True
        connexion.xautoclaim.return_value = ('0-0', [], [])
        return ConsommateurPositions(connexion=connexion, nom_consommateur='test-worker')

    def test_ignore_busygroup_a_la_creation_du_groupe(self):
        connexion = MagicMock()
        connexion.xgroup_create.side_effect = redis.ResponseError('BUSYGROUP Consumer Group name already exists')
        ConsommateurPositions(connexion=connexion, nom_consommateur='test-worker')  # ne doit pas lever

    def test_propage_une_erreur_redis_non_busygroup(self):
        connexion = MagicMock()
        connexion.xgroup_create.side_effect = redis.ResponseError('WRONGTYPE mauvais type de cle')
        with self.assertRaises(redis.ResponseError):
            ConsommateurPositions(connexion=connexion, nom_consommateur='test-worker')

    def test_sans_message_ne_fait_rien(self):
        connexion = MagicMock()
        connexion.xreadgroup.return_value = []
        consommateur = self._consommateur(connexion)

        self.assertEqual(consommateur.consommer(), 0)
        connexion.xack.assert_not_called()

    def test_lit_les_orphelins_avant_les_nouveaux_messages(self):
        connexion = MagicMock()
        consommateur = self._consommateur(connexion)
        connexion.xreadgroup.return_value = []

        consommateur.consommer()

        connexion.xautoclaim.assert_called_once_with(
            FLUX_POSITIONS, 'consommateurs-trafic', 'test-worker',
            min_idle_time=30_000, start_id='0', count=200,
        )
        connexion.xreadgroup.assert_called_once_with(
            'consommateurs-trafic', 'test-worker', {FLUX_POSITIONS: '>'}, count=200,
        )

    def test_deux_positions_sur_la_meme_arete_accumulent_et_acquittent(self):
        connexion = MagicMock()
        consommateur = self._consommateur(connexion)
        connexion.xreadgroup.return_value = [(
            FLUX_POSITIONS,
            [
                ('1-1', {'donnees': json.dumps(_evenement('t1', 4.05, 9.70, '2026-01-01T10:00:00+00:00'))}),
                ('1-2', {'donnees': json.dumps(_evenement('t1', 4.06, 9.71, '2026-01-01T10:01:00+00:00'))}),
            ],
        )]
        # Arete de 1km, la moitie parcourue en 60s -- 500m/60s = 30 km/h tout rond.
        edges = [{'id': 42, 'length': 1.0}]
        matched_points = [
            {'edge_index': 0, 'distance_along_edge': 0.0},
            {'edge_index': 0, 'distance_along_edge': 0.5},
        ]

        with patch('trips.services.consommateur_positions.tracer', return_value=(edges, matched_points)) as trace:
            nb = consommateur.consommer()

        self.assertEqual(nb, 2)
        trace.assert_called_once()
        points_envoyes = trace.call_args.args[0]
        self.assertEqual(len(points_envoyes), 2)
        self.assertEqual(points_envoyes[0]['time'], 0)
        self.assertEqual(points_envoyes[1]['time'], 60)

        bucket = ConsommateurPositions._bucket_5min(ConsommateurPositions._parser_horodatage('2026-01-01T10:00:00+00:00'))
        pipe = connexion.pipeline.return_value.__enter__.return_value
        pipe.hincrbyfloat.assert_called_once_with(f'trafic:accumulateur:42:{bucket}', 'somme_vitesse', 30.0)
        pipe.hincrby.assert_called_once_with(f'trafic:accumulateur:42:{bucket}', 'nombre', 1)
        connexion.xack.assert_called_once_with(FLUX_POSITIONS, 'consommateurs-trafic', '1-1', '1-2')

    def test_correspondance_vide_acquitte_sans_accumuler(self):
        connexion = MagicMock()
        consommateur = self._consommateur(connexion)
        connexion.xreadgroup.return_value = [(
            FLUX_POSITIONS,
            [
                ('2-1', {'donnees': json.dumps(_evenement('t2', 4.05, 9.70, '2026-01-01T10:00:00+00:00'))}),
                ('2-2', {'donnees': json.dumps(_evenement('t2', 4.06, 9.71, '2026-01-01T10:01:00+00:00'))}),
            ],
        )]
        matched_points = [{'edge_index': None}, {'edge_index': None}]

        with patch('trips.services.consommateur_positions.tracer', return_value=([], matched_points)):
            nb = consommateur.consommer()

        self.assertEqual(nb, 2)
        connexion.pipeline.assert_not_called()
        connexion.xack.assert_called_once_with(FLUX_POSITIONS, 'consommateurs-trafic', '2-1', '2-2')

    def test_echec_meili_laisse_les_messages_en_pending(self):
        connexion = MagicMock()
        consommateur = self._consommateur(connexion)
        connexion.xreadgroup.return_value = [(
            FLUX_POSITIONS,
            [
                ('3-1', {'donnees': json.dumps(_evenement('t3', 4.05, 9.70, '2026-01-01T10:00:00+00:00'))}),
                ('3-2', {'donnees': json.dumps(_evenement('t3', 4.06, 9.71, '2026-01-01T10:01:00+00:00'))}),
            ],
        )]

        with patch('trips.services.consommateur_positions.tracer', side_effect=ErreurMeili('valhalla en panne')):
            nb = consommateur.consommer()

        self.assertEqual(nb, 2)
        connexion.xack.assert_not_called()

    def test_disjoncteur_ouvert_laisse_aussi_les_messages_en_pending(self):
        connexion = MagicMock()
        consommateur = self._consommateur(connexion)
        connexion.xreadgroup.return_value = [(
            FLUX_POSITIONS,
            [
                ('4-1', {'donnees': json.dumps(_evenement('t4', 4.05, 9.70, '2026-01-01T10:00:00+00:00'))}),
                ('4-2', {'donnees': json.dumps(_evenement('t4', 4.06, 9.71, '2026-01-01T10:01:00+00:00'))}),
            ],
        )]

        with patch('trips.services.consommateur_positions.tracer', side_effect=DisjoncteurOuvert()):
            nb = consommateur.consommer()

        self.assertEqual(nb, 2)
        connexion.xack.assert_not_called()

    def test_position_isolee_acquittee_sans_appel_meili(self):
        connexion = MagicMock()
        consommateur = self._consommateur(connexion)
        connexion.xreadgroup.return_value = [(
            FLUX_POSITIONS,
            [('5-1', {'donnees': json.dumps(_evenement('t5', 4.05, 9.70, '2026-01-01T10:00:00+00:00'))})],
        )]

        with patch('trips.services.consommateur_positions.tracer') as trace:
            nb = consommateur.consommer()

        trace.assert_not_called()
        self.assertEqual(nb, 1)
        connexion.xack.assert_called_once_with(FLUX_POSITIONS, 'consommateurs-trafic', '5-1')

    def test_regroupe_par_trajet_avant_appel_meili(self):
        connexion = MagicMock()
        consommateur = self._consommateur(connexion)
        connexion.xreadgroup.return_value = [(
            FLUX_POSITIONS,
            [
                ('6-1', {'donnees': json.dumps(_evenement('a', 4.05, 9.70, '2026-01-01T10:00:00+00:00'))}),
                ('7-1', {'donnees': json.dumps(_evenement('b', 4.05, 9.70, '2026-01-01T10:00:00+00:00'))}),
                ('6-2', {'donnees': json.dumps(_evenement('a', 4.06, 9.71, '2026-01-01T10:01:00+00:00'))}),
                ('7-2', {'donnees': json.dumps(_evenement('b', 4.06, 9.71, '2026-01-01T10:01:00+00:00'))}),
            ],
        )]

        with patch(
            'trips.services.consommateur_positions.tracer', return_value=([], [{'edge_index': None}] * 2)
        ) as trace:
            nb = consommateur.consommer()

        self.assertEqual(nb, 4)
        self.assertEqual(trace.call_count, 2)  # un appel par trajet, jamais un lot melange
        connexion.xack.assert_called_once_with(
            FLUX_POSITIONS, 'consommateurs-trafic', '6-1', '6-2', '7-1', '7-2',
        )


class ServiceTraficTests(TestCase):
    ARETE = 999

    def test_vitesse_typique_moyenne_ponderee(self):
        EchantillonVitesse.objects.create(
            identifiant_arete=self.ARETE, debut_intervalle=timezone.now() - timezone.timedelta(days=7),
            jour_semaine=2, heure_jour=8, vitesse_moyenne=Decimal('40.00'), nombre_echantillons=2,
        )
        EchantillonVitesse.objects.create(
            identifiant_arete=self.ARETE, debut_intervalle=timezone.now() - timezone.timedelta(days=14),
            jour_semaine=2, heure_jour=8, vitesse_moyenne=Decimal('60.00'), nombre_echantillons=1,
        )
        typique = service_trafic.vitesse_typique(self.ARETE, jour_semaine=2, heure_jour=8)
        self.assertAlmostEqual(float(typique), 140 / 3, places=2)  # (40*2 + 60*1) / 3

    def test_vitesse_typique_sans_historique_est_none(self):
        self.assertIsNone(service_trafic.vitesse_typique(123456, jour_semaine=1, heure_jour=1))

    def test_vitesse_typique_ignore_jour_heure_differents(self):
        EchantillonVitesse.objects.create(
            identifiant_arete=self.ARETE, debut_intervalle=timezone.now() - timezone.timedelta(days=7),
            jour_semaine=3, heure_jour=9, vitesse_moyenne=Decimal('40.00'), nombre_echantillons=2,
        )
        self.assertIsNone(service_trafic.vitesse_typique(self.ARETE, jour_semaine=2, heure_jour=8))

    def test_vitesse_typique_ignore_hors_fenetre_historique(self):
        EchantillonVitesse.objects.create(
            identifiant_arete=self.ARETE,
            debut_intervalle=timezone.now() - service_trafic.FENETRE_HISTORIQUE - timezone.timedelta(days=1),
            jour_semaine=2, heure_jour=8, vitesse_moyenne=Decimal('40.00'), nombre_echantillons=2,
        )
        self.assertIsNone(service_trafic.vitesse_typique(self.ARETE, jour_semaine=2, heure_jour=8))

    def test_vitesse_recente_renvoie_le_dernier_echantillon(self):
        EchantillonVitesse.objects.create(
            identifiant_arete=self.ARETE, debut_intervalle=timezone.now() - timezone.timedelta(minutes=30),
            jour_semaine=2, heure_jour=8, vitesse_moyenne=Decimal('30.00'), nombre_echantillons=1,
        )
        EchantillonVitesse.objects.create(
            identifiant_arete=self.ARETE, debut_intervalle=timezone.now() - timezone.timedelta(minutes=5),
            jour_semaine=2, heure_jour=8, vitesse_moyenne=Decimal('10.00'), nombre_echantillons=1,
        )
        self.assertEqual(service_trafic.vitesse_recente(self.ARETE), Decimal('10.00'))

    def test_vitesse_recente_sans_historique_est_none(self):
        self.assertIsNone(service_trafic.vitesse_recente(424242))

    def test_niveau_relatif_sans_donnees_est_normal(self):
        self.assertEqual(service_trafic.niveau_relatif(None, None), NiveauTrafic.NORMAL)
        self.assertEqual(service_trafic.niveau_relatif(20, None), NiveauTrafic.NORMAL)

    def test_niveau_relatif_seuils(self):
        self.assertEqual(service_trafic.niveau_relatif(45, 50), NiveauTrafic.NORMAL)  # 90% du typique
        self.assertEqual(service_trafic.niveau_relatif(30, 50), NiveauTrafic.MODERE)  # 60%
        self.assertEqual(service_trafic.niveau_relatif(15, 50), NiveauTrafic.DENSE)  # 30%

    def test_evaluer_route_sans_edge_replie_sur_normal(self):
        with patch('trips.services.service_trafic.tracer', return_value=([], [])):
            resultat = service_trafic._evaluer_sans_cache(GEOMETRIE_TEST)
        self.assertEqual(resultat, {'niveau_trafic': NiveauTrafic.NORMAL, 'duree_avec_trafic': None})

    def test_evaluer_route_panne_meili_replie_sur_normal(self):
        with patch('trips.services.service_trafic.tracer', side_effect=ErreurMeili('down')):
            resultat = service_trafic._evaluer_sans_cache(GEOMETRIE_TEST)
        self.assertEqual(resultat, {'niveau_trafic': NiveauTrafic.NORMAL, 'duree_avec_trafic': None})

    def test_evaluer_route_disjoncteur_ouvert_replie_sur_normal(self):
        with patch('trips.services.service_trafic.tracer', side_effect=DisjoncteurOuvert()):
            resultat = service_trafic._evaluer_sans_cache(GEOMETRIE_TEST)
        self.assertEqual(resultat, {'niveau_trafic': NiveauTrafic.NORMAL, 'duree_avec_trafic': None})

    def test_evaluer_route_sans_historique_utilise_la_vitesse_valhalla(self):
        edges = [{'id': self.ARETE, 'length': 1.0, 'speed': 40}]
        matched_points = [{'edge_index': 0}, {'edge_index': 0}]
        with patch('trips.services.service_trafic.tracer', return_value=(edges, matched_points)):
            resultat = service_trafic._evaluer_sans_cache(GEOMETRIE_TEST)
        self.assertEqual(resultat['niveau_trafic'], NiveauTrafic.NORMAL)
        self.assertEqual(resultat['duree_avec_trafic'], 90)  # 1km a 40km/h = 90s

    def test_evaluer_route_arete_congestionnee_remonte_dense(self):
        maintenant_local = timezone.now().astimezone(FUSEAU_TRAFIC)
        EchantillonVitesse.objects.create(
            identifiant_arete=self.ARETE, debut_intervalle=timezone.now() - timezone.timedelta(minutes=5),
            jour_semaine=maintenant_local.weekday(), heure_jour=maintenant_local.hour,
            vitesse_moyenne=Decimal('10.00'), nombre_echantillons=5,
        )
        EchantillonVitesse.objects.create(
            identifiant_arete=self.ARETE, debut_intervalle=timezone.now() - timezone.timedelta(days=7),
            jour_semaine=maintenant_local.weekday(), heure_jour=maintenant_local.hour,
            vitesse_moyenne=Decimal('50.00'), nombre_echantillons=5,
        )
        edges = [{'id': self.ARETE, 'length': 1.0, 'speed': 40}]
        matched_points = [{'edge_index': 0}, {'edge_index': 0}]

        with patch('trips.services.service_trafic.tracer', return_value=(edges, matched_points)):
            resultat = service_trafic._evaluer_sans_cache(GEOMETRIE_TEST)

        self.assertEqual(resultat['niveau_trafic'], NiveauTrafic.DENSE)
        # Vitesse recente reelle (10 km/h), pas celle de Valhalla -- 1km/10km/h = 360s.
        self.assertEqual(resultat['duree_avec_trafic'], 360)

    def test_evaluer_route_est_mise_en_cache(self):
        edges = [{'id': self.ARETE, 'length': 1.0, 'speed': 40}]
        matched_points = [{'edge_index': 0}, {'edge_index': 0}]
        with patch('trips.services.service_trafic.tracer', return_value=(edges, matched_points)) as trace:
            service_trafic.evaluer_route(GEOMETRIE_TEST)
            service_trafic.evaluer_route(GEOMETRIE_TEST)
        trace.assert_called_once()


class FlusherEchantillonsVitesseTests(TestCase):
    def _connexion_avec_bucket(self, identifiant_arete, bucket_epoch, somme, nombre):
        connexion = MagicMock()
        cle = f'trafic:accumulateur:{identifiant_arete}:{bucket_epoch}'
        connexion.smembers.return_value = {cle}
        connexion.hgetall.return_value = {'somme_vitesse': str(somme), 'nombre': str(nombre)}
        return connexion, cle

    def test_ignore_un_bucket_encore_ouvert(self):
        bucket_epoch = int(timezone.now().timestamp())  # vient tout juste de s'ouvrir
        connexion, cle = self._connexion_avec_bucket(111, bucket_epoch, 100, 4)

        with patch('trips.tasks.connexion_redis_telemetrie', return_value=connexion):
            nb = flusher_echantillons_vitesse()

        self.assertEqual(nb, 0)
        connexion.hgetall.assert_not_called()
        connexion.delete.assert_not_called()
        self.assertEqual(EchantillonVitesse.objects.count(), 0)

    def test_flush_un_bucket_ferme_et_nettoie_redis(self):
        bucket_epoch = int(timezone.now().timestamp()) - TAILLE_BUCKET_S - int(MARGE_FLUSH_S) - 10
        connexion, cle = self._connexion_avec_bucket(222, bucket_epoch, 120, 4)  # moyenne 30 km/h

        with patch('trips.tasks.connexion_redis_telemetrie', return_value=connexion):
            nb = flusher_echantillons_vitesse()

        self.assertEqual(nb, 1)
        echantillon = EchantillonVitesse.objects.get(identifiant_arete=222)
        self.assertEqual(echantillon.vitesse_moyenne, Decimal('30.00'))
        self.assertEqual(echantillon.nombre_echantillons, 4)
        connexion.delete.assert_called_once_with(cle)
        connexion.srem.assert_called_once_with('trafic:buckets:actifs', cle)

    def test_jour_semaine_et_heure_jour_calcules_en_heure_locale(self):
        # 2026-01-05 23:30 UTC == 2026-01-06 00:30 en Afrique/Douala (UTC+1, mardi).
        debut_intervalle = datetime(2026, 1, 5, 23, 30, tzinfo=dt_timezone.utc)
        bucket_epoch = int(debut_intervalle.timestamp())
        bucket_epoch -= bucket_epoch % TAILLE_BUCKET_S
        connexion, cle = self._connexion_avec_bucket(333, bucket_epoch, 50, 2)

        with patch('trips.tasks.connexion_redis_telemetrie', return_value=connexion):
            flusher_echantillons_vitesse()

        echantillon = EchantillonVitesse.objects.get(identifiant_arete=333)
        attendu_local = datetime.fromtimestamp(bucket_epoch, tz=dt_timezone.utc).astimezone(FUSEAU_TRAFIC)
        self.assertEqual(echantillon.jour_semaine, attendu_local.weekday())
        self.assertEqual(echantillon.heure_jour, attendu_local.hour)

    def test_flush_est_idempotent_sur_nouvelle_tentative(self):
        bucket_epoch = int(timezone.now().timestamp()) - TAILLE_BUCKET_S - int(MARGE_FLUSH_S) - 10
        connexion, cle = self._connexion_avec_bucket(444, bucket_epoch, 100, 2)  # 50 km/h

        with patch('trips.tasks.connexion_redis_telemetrie', return_value=connexion):
            flusher_echantillons_vitesse()
            # Meme cle encore presente cote Redis (simule un nettoyage rate) :
            # reflusher ne doit jamais lever d'IntegrityError.
            connexion.smembers.return_value = {cle}
            flusher_echantillons_vitesse()

        self.assertEqual(EchantillonVitesse.objects.filter(identifiant_arete=444).count(), 1)

    def test_bucket_sans_echantillons_est_nettoye_sans_ecrire(self):
        bucket_epoch = int(timezone.now().timestamp()) - TAILLE_BUCKET_S - int(MARGE_FLUSH_S) - 10
        connexion, cle = self._connexion_avec_bucket(555, bucket_epoch, 0, 0)

        with patch('trips.tasks.connexion_redis_telemetrie', return_value=connexion):
            nb = flusher_echantillons_vitesse()

        self.assertEqual(nb, 0)
        self.assertFalse(EchantillonVitesse.objects.filter(identifiant_arete=555).exists())
        connexion.delete.assert_called_once_with(cle)

    def test_cle_malformee_est_retiree_sans_planter(self):
        connexion = MagicMock()
        connexion.smembers.return_value = {'trafic:accumulateur:pasunentier'}

        with patch('trips.tasks.connexion_redis_telemetrie', return_value=connexion):
            nb = flusher_echantillons_vitesse()

        self.assertEqual(nb, 0)
        connexion.srem.assert_called_once_with('trafic:buckets:actifs', 'trafic:accumulateur:pasunentier')
