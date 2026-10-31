"""
Positions en direct des vehicules en trajet, pour la carte du back-office
(admins seulement). Redis uniquement, jamais en base : la derniere position
de chaque trajet, oubliee apres DUREE_S sans nouvelle position.

Anonyme : ni utilisateur ni trajet expose -- un identifiant tire du trajet
et du jour (HMAC), stable le temps du trajet pour animer le point, impossible
a relier a un compte. Un conducteur en mode invisible n'envoie rien
(TelemetriePositionsView s'arrete avant).
"""

import hashlib
import hmac
import json
import logging
import time

from django.conf import settings
from django.utils import timezone

from .producteur_evenements import connexion_redis_telemetrie

logger = logging.getLogger(__name__)

CLE_POSITIONS = 'direct:positions'   # HASH trajet_id -> json
CLE_MAJ = 'direct:maj'               # ZSET trajet_id -> horodatage
DUREE_S = 120


def _identifiant(trajet_id):
    jour = timezone.now().date().isoformat()
    return hmac.new(settings.SECRET_KEY.encode(), f'{trajet_id}:{jour}'.encode(), hashlib.sha256).hexdigest()[:12]


def memoriser(trajet, position, type_vehicule):
    """position : dernier point du lot (lat, lon, vitesse_kmh?, cap?). Une
    erreur Redis ne doit jamais faire echouer l'envoi de telemetrie."""
    try:
        connexion = connexion_redis_telemetrie()
        maintenant = time.time()
        with connexion.pipeline() as pipe:
            pipe.hset(CLE_POSITIONS, str(trajet.id), json.dumps({
                'lat': position['lat'], 'lon': position['lon'], 'speed_kmh': position.get('vitesse_kmh'),
                'heading': position.get('cap'), 'vehicle_type': type_vehicule or 'VOITURE', 't': maintenant,
            }))
            pipe.zadd(CLE_MAJ, {str(trajet.id): maintenant})
            pipe.execute()
    except Exception:
        logger.exception('Position en direct non memorisee')


def lire(bbox=None):
    """Vehicules vus depuis moins de DUREE_S, dans bbox (south, west, north, east) si fournie."""
    connexion = connexion_redis_telemetrie()
    limite = time.time() - DUREE_S
    perimes = connexion.zrangebyscore(CLE_MAJ, '-inf', f'({limite}')
    if perimes:
        with connexion.pipeline() as pipe:
            pipe.zrem(CLE_MAJ, *perimes)
            pipe.hdel(CLE_POSITIONS, *perimes)
            pipe.execute()
    trajets = connexion.zrangebyscore(CLE_MAJ, limite, '+inf')
    if not trajets:
        return []
    vehicules = []
    for trajet_id, brut in zip(trajets, connexion.hmget(CLE_POSITIONS, trajets)):
        if not brut:
            continue
        v = json.loads(brut)
        if bbox and not (bbox[0] <= v['lat'] <= bbox[2] and bbox[1] <= v['lon'] <= bbox[3]):
            continue
        vehicules.append({
            'id': _identifiant(trajet_id), 'lat': v['lat'], 'lon': v['lon'], 'speed_kmh': v['speed_kmh'],
            'heading': v['heading'], 'vehicle_type': v['vehicle_type'], 'age_s': round(time.time() - v['t']),
        })
    return vehicules
