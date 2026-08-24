"""
Disjoncteur partage entre les clients Valhalla (routage, client_valhalla.py,
et map-matching, client_meili.py) : meme conteneur, donc le meme etat "Valhalla
est en panne" doit valoir pour les deux. Un disjoncteur independant par client
redecouvrirait la panne separement -- l'un pourrait continuer a tenter des
appels pendant que l'autre l'a deja detectee et bascule sur son repli. Vit
dans le cache Django (Redis) plutot qu'en memoire de process, pour que son
etat soit partage entre workers/process.
"""

import time

from django.core.cache import cache

CLE_ECHECS = 'valhalla:disjoncteur:echecs'
CLE_OUVERT_JUSQU_A = 'valhalla:disjoncteur:ouvert_jusqu_a'
SEUIL_ECHECS = 3
DUREE_OUVERTURE_S = 30
FENETRE_COMPTAGE_ECHECS_S = 60


class DisjoncteurOuvert(Exception):
    """Leve en interne uniquement -- chaque appelant bascule sur son propre
    repli (ou renonce, pour Meili) des qu'il attrape ceci."""


def verifier():
    ouvert_jusqu_a = cache.get(CLE_OUVERT_JUSQU_A)
    if ouvert_jusqu_a is not None and time.time() < ouvert_jusqu_a:
        raise DisjoncteurOuvert()


def enregistrer_echec():
    echecs = (cache.get(CLE_ECHECS) or 0) + 1
    cache.set(CLE_ECHECS, echecs, timeout=FENETRE_COMPTAGE_ECHECS_S)
    if echecs >= SEUIL_ECHECS:
        cache.set(CLE_OUVERT_JUSQU_A, time.time() + DUREE_OUVERTURE_S, timeout=DUREE_OUVERTURE_S)


def reinitialiser_echecs():
    cache.delete(CLE_ECHECS)
    cache.delete(CLE_OUVERT_JUSQU_A)
