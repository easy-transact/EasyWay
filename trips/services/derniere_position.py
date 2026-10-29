"""
Derniere position connue d'un conducteur en trajet (cache seulement, 5 min,
jamais en base) : sert a refuser un signalement fait loin de lui
(community.views.IncidentCreationView).
"""

import time

from django.core.cache import cache

CLE = 'position:derniere:'
DUREE_S = 5 * 60


def memoriser(utilisateur_id, lat, lon):
    cache.set(f'{CLE}{utilisateur_id}', (lat, lon, time.time()), timeout=DUREE_S)


def lire(utilisateur_id, age_max_s):
    """(lat, lon) si une position de moins de age_max_s est connue, sinon None."""
    valeur = cache.get(f'{CLE}{utilisateur_id}')
    if not valeur or time.time() - valeur[2] > age_max_s:
        return None
    return valeur[0], valeur[1]
