"""
ClientMeili : appelle /trace_attributes (map-matching), pas /route
(itineraires, cf. client_valhalla.py) -- meme conteneur Valhalla, donc
partage son disjoncteur (disjoncteur.py) : si Valhalla est en panne, les deux
clients le savent en meme temps.

Une correspondance vide ou partielle (trace GPS bruitee, points hors reseau
routier connu) est une reponse VALIDE de Meili, pas une erreur : ErreurMeili
n'est levee que pour une panne reseau/HTTP reelle. Confondre les deux ferait
ouvrir le disjoncteur sur de la mauvaise qualite de donnees plutot que sur une
panne de service -- exactement ce qu'il ne faut pas.
"""

import requests
from django.conf import settings

from . import disjoncteur

TIMEOUT_S = 5


class ErreurMeili(Exception):
    """Panne reseau/HTTP de Valhalla -- distincte d'une correspondance vide,
    qui est un resultat normal renvoye sans lever d'exception."""


def tracer(points: list[dict], shape_match: str = 'map_snap') -> tuple[list[dict], list[dict]]:
    """points : [{'lat':, 'lon':, 'time': <secondes ecoulees depuis le premier
    point du lot>}, ...], au moins 2 points.

    shape_match : 'map_snap' pour une trace GPS bruitee (positions brutes,
    cf. consommateur_positions.py) ; 'walk_or_snap' pour une geometrie deja
    issue de Valhalla /route (cf. service_trafic.py) -- tente edge_walk
    (rapide, precis) et ne bascule sur map_snap que si necessaire, plutot que
    d'echouer sur la moindre micro-discontinuite de snapping.

    Retourne (edges, matched_points) tels que renvoyes par Valhalla -- edges
    peut etre vide et des matched_points peuvent avoir edge_index=None : ce
    n'est pas une erreur, l'appelant decide quoi en faire (cf. module docstring)."""
    disjoncteur.verifier()  # leve DisjoncteurOuvert sans appel reseau si Valhalla est deja marque en panne

    try:
        reponse = requests.post(
            f'{settings.VALHALLA_URL}/trace_attributes',
            json={
                'shape': points,
                'costing': 'auto',
                'shape_match': shape_match,
                'filters': {'attributes': ['edge.id', 'edge.length', 'edge.speed'], 'action': 'include'},
            },
            timeout=TIMEOUT_S,
        )
        reponse.raise_for_status()
    except requests.RequestException as exc:
        disjoncteur.enregistrer_echec()
        raise ErreurMeili(str(exc)) from exc

    disjoncteur.reinitialiser_echecs()
    data = reponse.json()
    return data.get('edges', []), data.get('matched_points', [])
