"""
ClientLocate : appelle /locate (position -> arete du graphe routier la plus
proche), pas /route ni /trace_attributes -- meme conteneur Valhalla, partage
son disjoncteur (disjoncteur.py) : si Valhalla est en panne, tous les clients
de ce module le savent en meme temps.

Usage principal : verifier qu'un signalement d'incident est bien sur/pres
d'une route (cf. community/services.py) plutot qu'a l'interieur d'un
batiment ou d'un parc. Valhalla renvoie toujours l'arete la plus proche,
aussi loin soit-elle -- /locate ne leve jamais d'erreur "rien trouve", c'est
a l'appelant de juger la distance retournee.
"""

import math

import requests
from django.conf import settings

from . import disjoncteur

TIMEOUT_S = 5


class ErreurLocate(Exception):
    """Panne reseau/HTTP de Valhalla -- l'appelant doit se degrader (ne pas
    bloquer l'action en cours) plutot que de laisser cette exception remonter,
    meme principe que ClientNominatim/ClientMeili."""


def distance_a_la_route_m(lat: float, lon: float) -> float | None:
    """Distance (metres) entre (lat, lon) et l'arete routiere connue de
    Valhalla la plus proche. None si Valhalla ne connait aucune route dans
    la zone (tres rare -- meme un point isole trouve generalement l'arete
    la moins lointaine). Leve ErreurLocate/DisjoncteurOuvert si Valhalla est
    indisponible -- a l'appelant de decider du repli."""
    disjoncteur.verifier()

    try:
        reponse = requests.post(
            f'{settings.VALHALLA_URL}/locate',
            json={'locations': [{'lat': lat, 'lon': lon}], 'costing': 'auto'},
            timeout=TIMEOUT_S,
        )
        reponse.raise_for_status()
    except requests.RequestException as exc:
        disjoncteur.enregistrer_echec()
        raise ErreurLocate(str(exc)) from exc

    disjoncteur.reinitialiser_echecs()
    resultats = reponse.json()
    if not resultats or not resultats[0].get('edges'):
        return None

    arete = resultats[0]['edges'][0]
    return _distance_haversine_m(lat, lon, arete['correlated_lat'], arete['correlated_lon'])


def _distance_haversine_m(lat1, lon1, lat2, lon2):
    rayon_terre_m = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = math.sin(delta_lat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(delta_lon / 2) ** 2
    return 2 * rayon_terre_m * math.asin(math.sqrt(a))
