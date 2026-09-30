"""
Ecriture des vitesses de trafic temps reel dans le traffic.tar de Valhalla
(mjolnir.traffic_extract). Valhalla ouvre ce fichier en mmap et relit chaque
vitesse a chaque calcul : une ecriture ici change le routage suivant, sans
redemarrage.

Format (Valhalla 3.5.1, verifie dans baldr/traffictile.h et
scripts/valhalla_build_extract) : un membre tar par tuile routiere, chacun
fait d'un TrafficTileHeader de 32 octets ('<2Q4I' : tile_id, last_update,
directed_edge_count, traffic_tile_version, 2 spare) suivi d'un TrafficSpeed
de 8 octets par arete dirigee, dans l'ordre des aretes de la tuile.

Identifiant d'arete = GraphId Valhalla, tel que renvoye par trace_attributes
(edge.id, cf. client_meili.py) et stocke dans EchantillonVitesse :
bits 0-2 niveau, 3-24 tuile, 25-45 index de l'arete dans la tuile. La tuile
se retrouve par `graph_id & 0x1ffffff` (= tile_id de l'en-tete).
"""

import logging
import mmap
import os
import struct
import tarfile
import time

logger = logging.getLogger(__name__)

FORMAT_ENTETE = '<2Q4I'
TAILLE_ENTETE = struct.calcsize(FORMAT_ENTETE)
TAILLE_VITESSE = 8
VERSION_TUILE_TRAFIC = 3

MASQUE_TUILE = 0x1ffffff
DECALAGE_INDEX_ARETE = 25

# TrafficSpeed : bits 0-6 vitesse globale, 7-13/14-20/21-27 vitesses des 3
# sous-segments, 28-35/36-43 breakpoints, 44-61 congestions, 62 incidents.
VITESSE_BRUTE_INCONNUE = 127
VITESSE_BRUTE_MAX = VITESSE_BRUTE_INCONNUE - 1
# Une vitesse brute 0 signifie route FERMEE pour Valhalla (TrafficSpeed.closed) :
# une mesure lente ne doit jamais y tomber par arrondi.
VITESSE_BRUTE_MIN = 1


class TraficIndisponible(Exception):
    """traffic.tar absent, illisible ou d'une version inattendue."""


def encoder_vitesse(vitesse_kmh: float) -> int:
    """TrafficSpeed uniforme sur toute l'arete : breakpoint1=255 fait
    couvrir l'arete entiere par le premier sous-segment ; congestion laissee
    a 0 (inconnue), Valhalla ne s'en sert alors pas."""
    brute = max(VITESSE_BRUTE_MIN, min(VITESSE_BRUTE_MAX, round(vitesse_kmh / 2)))
    return (
        brute
        | brute << 7
        | brute << 14
        | brute << 21
        | 255 << 28
        | 255 << 36
    )


# Tous les bits a 0 : breakpoint1=0 -> speed_valid() faux, Valhalla ignore
# l'arete (etat initial d'un traffic.tar genere par valhalla_build_extract).
VITESSE_EFFACEE = 0


def _indexer(chemin):
    """{tile_id: (offset des vitesses, nombre d'aretes)}, lu depuis l'en-tete
    de chaque tuile plutot que deduit du nom de fichier."""
    index = {}
    with tarfile.open(chemin) as archive, open(chemin, 'rb') as fichier:
        for membre in archive.getmembers():
            if not membre.name.endswith('.gph'):
                continue
            fichier.seek(membre.offset_data)
            tile_id, _maj, nb_aretes, version, _s2, _s3 = struct.unpack(
                FORMAT_ENTETE, fichier.read(TAILLE_ENTETE)
            )
            if version != VERSION_TUILE_TRAFIC:
                raise TraficIndisponible(f'Version de tuile trafic inattendue: {version}')
            index[tile_id] = (membre.offset_data, nb_aretes)
    return index


_cache_index = {}


def _index(chemin):
    """Index mis en cache par process, invalide si le fichier change (le
    rebuild nocturne recree traffic.tar : nouvelle date de modification)."""
    try:
        signature = (os.stat(chemin).st_mtime_ns, os.stat(chemin).st_size)
    except OSError as exc:
        raise TraficIndisponible(str(exc)) from exc
    en_cache = _cache_index.get(chemin)
    if en_cache and en_cache[0] == signature:
        return en_cache[1]
    try:
        index = _indexer(chemin)
    except (OSError, tarfile.TarError, struct.error) as exc:
        raise TraficIndisponible(str(exc)) from exc
    _cache_index[chemin] = (signature, index)
    return index


def ecrire(chemin, valeurs: dict) -> int:
    """valeurs : {graph_id: TrafficSpeed encode (int)}. Retourne le nombre
    d'aretes ecrites ; un graph_id absent du fichier (carte regeneree depuis
    la mesure) est ignore, jamais une erreur. Met a jour last_update des
    tuiles touchees."""
    if not valeurs:
        return 0
    index = _index(chemin)
    maintenant = int(time.time())
    nb = 0
    tuiles_touchees = set()
    try:
        with open(chemin, 'r+b') as fichier, mmap.mmap(fichier.fileno(), 0) as memoire:
            for graph_id, valeur in valeurs.items():
                tuile = index.get(graph_id & MASQUE_TUILE)
                index_arete = graph_id >> DECALAGE_INDEX_ARETE
                if tuile is None or index_arete >= tuile[1]:
                    continue
                position = tuile[0] + TAILLE_ENTETE + index_arete * TAILLE_VITESSE
                memoire[position:position + TAILLE_VITESSE] = struct.pack('<Q', valeur)
                tuiles_touchees.add(tuile[0])
                nb += 1
            for offset in tuiles_touchees:
                memoire[offset + 8:offset + 16] = struct.pack('<Q', maintenant)
    except OSError as exc:
        raise TraficIndisponible(str(exc)) from exc
    return nb


def ecrire_vitesses(chemin, vitesses_kmh: dict) -> int:
    return ecrire(chemin, {graph_id: encoder_vitesse(v) for graph_id, v in vitesses_kmh.items()})


def effacer(chemin, graph_ids) -> int:
    return ecrire(chemin, {graph_id: VITESSE_EFFACEE for graph_id in graph_ids})
