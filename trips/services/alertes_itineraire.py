"""
Envoi en direct aux conducteurs concernes (WebSocket /ws/incidents/) : quand
un signalement qui agit sur les itineraires apparait ou change sur la route
qu'un conducteur suit ENCORE, le serveur lui envoie aussitot

    {"type": "route.recheck", "trip_id", "incident_id", "incident_type",
     "incident_subtype", "cause": "ROAD_CLOSED" | "NEW_REPORT"}

et l'appli appelle better-route tout de suite, au lieu d'attendre son
prochain appel periodique (90 s). On n'envoie PAS l'itineraire lui-meme : le
serveur ne connait ni le cap, ni la vitesse, ni les etapes restantes, ni les
points evites de l'appli, et better-route garde sa regle de stabilite (une
proposition doit sortir deux fois de suite). L'appli peut donc espacer ses
appels periodiques (filet de securite) sans perdre en reactivite.

Bornes : un seul envoi par incident toutes les DELAI_INCIDENT_S, un seul par
conducteur toutes les DELAI_CONDUCTEUR_S, au plus MAX_TRAJETS trajets par
incident.
"""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.core.cache import cache
from django.utils import timezone

from .sur_trajet import COULOIR_M, Trace  # couloir commun (cf. sur_trajet)

logger = logging.getLogger(__name__)

DELAI_INCIDENT_S = 120
DELAI_CONDUCTEUR_S = 60
MAX_TRAJETS = 300
AGE_MAX_POSITION_S = 300
CONFIANCE_MIN = 0.5  # en dessous, le signalement pese trop peu pour justifier un recalcul
TYPE_MESSAGE = 'route.recheck'


def groupe_utilisateur(utilisateur_id) -> str:
    """Groupe Channels personnel : rejoint par chaque connexion WebSocket
    authentifiee de l'utilisateur (cf. community.consumers)."""
    return f'utilisateur.{utilisateur_id}'


def alerter_conducteurs(incident_id) -> int:
    """Retourne le nombre de conducteurs prevenus."""
    from community.models import Incident, StatutIncident

    from ..models import StatutTrajet, Trajet
    from . import derniere_position, retards_signalements as rs

    incident = Incident.objects.select_related('auteur').filter(
        pk=incident_id, statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE],
        expire_le__gt=timezone.now(),
    ).first()
    if incident is None or incident.est_conteste:
        return 0
    effet = rs.effet_de(incident)
    if effet is None:
        return 0  # police, radar, nid-de-poule : aucun effet sur l'itineraire
    fermeture = rs.fermeture_confirmee(incident, effet, 'auto')
    if not fermeture and rs.confiance(incident) < CONFIANCE_MIN:
        return 0
    if not cache.add(f'alerte:incident:{incident.pk}', 1, timeout=DELAI_INCIDENT_S):
        return 0  # deja traite il y a peu (votes en rafale)

    point = Point(incident.position.x, incident.position.y, srid=4326)
    trajets = (
        Trajet.objects.filter(statut=StatutTrajet.ACTIF, geometrie__dwithin=(point, D(m=COULOIR_M)))
        .exclude(utilisateur__mode_invisible=True)
        .only('id', 'utilisateur', 'geometrie')[:MAX_TRAJETS]
    )
    couche = get_channel_layer()
    message = {
        'type': TYPE_MESSAGE, 'incident_id': str(incident.pk), 'incident_type': incident.type,
        'incident_subtype': incident.sous_type or '', 'cause': 'ROAD_CLOSED' if fermeture else 'NEW_REPORT',
    }
    nb = 0
    for trajet in trajets:
        position = derniere_position.lire(trajet.utilisateur_id, AGE_MAX_POSITION_S)
        if position is not None and not _devant(trajet, position, incident):
            continue  # deja passe : rien a recalculer
        if not cache.add(f'alerte:conducteur:{trajet.utilisateur_id}', 1, timeout=DELAI_CONDUCTEUR_S):
            continue
        try:
            async_to_sync(couche.group_send)(
                groupe_utilisateur(trajet.utilisateur_id),
                {'type': 'itineraire.evenement', 'payload': {**message, 'trip_id': str(trajet.pk)}},
            )
            nb += 1
        except Exception:
            logger.warning('Envoi route.recheck impossible (trajet %s)', trajet.pk, exc_info=True)
    return nb


def _devant(trajet, position, incident) -> bool:
    """L'incident est-il sur la partie qui reste a parcourir, dans son sens ?"""
    from .meilleur_itineraire import troncon_restant

    reste = troncon_restant(list(trajet.geometrie.coords), position)
    return len(reste) >= 2 and Trace(reste).contient_incident(incident)


def planifier_alerte(incident_id) -> None:
    """Apres commit : tache Celery si le broker repond ; sinon on laisse
    tomber (l'appel periodique de better-route reste le filet de securite)."""
    from django.db import transaction

    def lancer():
        from ..tasks import alerter_conducteurs_concernes

        try:
            alerter_conducteurs_concernes.delay(str(incident_id))
        except Exception:
            logger.warning('Alerte des conducteurs non planifiee (broker indisponible)', exc_info=True)

    transaction.on_commit(lancer)
