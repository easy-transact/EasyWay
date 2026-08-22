"""
Cache Redis par cellule H3 (resolution 7, ~1.2km) pour /api/incidents/proches/,
le chemin le plus sollicite du module. La cle est la forme hexadecimale de la
cellule -- la meme que celle envoyee par le client dans ?cellules= -- pour que
lecture (vue) et invalidation (creation/vote/expiration/retrait) s'accordent
sans reconversion ambigue.
"""

import h3
from django.core.cache import cache

DUREE_CACHE_CELLULE_S = 30


def cle_cache_cellule(cellule_h3_res7: int) -> str:
    return f'incidents:cellule:{h3.int_to_str(cellule_h3_res7)}'


def invalider_cache_cellule(cellule_h3_res7: int) -> None:
    cache.delete(cle_cache_cellule(cellule_h3_res7))
