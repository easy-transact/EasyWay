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


def chemins_tuiles(chemin):
    """{tile_id: chemin relatif de la tuile sans extension} (ex. '1/034/029') :
    traffic.tar reprend l'arborescence des tuiles routieres (cf. trafic_predit)."""
    chemins = {}
    try:
        with tarfile.open(chemin) as archive, open(chemin, 'rb') as fichier:
            for membre in archive.getmembers():
                if not membre.name.endswith('.gph'):
                    continue
                fichier.seek(membre.offset_data)
                tile_id = struct.unpack(FORMAT_ENTETE, fichier.read(TAILLE_ENTETE))[0]
                chemins[tile_id] = membre.name[:-len('.gph')]
    except (OSError, tarfile.TarError, struct.error) as exc:
        raise TraficIndisponible(str(exc)) from exc
    return chemins


def generation(chemin):
    """Identifiant du fichier traffic.tar (peripherique, inode) : change quand
    les tuiles sont reconstruites (nouveau fichier), pas quand on y ecrit."""
    try:
        etat = os.stat(chemin)
    except OSError as exc:
        raise TraficIndisponible(str(exc)) from exc
    return f'{etat.st_dev}:{etat.st_ino}'


def _index(chemin):
    """Index mis en cache par process, invalide si le fichier est REMPLACE
    (reconstruction des tuiles : nouveau fichier, donc nouvel inode et/ou
    nouvelle taille). Pas sur la date de modification : nos propres
    ecritures par mmap la changent a chaque publication (toutes les 90 s), ce
    qui faisait relire l'en-tete de toutes les tuiles a chaque fois."""
    try:
        etat = os.stat(chemin)
        signature = (etat.st_dev, etat.st_ino, etat.st_size)
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


# Arete FERMEE pour Valhalla (TrafficSpeed.closed() dans baldr/traffictile.h,
# 3.5.1 : breakpoint1 != 0 et vitesse globale 0 ; congestion1 au maximum 63
# en plus, pour closed(0)). Respectee par les calculs dont speed_types
# contient 'current' (sauf ignore_closures) ; les points de depart/arrivee ne
# se calent pas sur une arete fermee (search_filter.exclude_closures, vrai par
# defaut).
VITESSE_FERMEE = 255 << 28 | 255 << 36 | 63 << 44


def ecrire_vitesses(chemin, vitesses_kmh: dict, fermees=()) -> int:
    """vitesses_kmh : {graph_id: km/h} ; fermees : graph_ids a marquer fermes
    (l'emportent sur une vitesse du meme graph_id)."""
    valeurs = {graph_id: encoder_vitesse(v) for graph_id, v in vitesses_kmh.items()}
    valeurs.update({graph_id: VITESSE_FERMEE for graph_id in fermees})
    return ecrire(chemin, valeurs)


def effacer(chemin, graph_ids) -> int:
    return ecrire(chemin, {graph_id: VITESSE_EFFACEE for graph_id in graph_ids})
