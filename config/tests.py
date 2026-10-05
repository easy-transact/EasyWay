import hashlib
import time
import uuid

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .signature_hmac import CORPS_NON_SIGNE, chaine_canonique, signer

CLE = 'secret-de-test'
URL = '/api/incidents/types/'  # AllowAny, sans DB : isole le middleware


def entetes_signes(methode, chemin, corps=b'', cle=CLE, id_cle='v1', timestamp=None, nonce=None, multipart=False):
    timestamp = str(timestamp if timestamp is not None else int(time.time()))
    nonce = nonce or uuid.uuid4().hex
    empreinte = CORPS_NON_SIGNE if multipart else hashlib.sha256(corps).hexdigest()
    return {
        'HTTP_X_EW_KEY_ID': id_cle,
        'HTTP_X_EW_TIMESTAMP': timestamp,
        'HTTP_X_EW_NONCE': nonce,
        'HTTP_X_EW_SIGNATURE': signer(cle, chaine_canonique(methode, chemin, timestamp, nonce, empreinte)),
    }


@override_settings(
    HMAC_MODE='enforce',
    HMAC_CLES={'v1': CLE, 'v2': 'autre-cle'},
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
)
class SignatureHmacTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_requete_signee_acceptee(self):
        self.assertEqual(self.client.get(URL, **entetes_signes('GET', URL)).status_code, 200)

    def test_query_string_fait_partie_de_la_signature(self):
        entetes = entetes_signes('GET', URL + '?a=1')
        self.assertEqual(self.client.get(URL + '?a=1', **entetes).status_code, 200)
        self.assertEqual(self.client.get(URL + '?a=2', **entetes_signes('GET', URL + '?a=1')).status_code, 401)

    def test_sans_signature_refusee(self):
        reponse = self.client.get(URL)
        self.assertEqual(reponse.status_code, 401)
        self.assertEqual(reponse.json()['code'], 'invalid_signature')
        self.assertEqual(reponse['Cache-Control'], 'no-store')

    def test_mauvaise_cle_refusee(self):
        self.assertEqual(self.client.get(URL, **entetes_signes('GET', URL, cle='pirate')).status_code, 401)
        self.assertEqual(self.client.get(URL, **entetes_signes('GET', URL, id_cle='v9')).status_code, 401)

    def test_rotation_deuxieme_cle_acceptee(self):
        entetes = entetes_signes('GET', URL, cle='autre-cle', id_cle='v2')
        self.assertEqual(self.client.get(URL, **entetes).status_code, 200)

    def test_rejeu_refuse(self):
        entetes = entetes_signes('GET', URL)
        self.assertEqual(self.client.get(URL, **entetes).status_code, 200)
        reponse = self.client.get(URL, **entetes)
        self.assertEqual(reponse.status_code, 401)
        self.assertEqual(reponse.json()['detail'], 'Nonce already used.')

    def test_timestamp_trop_ancien_refuse(self):
        entetes = entetes_signes('GET', URL, timestamp=int(time.time()) - 3600)
        self.assertEqual(self.client.get(URL, **entetes).status_code, 401)

    def test_corps_altere_refuse(self):
        entetes = entetes_signes('POST', URL, corps=b'{"a": 1}')
        reponse = self.client.post(URL, b'{"a": 2}', content_type='application/json', **entetes)
        self.assertEqual(reponse.status_code, 401)
        # Corps intact : passe le middleware (405 = la vue n'accepte pas POST).
        entetes = entetes_signes('POST', URL, corps=b'{"a": 1}')
        reponse = self.client.post(URL, b'{"a": 1}', content_type='application/json', **entetes)
        self.assertEqual(reponse.status_code, 405)

    def test_multipart_signe_sans_le_corps(self):
        entetes = entetes_signes('POST', URL, multipart=True)
        self.assertEqual(self.client.post(URL, {'champ': 'x'}, **entetes).status_code, 405)

    def test_chemins_exemptes_et_preflight(self):
        self.assertNotEqual(self.client.get('/api/schema/').status_code, 401)
        self.assertNotEqual(self.client.options(URL).status_code, 401)

    @override_settings(HMAC_MODE='log')
    def test_mode_log_ne_bloque_pas_et_indique_le_statut(self):
        with self.assertLogs('easyway.signature_hmac', level='WARNING'):
            reponse = self.client.get(URL)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse['X-EW-Signature-Status'], 'invalid: Missing signature headers.')

    @override_settings(HMAC_MODE='log')
    def test_mode_log_journalise_les_signatures_valides(self):
        with self.assertLogs('easyway.signature_hmac', level='INFO') as journaux:
            reponse = self.client.get(URL, **entetes_signes('GET', URL))
        self.assertEqual(reponse['X-EW-Signature-Status'], 'valid')
        self.assertIn('Signature HMAC valide (cle v1)', journaux.output[0])

    def test_pas_d_entete_de_statut_en_enforce(self):
        reponse = self.client.get(URL, **entetes_signes('GET', URL))
        self.assertNotIn('X-EW-Signature-Status', reponse)

    @override_settings(HMAC_MODE='off')
    def test_mode_off_ne_verifie_rien(self):
        self.assertEqual(self.client.get(URL).status_code, 200)


URL_WS = '/ws/incidents/'


def entetes_ws(entetes_django):
    """En-tetes WSGI (HTTP_X_EW_KEY_ID) -> en-tetes ASGI ([(b'x-ew-key-id', ...)])."""
    return [
        (nom[len('HTTP_'):].replace('_', '-').lower().encode(), valeur.encode())
        for nom, valeur in entetes_django.items()
    ]


@override_settings(
    HMAC_MODE='enforce',
    HMAC_CLES={'v1': CLE},
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    CHANNEL_LAYERS={'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}},
)
class SignatureHmacWebSocketTests(SimpleTestCase):
    """Demande d'ouverture de /ws/incidents/ signee comme GET a corps vide."""

    def setUp(self):
        cache.clear()

    async def _connecter(self, entetes=(), chemin=URL_WS):
        from channels.testing import WebsocketCommunicator

        from config.asgi import application

        communicateur = WebsocketCommunicator(application, chemin, headers=list(entetes))
        connecte, _ = await communicateur.connect()
        if connecte:
            await communicateur.disconnect()
        return connecte

    async def test_ouverture_signee_acceptee(self):
        self.assertTrue(await self._connecter(entetes_ws(entetes_signes('GET', URL_WS))))

    async def test_ouverture_sans_signature_refusee(self):
        self.assertFalse(await self._connecter())

    async def test_ouverture_signee_sur_un_autre_chemin_refusee(self):
        self.assertFalse(await self._connecter(entetes_ws(entetes_signes('GET', '/api/incidents/nearby/'))))

    async def test_rejeu_de_l_ouverture_refuse(self):
        entetes = entetes_ws(entetes_signes('GET', URL_WS))
        self.assertTrue(await self._connecter(entetes))
        self.assertFalse(await self._connecter(entetes))

    @override_settings(HMAC_MODE='log')
    async def test_mode_log_ne_bloque_pas(self):
        with self.assertLogs('easyway.signature_hmac', level='WARNING'):
            self.assertTrue(await self._connecter())
