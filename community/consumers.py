"""
WebSocket /ws/incidents/ : signalements en temps reel (cf.
REALTIME_INCIDENTS_FRONTEND.txt pour le protocole complet).

Telephone -> serveur : auth (premier message, obligatoire), subscribe, ping.
Serveur -> telephone : ready, incident.created/updated/removed, pong, error.

Codes de fermeture :
    4001  jeton invalide ou expire (l'application le renouvelle et se reconnecte)
    4003  compte banni ou desactive
    4008  trop de cellules dans un abonnement (> MAX_CELLULES)
    4401  pas de message auth valide dans les TEMPS_REEL_DELAI_AUTH_S secondes,
          ou message autre que auth/ping avant l'authentification

Le jeton passe dans le premier message, jamais dans l'URL (qui finit dans les
journaux des serveurs et des intermediaires). La signature HMAC de la demande
d'ouverture est verifiee en amont, cf. config.signature_hmac.SignatureHmacWsMiddleware.
"""

import asyncio
import json
import time

import h3
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings as jwt_settings
from rest_framework_simplejwt.tokens import AccessToken

from .cache_incidents import MAX_CELLULES
from .models import RESOLUTION_H3_FIN
from .temps_reel import nom_groupe

FERMETURE_JETON_INVALIDE = 4001
FERMETURE_COMPTE_BANNI = 4003
FERMETURE_TROP_DE_CELLULES = 4008
FERMETURE_NON_AUTHENTIFIE = 4401


@database_sync_to_async
def _charger_utilisateur(user_id):
    return get_user_model().objects.filter(**{jwt_settings.USER_ID_FIELD: user_id}).first()


class IncidentsConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.utilisateur = None
        self.cellules = set()
        self._minuteur = None
        await self.accept()
        self._fermer_dans(settings.TEMPS_REEL_DELAI_AUTH_S, FERMETURE_NON_AUTHENTIFIE)

    async def disconnect(self, code):
        if self._minuteur is not None:
            self._minuteur.cancel()
        for cellule in self.cellules:
            await self.channel_layer.group_discard(nom_groupe(cellule), self.channel_name)
        self.cellules = set()

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        # Surcharge de AsyncJsonWebsocketConsumer.receive : un message illisible
        # renvoie une erreur au lieu de faire tomber la connexion.
        try:
            contenu = json.loads(text_data) if text_data is not None else None
        except ValueError:
            contenu = None
        if not isinstance(contenu, dict):
            return await self._erreur('invalid_message', 'Expected a JSON object in a text frame.')
        await self.receive_json(contenu)

    async def receive_json(self, contenu, **kwargs):
        type_message = contenu.get('type')
        if type_message == 'ping':
            return await self.send_json({'type': 'pong'})
        if type_message == 'auth':
            return await self._authentifier(contenu.get('token'))
        if self.utilisateur is None:
            return await self.close(code=FERMETURE_NON_AUTHENTIFIE)
        if type_message == 'subscribe':
            return await self._abonner(contenu.get('cells'))
        await self._erreur('unknown_type', f'Unknown message type: {type_message!r}.')

    async def _authentifier(self, jeton):
        # Un nouvel auth sur une connexion deja authentifiee est accepte :
        # l'application peut y pousser un jeton renouvele sans se reconnecter.
        try:
            jeton_acces = AccessToken(jeton if isinstance(jeton, str) else '')
            user_id = jeton_acces[jwt_settings.USER_ID_CLAIM]
        except (TokenError, KeyError):
            return await self.close(code=FERMETURE_JETON_INVALIDE)

        utilisateur = await _charger_utilisateur(user_id)
        if utilisateur is None:
            return await self.close(code=FERMETURE_JETON_INVALIDE)
        if not utilisateur.peut_signaler():  # is_active et non banni
            return await self.close(code=FERMETURE_COMPTE_BANNI)

        self.utilisateur = utilisateur
        # Jeton expire en cours de connexion : fermeture 4001 a l'echeance.
        self._fermer_dans(jeton_acces['exp'] - time.time(), FERMETURE_JETON_INVALIDE)
        await self.send_json({'type': 'ready', 'server_time': timezone.now().isoformat()})

    async def _abonner(self, cellules):
        """Remplace l'abonnement en cours. Seule la difference est appliquee :
        un deplacement d'une ou deux cellules ne refait pas 50 group_add."""
        if not isinstance(cellules, list) or not all(isinstance(c, str) for c in cellules):
            return await self._erreur('invalid_cells', "'cells' must be a list of H3 cell strings.")
        if len(cellules) > MAX_CELLULES:
            await self._erreur(
                'too_many_cells', f'Too many cells requested ({len(cellules)}), maximum {MAX_CELLULES}.',
            )
            return await self.close(code=FERMETURE_TROP_DE_CELLULES)

        cellules = {c.lower() for c in cellules}
        invalides = [c for c in cellules if not h3.is_valid_cell(c) or h3.get_resolution(c) != RESOLUTION_H3_FIN]
        if invalides:
            return await self._erreur(
                'invalid_cells', f'Invalid or non resolution-{RESOLUTION_H3_FIN} H3 cells: {", ".join(sorted(invalides)[:5])}.',
            )

        for cellule in self.cellules - cellules:
            await self.channel_layer.group_discard(nom_groupe(cellule), self.channel_name)
        for cellule in cellules - self.cellules:
            await self.channel_layer.group_add(nom_groupe(cellule), self.channel_name)
        self.cellules = cellules

    async def incident_evenement(self, evenement):
        """group_send de community.temps_reel : payload deja au format client."""
        await self.send_json(evenement['payload'])

    async def _erreur(self, code, detail):
        await self.send_json({'type': 'error', 'code': code, 'detail': detail})

    def _fermer_dans(self, delai_s, code):
        if self._minuteur is not None:
            self._minuteur.cancel()

        async def fermer():
            await asyncio.sleep(max(0, delai_s))
            await self.close(code=code)

        self._minuteur = asyncio.create_task(fermer())
