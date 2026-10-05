"""
Signalements en temps reel : publication vers les connexions WebSocket
abonnees a la cellule H3 (resolution 8) de l'incident (cf. consumers.py).

Le temps reel n'est qu'un accelerateur -- l'API (nearby/along-route) reste la
source de verite, et l'application la recharge a chaque reconnexion. Donc :
  - aucun message n'est conserve ni rejoue ;
  - une publication qui echoue (Redis indisponible...) est journalisee, jamais
    remontee : un POST /api/incidents/ ne doit pas echouer pour ca ;
  - l'expiration n'est pas publiee : chaque incident porte son expires_at et
    l'application le retire elle-meme a l'heure dite.

Envoi apres COMMIT (transaction.on_commit) : un client qui recoit l'evenement
puis recharge par l'API doit y trouver le meme etat, pas l'etat d'avant.
"""

import logging

import h3
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

from .serializers import IncidentSerializer

journal = logging.getLogger('easyway.temps_reel')

CREE = 'incident.created'
MODIFIE = 'incident.updated'
RETIRE = 'incident.removed'

# Valeurs de `reason` dans incident.removed.
RAISON_CONTESTE = 'disputed'      # votes "Plus rien" : expire_le ramene dans le passe
RAISON_RETIRE_AUTEUR = 'withdrawn'
RAISON_MODERE = 'moderated'


def nom_groupe(cellule_hex: str) -> str:
    """Groupe Channels d'une cellule H3 -- meme forme hexadecimale que
    ?cells= de /nearby/ et que l'abonnement envoye par l'application."""
    return f'incidents.h3.{cellule_hex}'


def publier_incident(incident, evenement: str) -> None:
    """incident.created / incident.updated. Serialise tout de suite (etat de
    l'incident au moment de l'ecriture), envoie apres commit. Meme
    IncidentSerializer sans requete que le cache de /nearby/ : reporter_id
    deja masque si l'auteur est en mode invisible."""
    payload = {'type': evenement, 'incident': IncidentSerializer(incident).data}
    _publier_apres_commit(incident.cellule_h3_res8, payload)


def publier_retrait(incident_id, cellule_h3: int, raison: str) -> None:
    payload = {'type': RETIRE, 'id': str(incident_id), 'reason': raison}
    _publier_apres_commit(cellule_h3, payload)


def _publier_apres_commit(cellule_h3: int, payload: dict) -> None:
    groupe = nom_groupe(h3.int_to_str(cellule_h3))

    def envoyer():
        try:
            async_to_sync(get_channel_layer().group_send)(
                groupe, {'type': 'incident.evenement', 'payload': payload},
            )
        except Exception:
            journal.warning('Publication temps reel impossible (%s, %s)', payload['type'], groupe, exc_info=True)

    transaction.on_commit(envoyer)
