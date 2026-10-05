"""Signalements en temps reel : WebSocket /ws/incidents/ (consumers.py) et
publication depuis les chemins d'ecriture (temps_reel.py)."""

import asyncio
from unittest.mock import patch

import h3
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from accounts.tests import connecter
from config.asgi import application

from . import temps_reel
from .cache_incidents import MAX_CELLULES
from .models import Incident, StatutIncident, TypeIncident
from .tests import (
    DOUALA_LAT,
    DOUALA_LON,
    creer_incident,
    creer_utilisateur,
    patcher_locate_incident,
    patcher_nominatim_incident,
)

COUCHE_MEMOIRE = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}

# Deux quartiers de Douala, de part et d'autre du Wouri : jamais la meme cellule.
AKWA = (4.0511, 9.7006)
BONABERI = (4.0710, 9.6630)
CELLULE_AKWA = h3.latlng_to_cell(*AKWA, 8)
CELLULE_BONABERI = h3.latlng_to_cell(*BONABERI, 8)


@override_settings(CHANNEL_LAYERS=COUCHE_MEMOIRE, HMAC_MODE='off')
class IncidentsConsumerTests(TransactionTestCase):
    # TransactionTestCase : le consumer lit la base depuis un autre thread
    # (database_sync_to_async), qui ne verrait pas la transaction d'un TestCase.
    # serialized_rollback : restaure les donnees semees par migration (Droits).
    serialized_rollback = True

    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()

    async def _connecter(self, authentifier=True):
        communicateur = WebsocketCommunicator(application, '/ws/incidents/')
        connecte, _ = await communicateur.connect()
        self.assertTrue(connecte)
        if authentifier:
            jeton = await sync_to_async(lambda: str(AccessToken.for_user(self.utilisateur)))()
            await communicateur.send_json_to({'type': 'auth', 'token': jeton})
            pret = await communicateur.receive_json_from()
            self.assertEqual(pret['type'], 'ready')
            self.assertIn('server_time', pret)
        return communicateur

    async def _attendre_fermeture(self, communicateur):
        sortie = await communicateur.receive_output(timeout=2)
        self.assertEqual(sortie['type'], 'websocket.close')
        return sortie['code']

    @override_settings(TEMPS_REEL_DELAI_AUTH_S=0.1)
    async def test_sans_auth_fermeture_4401_apres_le_delai(self):
        communicateur = await self._connecter(authentifier=False)
        self.assertEqual(await self._attendre_fermeture(communicateur), 4401)

    async def test_message_avant_auth_fermeture_4401(self):
        communicateur = await self._connecter(authentifier=False)
        await communicateur.send_json_to({'type': 'subscribe', 'cells': [CELLULE_AKWA]})
        self.assertEqual(await self._attendre_fermeture(communicateur), 4401)

    async def test_ping_accepte_avant_auth(self):
        communicateur = await self._connecter(authentifier=False)
        await communicateur.send_json_to({'type': 'ping'})
        self.assertEqual(await communicateur.receive_json_from(), {'type': 'pong'})
        await communicateur.disconnect()

    async def test_jeton_invalide_fermeture_4001(self):
        communicateur = await self._connecter(authentifier=False)
        await communicateur.send_json_to({'type': 'auth', 'token': 'pas-un-jeton'})
        self.assertEqual(await self._attendre_fermeture(communicateur), 4001)

    async def test_jeton_expire_en_cours_de_connexion_fermeture_4001(self):
        communicateur = await self._connecter(authentifier=False)

        def jeton_presque_expire():
            jeton = AccessToken.for_user(self.utilisateur)
            jeton.set_exp(lifetime=timezone.timedelta(seconds=0.3))
            return str(jeton)

        await communicateur.send_json_to({'type': 'auth', 'token': await sync_to_async(jeton_presque_expire)()})
        self.assertEqual((await communicateur.receive_json_from())['type'], 'ready')
        self.assertEqual(await self._attendre_fermeture(communicateur), 4001)

    async def test_compte_banni_fermeture_4003(self):
        await sync_to_async(self.utilisateur.bannir)()
        communicateur = await self._connecter(authentifier=False)
        jeton = await sync_to_async(lambda: str(AccessToken.for_user(self.utilisateur)))()
        await communicateur.send_json_to({'type': 'auth', 'token': jeton})
        self.assertEqual(await self._attendre_fermeture(communicateur), 4003)

    async def test_trop_de_cellules_erreur_puis_fermeture_4008(self):
        communicateur = await self._connecter()
        cellules = [h3.latlng_to_cell(0.0, float(i), 8) for i in range(MAX_CELLULES + 1)]
        await communicateur.send_json_to({'type': 'subscribe', 'cells': cellules})
        erreur = await communicateur.receive_json_from()
        self.assertEqual((erreur['type'], erreur['code']), ('error', 'too_many_cells'))
        self.assertEqual(await self._attendre_fermeture(communicateur), 4008)

    async def test_cellules_invalides_ou_mauvaise_resolution_refusees(self):
        communicateur = await self._connecter()
        for cellules in (['pas-une-cellule'], [h3.latlng_to_cell(*AKWA, 7)], 'pas-une-liste'):
            await communicateur.send_json_to({'type': 'subscribe', 'cells': cellules})
            erreur = await communicateur.receive_json_from()
            self.assertEqual((erreur['type'], erreur['code']), ('error', 'invalid_cells'))
        await communicateur.disconnect()

    async def test_message_illisible_ne_ferme_pas_la_connexion(self):
        communicateur = await self._connecter()
        await communicateur.send_to(text_data='{pas du json')
        self.assertEqual((await communicateur.receive_json_from())['code'], 'invalid_message')
        await communicateur.send_json_to({'type': 'ping'})
        self.assertEqual(await communicateur.receive_json_from(), {'type': 'pong'})
        await communicateur.disconnect()

    async def test_reception_dans_sa_cellule_seulement(self):
        communicateur = await self._connecter()
        await communicateur.send_json_to({'type': 'subscribe', 'cells': [CELLULE_AKWA]})
        await communicateur.send_json_to({'type': 'ping'})  # subscribe traite avant le pong
        await communicateur.receive_json_from()

        await sync_to_async(temps_reel.publier_retrait)('id-bonaberi', h3.str_to_int(CELLULE_BONABERI), 'withdrawn')
        await sync_to_async(temps_reel.publier_retrait)('id-akwa', h3.str_to_int(CELLULE_AKWA), 'withdrawn')
        message = await communicateur.receive_json_from()
        self.assertEqual(message, {'type': 'incident.removed', 'id': 'id-akwa', 'reason': 'withdrawn'})
        self.assertTrue(await communicateur.receive_nothing())
        await communicateur.disconnect()

    async def test_nouvel_abonnement_remplace_l_ancien(self):
        communicateur = await self._connecter()
        await communicateur.send_json_to({'type': 'subscribe', 'cells': [CELLULE_AKWA]})
        await communicateur.send_json_to({'type': 'subscribe', 'cells': [CELLULE_BONABERI]})
        await communicateur.send_json_to({'type': 'ping'})
        await communicateur.receive_json_from()

        await sync_to_async(temps_reel.publier_retrait)('id-akwa', h3.str_to_int(CELLULE_AKWA), 'withdrawn')
        self.assertTrue(await communicateur.receive_nothing())
        await sync_to_async(temps_reel.publier_retrait)('id-bonaberi', h3.str_to_int(CELLULE_BONABERI), 'withdrawn')
        self.assertEqual((await communicateur.receive_json_from())['id'], 'id-bonaberi')
        await communicateur.disconnect()

    async def test_signalement_par_l_api_recu_par_un_autre_conducteur(self):
        # Critere de reussite : telephone A signale, telephone B dans la meme
        # cellule le recoit (bout en bout : POST -> commit -> groupe -> socket).
        def signaler_par_l_api():
            patcher_nominatim_incident(self)
            patcher_locate_incident(self)
            auteur = creer_utilisateur(email='a@easyway.local')
            reponse = self.client.post(
                reverse('community:incidents'),
                {'type': TypeIncident.ACCIDENT, 'lat': AKWA[0], 'lon': AKWA[1]},
                content_type='application/json', HTTP_IDEMPOTENCY_KEY='cle-temps-reel',
                **connecter(self.client, auteur.telephone),
            )
            self.assertEqual(reponse.status_code, 201)
            return reponse.json()

        communicateur = await self._connecter()
        await communicateur.send_json_to({'type': 'subscribe', 'cells': [CELLULE_AKWA]})
        await communicateur.send_json_to({'type': 'ping'})
        await communicateur.receive_json_from()

        corps = await sync_to_async(signaler_par_l_api)()
        message = await asyncio.wait_for(communicateur.receive_json_from(), timeout=3)
        self.assertEqual(message['type'], 'incident.created')
        self.assertEqual(message['incident']['id'], corps['id'])
        await communicateur.disconnect()


class GroupeEnregistreur:
    """Couche de canaux factice : enregistre les group_send."""

    def __init__(self):
        self.envois = []

    async def group_send(self, groupe, message):
        self.envois.append((groupe, message['payload']))


class PublicationTempsReelTests(TestCase):
    def setUp(self):
        patcher_nominatim_incident(self)
        patcher_locate_incident(self)
        cache.clear()
        self.couche = GroupeEnregistreur()
        patcheur = patch('community.temps_reel.get_channel_layer', return_value=self.couche)
        patcheur.start()
        self.addCleanup(patcheur.stop)
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.telephone)

    def _signaler(self, cle, lat=DOUALA_LAT, lon=DOUALA_LON, jetons=None):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse('community:incidents'),
                {'type': TypeIncident.EMBOUTEILLAGE, 'lat': lat, 'lon': lon},
                content_type='application/json', HTTP_IDEMPOTENCY_KEY=cle, **(jetons or self.jetons),
            )

    def _voter(self, incident, sens, votant):
        jetons = connecter(self.client, votant.telephone)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse('community:incident-vote', args=[incident.id]), {'direction': sens},
                content_type='application/json', **jetons,
            )

    def test_creation_publiee_dans_la_cellule_de_l_incident(self):
        reponse = self._signaler('cle-1', *AKWA)
        self.assertEqual(reponse.status_code, 201)
        self.assertEqual(len(self.couche.envois), 1)
        groupe, payload = self.couche.envois[0]
        self.assertEqual(groupe, f'incidents.h3.{CELLULE_AKWA}')
        self.assertEqual(payload['type'], 'incident.created')
        # Meme contenu que la reponse de l'API (schema Incident).
        self.assertEqual(payload['incident'], {k: v for k, v in reponse.json().items() if k != 'duplicate_of_existing'})

    def test_rien_publie_si_la_transaction_n_est_pas_validee(self):
        # Pas de captureOnCommitCallbacks : TestCase ne commit jamais.
        self.client.post(
            reverse('community:incidents'), {'type': TypeIncident.EMBOUTEILLAGE, 'lat': DOUALA_LAT, 'lon': DOUALA_LON},
            content_type='application/json', HTTP_IDEMPOTENCY_KEY='cle-sans-commit', **self.jetons,
        )
        self.assertEqual(self.couche.envois, [])

    def test_doublon_publie_en_mise_a_jour_de_l_existant(self):
        premier = self._signaler('cle-1').json()
        autre = creer_utilisateur(email='autre@easyway.local')
        reponse = self._signaler('cle-2', jetons=connecter(self.client, autre.telephone))
        self.assertTrue(reponse.json()['duplicate_of_existing'])
        _, payload = self.couche.envois[-1]
        self.assertEqual(payload['type'], 'incident.updated')
        self.assertEqual(payload['incident']['id'], premier['id'])

    def test_vote_confirmation_publie_une_mise_a_jour(self):
        incident = creer_incident(self.utilisateur)
        self._voter(incident, 'confirm', creer_utilisateur(email='v@easyway.local'))
        _, payload = self.couche.envois[-1]
        self.assertEqual((payload['type'], payload['incident']['id']), ('incident.updated', str(incident.id)))

    def test_vote_plus_rien_qui_fait_tomber_le_signalement_publie_un_retrait(self):
        # infirmer() avance expire_le de 10 min : a 5 min de l'echeance, le
        # signalement tombe.
        incident = creer_incident(self.utilisateur, expire_le=timezone.now() + timezone.timedelta(minutes=5))
        self._voter(incident, 'dispute', creer_utilisateur(email='v@easyway.local'))
        self.assertEqual(
            self.couche.envois[-1][1], {'type': 'incident.removed', 'id': str(incident.id), 'reason': 'disputed'},
        )

    def test_vote_sur_un_incident_deja_retire_ne_publie_rien(self):
        incident = creer_incident(self.utilisateur, statut=StatutIncident.RETIRE)
        self._voter(incident, 'confirm', creer_utilisateur(email='v@easyway.local'))
        self.assertEqual(self.couche.envois, [])

    def test_retrait_par_l_auteur(self):
        incident = creer_incident(self.utilisateur)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.delete(reverse('community:incident-detail', args=[incident.id]), **self.jetons)
        self.assertEqual(
            self.couche.envois[-1][1], {'type': 'incident.removed', 'id': str(incident.id), 'reason': 'withdrawn'},
        )

    def test_retrait_et_suppression_par_le_staff(self):
        staff = creer_utilisateur(email='staff@easyway.local', is_staff=True)
        jetons_staff = connecter(self.client, staff.telephone)
        retire, supprime = creer_incident(self.utilisateur), creer_incident(self.utilisateur)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse('community:staff-incident-retirer', args=[retire.id]), {'reason': 'Faux'},
                content_type='application/json', **jetons_staff,
            )
            self.client.delete(reverse('community:staff-incident-supprimer', args=[supprime.id]), **jetons_staff)
        self.assertEqual([p for _, p in self.couche.envois], [
            {'type': 'incident.removed', 'id': str(retire.id), 'reason': 'moderated'},
            {'type': 'incident.removed', 'id': str(supprime.id), 'reason': 'moderated'},
        ])
        self.assertFalse(Incident.objects.filter(id=supprime.id).exists())

    def test_mode_invisible_masque_reporter_id(self):
        self.utilisateur.mode_invisible = True
        self.utilisateur.save(update_fields=['mode_invisible'])
        self._signaler('cle-invisible')
        incident = self.couche.envois[-1][1]['incident']
        self.assertIsNone(incident['reporter_id'])
        self.assertIsNone(incident['reporter_name'])

    def test_panne_du_relais_ne_casse_pas_le_signalement(self):
        async def en_panne(*args, **kwargs):
            raise ConnectionError('Redis indisponible')

        self.couche.group_send = en_panne
        with self.assertLogs('easyway.temps_reel', level='WARNING'):
            reponse = self._signaler('cle-panne')
        self.assertEqual(reponse.status_code, 201)
