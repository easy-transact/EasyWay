"""
ClientMatrice : appelle /sources_to_targets de Valhalla (matrice temps /
distance) -- un depart, plusieurs destinations, en un seul calcul. Sert a
classer les lieux proches par TEMPS DE TRAJET reel plutot qu'a vol d'oiseau
(un garage a 500 m de l'autre cote d'un canal peut etre a 15 min).

Meme conteneur, meme disjoncteur que les autres clients Valhalla. Ne leve
jamais : en cas de panne ou de refus, renvoie None et l'appelant garde son
tri par distance.

Reponse Valhalla 3.5.1 (mode verbose, par defaut) :
  {"sources_to_targets": [[{"from_index", "to_index", "time": s, "distance": km}, ...]]}
time/distance null si la destination est inaccessible.
"""

import logging

import requests
from django.conf import settings

from . import disjoncteur
from .disjoncteur import DisjoncteurOuvert

logger = logging.getLogger(__name__)

TIMEOUT_S = 5
MAX_CIBLES = 100  # bien sous service_limits.<costing>.max_matrix_location_pairs (2500 par defaut)


def durees_depuis(origine, cibles, costing='auto'):
    """origine : (lat, lon) ; cibles : [(lat, lon)] (au plus MAX_CIBLES).
    Retourne [(duree_s, distance_m)] dans l'ordre des cibles (None, None pour
    une cible inaccessible), ou None si la matrice est indisponible."""
    if not cibles:
        return []
    cibles = cibles[:MAX_CIBLES]
    try:
        disjoncteur.verifier()
    except DisjoncteurOuvert:
        return None
    try:
        reponse = requests.post(
            f'{settings.VALHALLA_URL}/sources_to_targets',
            json={
                'sources': [{'lat': origine[0], 'lon': origine[1]}],
                'targets': [{'lat': lat, 'lon': lon} for lat, lon in cibles],
                'costing': costing,
                'units': 'kilometers',
            },
            timeout=TIMEOUT_S,
        )
        reponse.raise_for_status()
    except requests.RequestException as exc:
        if not disjoncteur.est_requete_refusee(exc):
            disjoncteur.enregistrer_echec()
        logger.info('Matrice Valhalla indisponible: %s', exc)
        return None
    disjoncteur.reinitialiser_echecs()

    try:
        ligne = reponse.json()['sources_to_targets'][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    resultats = [(None, None)] * len(cibles)
    for case in ligne:
        index = case.get('to_index')
        if index is None or not 0 <= index < len(cibles):
            continue
        temps, distance_km = case.get('time'), case.get('distance')
        resultats[index] = (
            round(temps) if temps is not None else None,
            round(distance_km * 1000) if distance_km is not None else None,
        )
    return resultats
