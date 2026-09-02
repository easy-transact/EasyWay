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

    try:
        reponse = requests.post(
            f'{settings.VALHALLA_URL}/trace_attributes',
            json={
                'shape': [{'lat': lat, 'lon': lon} for lon, lat in points],
                'costing': 'auto',
                'shape_match': 'map_snap',
            },
            timeout=TIMEOUT_S,
        )
        reponse.raise_for_status()
    except requests.RequestException as exc:
        disjoncteur.enregistrer_echec()
        raise ErreurTraceAttributes(str(exc)) from exc

    disjoncteur.reinitialiser_echecs()
    resultat = reponse.json()
    aretes = resultat.get('edges')
    if not aretes:
        return None

    return [{'way_id': arete['way_id'], 'forward': arete['forward']} for arete in aretes]
