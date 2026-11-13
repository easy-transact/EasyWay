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


ATTRIBUTS_DEFAUT = ['edge.id', 'edge.length', 'edge.speed']
_optionnels_acceptes = {}  # {attributs optionnels: combien cette version de Valhalla en accepte}


def tracer(points: list[dict], shape_match: str = 'map_snap', attributs=None,
           attributs_optionnels=None) -> tuple[list[dict], list[dict]]:
    """points : [{'lat':, 'lon':, 'time': <secondes ecoulees depuis le premier
    point du lot>}, ...], au moins 2 points.

    shape_match : 'map_snap' pour une trace GPS bruitee (positions brutes,
    cf. consommateur_positions.py) ; 'walk_or_snap' pour une geometrie deja
    issue de Valhalla /route (cf. service_trafic.py) -- tente edge_walk
    (rapide, precis) et ne bascule sur map_snap que si necessaire, plutot que
    d'echouer sur la moindre micro-discontinuite de snapping.

    attributs : attributs d'arete/noeud demandes (defaut ATTRIBUTS_DEFAUT) --
    etat_routes demande aussi way_id, road_class, noms, rond-point, feux.
    attributs_optionnels : ajoutes a `attributs` si cette version de
    Valhalla les accepte, abandonnes sinon.

    Retourne (edges, matched_points) tels que renvoyes par Valhalla -- edges
    peut etre vide et des matched_points peuvent avoir edge_index=None : ce
    n'est pas une erreur, l'appelant decide quoi en faire (cf. module docstring)."""
    disjoncteur.verifier()  # leve DisjoncteurOuvert sans appel reseau si Valhalla est deja marque en panne

    def appeler(liste):
        return requests.post(
            f'{settings.VALHALLA_URL}/trace_attributes',
            json={
                'shape': points,
                'costing': 'auto',
                'shape_match': shape_match,
                'filters': {'attributes': liste, 'action': 'include'},
            },
            timeout=TIMEOUT_S,
        )

    try:
        reponse = None
        requis = list(attributs or ATTRIBUTS_DEFAUT)
        optionnels = list(attributs_optionnels or [])
        # Attributs utiles mais pas indispensables (ex. edge.forward, noeuds
        # OSM) : on retire les derniers tant que cette version de Valhalla
        # repond 400, jamais au prix des attributs requis. Le nombre accepte
        # est memorise par process : une seule serie d'essais, pas a chaque lot.
        cle_support = tuple(optionnels)
        nb = _optionnels_acceptes.get(cle_support, len(optionnels))
        while nb > 0:
            reponse = appeler(requis + optionnels[:nb])
            if reponse.status_code != 400:
                break
            reponse = None
            nb -= 1
        if reponse is None:
            reponse = appeler(requis)
        # Memorise seulement si un essai a reussi : un 400 sur TOUS les essais
        # vient de la trace (impossible a recaler), pas des attributs.
        if optionnels and reponse.status_code != 400:
            _optionnels_acceptes[cle_support] = nb
        if reponse.status_code == 400 and attributs and attributs != ATTRIBUTS_DEFAUT:
            # Un attribut supplementaire (etat_routes) refuse par cette version
            # de Valhalla ne doit jamais couper le trafic : nouvel essai avec
            # les attributs de base.
            reponse = appeler(ATTRIBUTS_DEFAUT)
        reponse.raise_for_status()
    except requests.RequestException as exc:
        if disjoncteur.est_requete_refusee(exc):
            # Valhalla repond 400 quand le recalage echoue completement : meme
            # sens qu'une correspondance vide (mauvaise trace, pas une panne).
            # Ne pas lever : le consommateur acquitte les messages au lieu de
            # rejouer indefiniment une trace que Valhalla refusera toujours.
            return [], []
        disjoncteur.enregistrer_echec()
        raise ErreurMeili(str(exc)) from exc

    disjoncteur.reinitialiser_echecs()
    data = reponse.json()
    return data.get('edges', []), data.get('matched_points', [])
