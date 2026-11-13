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


def est_requete_refusee(exc) -> bool:
    """True pour une reponse 4xx de Valhalla : requete refusee (aucun chemin,
    point trop loin d'une route, trace impossible a recaler...). C'est un cas
    NORMAL, pas une panne : il ne doit ni compter pour le disjoncteur, ni etre
    reessaye (meme requete = meme reponse). Seuls reseau, timeout et 5xx sont
    des pannes."""
    reponse = getattr(exc, 'response', None)
    return reponse is not None and 400 <= reponse.status_code < 500


def enregistrer_echec():
    # cache.add + cache.incr : increment atomique entre workers (un get puis un
    # set pouvait perdre des echecs comptes en meme temps).
    cache.add(CLE_ECHECS, 0, timeout=FENETRE_COMPTAGE_ECHECS_S)
    try:
        echecs = cache.incr(CLE_ECHECS)
    except ValueError:  # cle expiree entre add et incr
        cache.set(CLE_ECHECS, 1, timeout=FENETRE_COMPTAGE_ECHECS_S)
        echecs = 1
    if echecs is None:  # Redis injoignable (IGNORE_EXCEPTIONS) : on ne compte rien
        return
    if echecs >= SEUIL_ECHECS:
        cache.set(CLE_OUVERT_JUSQU_A, time.time() + DUREE_OUVERTURE_S, timeout=DUREE_OUVERTURE_S)


def reinitialiser_echecs():
    cache.delete(CLE_ECHECS)
    cache.delete(CLE_OUVERT_JUSQU_A)
