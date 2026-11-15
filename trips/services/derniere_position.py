"""
Derniere position connue d'un conducteur en trajet (cache seulement, 5 min,
jamais en base) : sert a refuser un signalement fait loin de lui
(community.views.IncidentCreationView).

Plus les quelques dernieres positions (memoriser_trace) : le sens dans lequel
il roule VRAIMENT (cap_de_trajectoire), plus fiable que le cap instantane du
telephone, aleatoire a l'arret ou au pas -- c'est justement en bouchon qu'on
signale. Jamais en base non plus.
"""

import time

from django.core.cache import cache

CLE = 'position:derniere:'
DUREE_S = 5 * 60


def memoriser(utilisateur_id, lat, lon):
    cache.set(f'{CLE}{utilisateur_id}', (lat, lon, time.time()), timeout=DUREE_S)


CLE_TRACE = 'position:trace:'
NB_POSITIONS_TRACE = 20
AGE_MAX_TRAJECTOIRE_S = 180
# En dessous, le deplacement n'est que du bruit GPS : pas de sens deductible.
DEPLACEMENT_MIN_M = 30


def memoriser_trace(utilisateur_id, positions):
    """positions : [(lat, lon, horodatage epoch)] du dernier lot de telemetrie."""
    trace = list(cache.get(f'{CLE_TRACE}{utilisateur_id}') or []) + list(positions)
    trace.sort(key=lambda p: p[2])
    cache.set(f'{CLE_TRACE}{utilisateur_id}', trace[-NB_POSITIONS_TRACE:], timeout=DUREE_S)


def cap_de_trajectoire(utilisateur_id, age_max_s=AGE_MAX_TRAJECTOIRE_S):
    """Cap (0-360) du deplacement recent : de la plus ancienne position de
    moins de age_max_s a la plus recente, s'il depasse DEPLACEMENT_MIN_M.
    None sinon (a l'arret depuis longtemps, pas de trajet, trop peu de points)."""
    from .geo import distance_haversine_m
    from .sur_trajet import cap_segment

    limite = time.time() - age_max_s
    recentes = [p for p in (cache.get(f'{CLE_TRACE}{utilisateur_id}') or []) if p[2] >= limite]
    if len(recentes) < 2:
        return None
    lat1, lon1 = recentes[0][:2]
    lat2, lon2 = recentes[-1][:2]
    if distance_haversine_m((lat1, lon1), (lat2, lon2)) < DEPLACEMENT_MIN_M:
        return None
    return cap_segment(lon1, lat1, lon2, lat2)


def lire(utilisateur_id, age_max_s):
    """(lat, lon) si une position de moins de age_max_s est connue, sinon None."""
    valeur = cache.get(f'{CLE}{utilisateur_id}')
    if not valeur or time.time() - valeur[2] > age_max_s:
        return None
    return valeur[0], valeur[1]
