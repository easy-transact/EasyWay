"""
Cache Redis par cellule H3 (resolution 8, ~0.74km²) pour /api/incidents/nearby/,
le chemin le plus sollicite du module. La cle est la forme hexadecimale de la
cellule -- la meme que celle envoyee par le client dans ?cellules= -- pour que
lecture (vue) et invalidation (creation/vote/expiration/retrait) s'accordent
sans reconversion ambigue.
"""

import h3
from django.core.cache import cache

DUREE_CACHE_CELLULE_S = 30

# Fenetre pendant laquelle un remplissage de cache (cache.set en cas de miss,
# cf. IncidentsProchesView.get) doit etre ignore pour une cellule qui vient
# d'etre invalidee. Ferme la course classique du cache-aside : une lecture
# amorcee juste avant une ecriture peut terminer sa requete DB (snapshot
# pre-ecriture) et tenter de re-cacher cette valeur perimee juste apres le
# delete() ci-dessous -- sans ce verrou, la valeur perimee resterait servie
# jusqu'a l'expiration de son propre TTL plutot que jusqu'a ce delete().
DUREE_VERROU_ECRITURE_S = 2


def cle_cache_cellule(cellule_h3: int) -> str:
    return f'incidents:cellule:{h3.int_to_str(cellule_h3)}'


def _cle_verrou_ecriture(cellule_h3: int) -> str:
    return f'incidents:cellule:{h3.int_to_str(cellule_h3)}:ecrit_recemment'


def invalider_cache_cellule(cellule_h3: int) -> None:
    cache.delete(cle_cache_cellule(cellule_h3))
    cache.set(_cle_verrou_ecriture(cellule_h3), True, timeout=DUREE_VERROU_ECRITURE_S)


def ecriture_recente(cellule_h3: int) -> bool:
    """True si cette cellule a ete invalidee il y a moins de DUREE_VERROU_ECRITURE_S --
    cf. IncidentsProchesView.get, qui s'en sert pour ne pas re-cacher une
    lecture potentiellement perimee pendant cette fenetre."""
    return cache.get(_cle_verrou_ecriture(cellule_h3)) is not None
