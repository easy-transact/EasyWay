"""Signature HMAC des requetes app mobile -> API (cf. reunion du 29/09).

Chaque requete /api/ porte quatre en-tetes :
    X-EW-Key-Id     identifiant de la cle (ex. "v1") -- permet une rotation
                    sans casser les versions de l'appli encore installees
    X-EW-Timestamp  secondes Unix au moment de l'envoi
    X-EW-Nonce      valeur aleatoire unique par requete (16 a 64 caracteres)
    X-EW-Signature  hex(HMAC-SHA256(cle, chaine_canonique))

    chaine_canonique = METHODE + "\\n" + CHEMIN_ET_QUERY + "\\n" + TIMESTAMP
                       + "\\n" + NONCE + "\\n" + hex(SHA256(corps))

CHEMIN_ET_QUERY : exactement tels qu'envoyes, sans schema ni hote (ex.
"/api/incidents/nearby/?lat=4.05&lon=9.7"). Corps multipart (televersement
d'avatar) : "UNSIGNED-PAYLOAD" a la place du hash -- un client mobile n'a
pas acces aux octets exacts d'un FormData (boundary generee par la couche
reseau).

Ce que ca protege : requete alteree en transit, rejeu d'une requete
capturee (fenetre de HMAC_TOLERANCE_S + nonce a usage unique), scripts qui
appellent l'API sans passer par l'appli. Ce que ca NE protege PAS : la cle
est embarquee dans l'appli, donc extractible par quelqu'un de determine --
c'est une barriere supplementaire, pas une authentification (le JWT reste
l'authentification, HTTPS reste le chiffrement).

HMAC_MODE : "off" (rien), "log" (verifie et journalise les echecs sans
bloquer -- a utiliser le temps que toutes les versions de l'appli signent),
"enforce" (401 si la signature est absente/invalide).

En mode "log", chaque requete verifiee est journalisee (succes en INFO,
echec en WARNING) et la reponse porte X-EW-Signature-Status ("valid" ou
"invalid: <raison>") : le serveur acceptant tout, c'est le seul moyen pour
l'equipe mobile de confirmer que ses signatures sont bonnes avant le
passage en "enforce". Jamais pose en "enforce" (un 401 dit deja tout).
"""

import hashlib
import hmac
import logging
import re
import time

from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

journal = logging.getLogger('easyway.signature_hmac')

MODES = ('off', 'log', 'enforce')
CORPS_NON_SIGNE = 'UNSIGNED-PAYLOAD'
FORMAT_NONCE = re.compile(r'^[A-Za-z0-9_-]{16,64}$')


class SignatureInvalide(Exception):
    pass


def chaine_canonique(methode: str, chemin: str, timestamp: str, nonce: str, empreinte_corps: str) -> str:
    return '\n'.join([methode.upper(), chemin, timestamp, nonce, empreinte_corps])


def empreinte_corps(corps: bytes, content_type: str = '') -> str:
    if content_type.startswith('multipart/'):
        return CORPS_NON_SIGNE
    return hashlib.sha256(corps or b'').hexdigest()


def signer(cle: str, chaine: str) -> str:
    return hmac.new(cle.encode(), chaine.encode(), hashlib.sha256).hexdigest()


def verifier(request):
    """Leve SignatureInvalide (message destine au journal et au client) si la
    requete n'est pas correctement signee."""
    verifier_entetes(
        request.method, request.get_full_path(), request.headers,
        request.body, request.content_type or '',
    )


def verifier_entetes(methode: str, chemin: str, entetes, corps: bytes, content_type: str = ''):
    """Coeur de verifier(), sans objet requete Django -- partage avec la
    poignee de main WebSocket (SignatureHmacWsMiddleware). `entetes` : mapping
    interroge en minuscules (request.headers l'accepte, insensible a la casse)."""
    id_cle = entetes.get('x-ew-key-id', '')
    timestamp = entetes.get('x-ew-timestamp', '')
    nonce = entetes.get('x-ew-nonce', '')
    signature = entetes.get('x-ew-signature', '')
    if not (id_cle and timestamp and nonce and signature):
        raise SignatureInvalide('Missing signature headers.')

    cle = settings.HMAC_CLES.get(id_cle)
    if not cle:
        raise SignatureInvalide('Unknown key id.')
    if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > settings.HMAC_TOLERANCE_S:
        raise SignatureInvalide('Timestamp outside the allowed window.')
    if not FORMAT_NONCE.match(nonce):
        raise SignatureInvalide('Invalid nonce format.')

    attendue = signer(cle, chaine_canonique(
        methode, chemin, timestamp, nonce, empreinte_corps(corps, content_type),
    ))
    if not hmac.compare_digest(attendue, signature.lower()):
        raise SignatureInvalide('Invalid signature.')

    # Nonce verifie en dernier : une requete a signature invalide ne doit pas
    # "bruler" un nonce. add() est atomique (SET NX Redis). Retour None = Redis
    # indisponible (IGNORE_EXCEPTIONS, cf. CACHES) : on accepte plutot que de
    # bloquer toute l'API -- la fenetre de timestamp limite deja le rejeu.
    if cache.add(f'hmac:nonce:{id_cle}:{nonce}', 1, timeout=settings.HMAC_TOLERANCE_S * 2) is False:
        raise SignatureInvalide('Nonce already used.')


class SignatureHmacMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not self._a_verifier(request):
            return self.get_response(request)

        try:
            verifier(request)
        except SignatureInvalide as exc:
            journal.warning(
                'Signature HMAC invalide (%s) : %s %s', exc, request.method, request.get_full_path()
            )
            if settings.HMAC_MODE == 'enforce':
                return JsonResponse({'detail': str(exc), 'code': 'invalid_signature'}, status=401)
            statut = f'invalid: {exc}'
        else:
            journal.info(
                'Signature HMAC valide (cle %s) : %s %s',
                request.headers.get('X-EW-Key-Id'), request.method, request.get_full_path(),
            )
            statut = 'valid'

        reponse = self.get_response(request)
        if settings.HMAC_MODE == 'log':
            reponse['X-EW-Signature-Status'] = statut
        return reponse

    def _a_verifier(self, request):
        if settings.HMAC_MODE not in ('log', 'enforce'):
            return False
        # Preflight CORS : jamais signe par un navigateur.
        if request.method == 'OPTIONS' or not request.path.startswith('/api/'):
            return False
        return not any(request.path.startswith(prefixe) for prefixe in settings.HMAC_CHEMINS_EXEMPTES)


class SignatureHmacWsMiddleware:
    """Meme verification que SignatureHmacMiddleware, sur la demande
    d'ouverture WebSocket (cf. config/asgi.py) : signee comme un
    GET <chemin> a corps vide. En "enforce", une poignee de main mal signee
    est refusee avant accept() (le client voit un 403) ; en "log", seulement
    journalisee -- aucun en-tete de reponse possible sur un WebSocket."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'websocket' or settings.HMAC_MODE not in ('log', 'enforce'):
            return await self.application(scope, receive, send)

        chemin = scope['path']
        if scope.get('query_string'):
            chemin += '?' + scope['query_string'].decode('latin-1')
        entetes = {nom.decode('latin-1').lower(): valeur.decode('latin-1') for nom, valeur in scope['headers']}

        try:
            await sync_to_async(verifier_entetes)('GET', chemin, entetes, b'')
        except SignatureInvalide as exc:
            journal.warning('Signature HMAC invalide (%s) : WS %s', exc, chemin)
            if settings.HMAC_MODE == 'enforce':
                # Message recu avant toute reponse = websocket.connect ; un
                # close a ce stade est traduit en refus HTTP 403 par le serveur.
                await receive()
                return await send({'type': 'websocket.close', 'code': 4401})
        else:
            journal.info('Signature HMAC valide (cle %s) : WS %s', entetes.get('x-ew-key-id'), chemin)

        return await self.application(scope, receive, send)
