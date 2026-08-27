"""
ClientLocate : appelle /locate (position -> arete du graphe routier la plus
proche), en mode verbose (classification de l'arete) -- pas /route ni
/trace_attributes -- meme conteneur Valhalla, partage son disjoncteur
(disjoncteur.py) : si Valhalla est en panne, tous les clients de ce module
le savent en meme temps.

Usage principal : verifier qu'un signalement d'incident est bien sur une
route publique (cf. community/services.py). Deux pieges trouves en
verification live (screenshot frontend, marqueurs "flottant" hors de la
route) :
  - Valhalla renvoie toujours l'arete la plus proche, aussi loin ou non
    pertinente soit-elle -- y compris une allee privee/un parking
    (classification.use="driveway", destination_only=True) a quelques
    metres d'une vraie route. La distance seule ne suffit pas a rejeter ca.
  - Meme sur un bon appariement, le point brut soumis (GPS client ou choix
    Nominatim) peut etre a 10-20m du centre de la route -- visible a
    l'affichage carte. D'ou correlated_lat/lon renvoyes ici : l'appelant
    cale la position stockee dessus plutot que de garder le point brut.
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


def localiser(lat: float, lon: float) -> dict | None:
    """None si Valhalla ne connait aucune route dans la zone (tres rare --
    meme un point isole trouve generalement l'arete la moins lointaine).
    Sinon {'distance_m', 'lat', 'lon', 'destination_only', 'use'} pour
    l'arete routiere connue de Valhalla la plus proche : distance_m et
    lat/lon du point correle sur cette arete, destination_only (True pour
    une allee privee/un parking -- jamais une route publique) et use
    (classification Valhalla, ex. 'road', 'driveway', 'footway'). Leve
    ErreurLocate/DisjoncteurOuvert si Valhalla est indisponible -- a
    l'appelant de decider du repli."""
    disjoncteur.verifier()

    try:
        reponse = requests.post(
            f'{settings.VALHALLA_URL}/locate',
            json={'locations': [{'lat': lat, 'lon': lon}], 'costing': 'auto', 'verbose': True},
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
    return {
        'distance_m': _distance_haversine_m(lat, lon, arete['correlated_lat'], arete['correlated_lon']),
        'lat': arete['correlated_lat'],
        'lon': arete['correlated_lon'],
        'destination_only': arete['edge']['destination_only'],
        'use': arete['edge']['classification']['use'],
    }


def _distance_haversine_m(lat1, lon1, lat2, lon2):
    rayon_terre_m = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = math.sin(delta_lat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(delta_lon / 2) ** 2
    return 2 * rayon_terre_m * math.asin(math.sqrt(a))
