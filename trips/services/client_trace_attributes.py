"""
ClientTraceAttributes : appelle /trace_attributes (geometrie -> liste ordonnee
d'aretes du graphe routier), meme conteneur Valhalla, meme disjoncteur partage
que client_locate.py/client_valhalla.py -- cf. disjoncteur.py.

Usage : matcher les incidents a un trajet par topologie plutot que par
distance (cf. community/views.py::IncidentsSurTrajetView) -- un incident est
sur le trajet si son (way_id, forward), cale au signalement (cf.
community/services.py::_verifier_position_routiere), fait partie de
l'ensemble des (way_id, forward) traverses par le trajet. Aucun couloir de
tolerance ne separe une contre-allee parallele de la route qu'elle longe,
seule cette appartenance topologique le peut.
"""

import requests
from django.conf import settings

from . import disjoncteur

TIMEOUT_S = 5


ATTRIBUTS_AVEC_NOEUDS = ['edge.way_id', 'edge.forward', 'edge.begin_osm_node_id', 'edge.end_osm_node_id']


class ErreurTraceAttributes(Exception):
    """Panne reseau/HTTP de Valhalla -- l'appelant doit se degrader (repli
    sur le couloir de distance, cf. IncidentsSurTrajetView) plutot que de
    laisser cette exception remonter, meme principe que ClientLocate."""


def attributs_trace(points: list[tuple[float, float]]) -> list[dict] | None:
    """`points` : liste de (lon, lat), meme forme que decoder_polyline6().
    Retourne la liste ordonnee des aretes traversees par le trajet --
    [{'way_id', 'forward'}, ...], dans l'ordre du trajet (begin_shape_index
    croissant cote Valhalla). None si Valhalla ne matche aucune arete sur
    cette geometrie (tres rare). Leve ErreurTraceAttributes/DisjoncteurOuvert
    si Valhalla est indisponible -- a l'appelant de decider du repli."""
    disjoncteur.verifier()

    from django.core.cache import cache

    def appeler(filtres):
        corps = {
            'shape': [{'lat': lat, 'lon': lon} for lon, lat in points],
            'costing': 'auto',
            'shape_match': 'map_snap',
        }
        if filtres:
            corps['filters'] = {'attributes': filtres, 'action': 'include'}
        return requests.post(f'{settings.VALHALLA_URL}/trace_attributes', json=corps, timeout=TIMEOUT_S)

    try:
        reponse = None
        if not cache.get(CLE_NOEUDS_INDISPONIBLES):
            # Noeuds OSM (troncon) en plus de la voie et du sens : demandes
            # explicitement (filtre), sinon Valhalla ne les renvoie pas.
            reponse = appeler(ATTRIBUTS_AVEC_NOEUDS)
            if reponse.status_code == 400:
                reponse = None
        if reponse is None:
            reponse = appeler(None)  # sortie complete, comme avant (voie + sens)
        reponse.raise_for_status()
    except requests.RequestException as exc:
        if disjoncteur.est_requete_refusee(exc):
            return None  # geometrie refusee (4xx) : aucune arete, pas une panne
        disjoncteur.enregistrer_echec()
        raise ErreurTraceAttributes(str(exc)) from exc

    disjoncteur.reinitialiser_echecs()
    resultat = reponse.json()
    aretes = resultat.get('edges')
    if not aretes:
        return None

    return [
        {
            'way_id': arete['way_id'], 'forward': arete['forward'],
            # (debut, fin) du troncon, ou None (Valhalla < 3.6, tuiles sans noeuds).
            'noeuds': (
                (int(arete['node_id']), int(arete['end_node']['node_id']))
                if arete.get('node_id') is not None and (arete.get('end_node') or {}).get('node_id') is not None
                else None
            ),
        }
        for arete in aretes
    ]


# Troncon OSM d'un point (noeuds de debut et de fin de l'arete, Valhalla >= 3.6
# avec mjolnir.keep_osm_node_ids). Version de Valhalla sans ces noeuds : on le
# retient une heure, plutot que de refaire un appel inutile a chaque signalement.
CLE_NOEUDS_INDISPONIBLES = 'valhalla:noeuds_osm:indisponibles'
DEMI_LONGUEUR_M = 20


def troncon_au_point(lat: float, lon: float, cap: int) -> tuple[int, int] | None:
    """(noeud OSM de debut, noeud OSM de fin) de l'arete parcourue au point,
    dans le sens `cap` -- un mini-trace de 2 x DEMI_LONGUEUR_M centre sur le
    point, recale par Valhalla ; l'arete est celle du point central (pas une
    voisine, meme pres d'un carrefour). None si indisponible, jamais d'exception."""
    import math

    from django.core.cache import cache

    if cache.get(CLE_NOEUDS_INDISPONIBLES):
        return None
    try:
        disjoncteur.verifier()
    except Exception:
        return None
    angle = math.radians(cap)
    d_lat = DEMI_LONGUEUR_M * math.cos(angle) / 111_320
    d_lon = DEMI_LONGUEUR_M * math.sin(angle) / (111_320 * math.cos(math.radians(lat)))
    forme = [
        {'lat': lat - d_lat, 'lon': lon - d_lon},
        {'lat': lat, 'lon': lon},
        {'lat': lat + d_lat, 'lon': lon + d_lon},
    ]
    try:
        reponse = requests.post(
            f'{settings.VALHALLA_URL}/trace_attributes',
            json={
                'shape': forme, 'costing': 'auto', 'shape_match': 'map_snap',
                'filters': {
                    'attributes': ['edge.begin_osm_node_id', 'edge.end_osm_node_id', 'matched.edge_index'],
                    'action': 'include',
                },
            },
            timeout=TIMEOUT_S,
        )
        reponse.raise_for_status()
        resultat = reponse.json()
    except (requests.RequestException, ValueError):
        return None  # refus ou panne : le signalement garde sa voie OSM, sans troncon
    aretes = resultat.get('edges') or []
    points = resultat.get('matched_points') or []
    index = points[1].get('edge_index') if len(points) > 1 else None
    if index is None or not 0 <= index < len(aretes):
        return None
    debut = aretes[index].get('node_id')
    fin = (aretes[index].get('end_node') or {}).get('node_id')
    if debut is None or fin is None:
        cache.set(CLE_NOEUDS_INDISPONIBLES, True, timeout=3600)  # Valhalla < 3.6 ou tuiles sans noeuds
        return None
    return int(debut), int(fin)
