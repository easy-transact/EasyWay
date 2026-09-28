"""
ClientValhalla : view -> ServiceItineraire -> ClientValhalla (jamais d'appel
direct a Valhalla depuis une vue). Le disjoncteur (partage avec client_meili.py,
cf. disjoncteur.py) vit dans le cache Django (Redis) plutot qu'en memoire de
process, pour que son etat soit partage entre workers/process.
"""

import copy
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings

from trips.polyline import decoder_polyline6

from . import disjoncteur
from .client_routage import ClientRoutage, ErreurRoutage
from .disjoncteur import DisjoncteurOuvert
from .geo import distance_haversine_m

VITESSE_REPLI_KMH = 25  # vitesse urbaine moyenne prudente, pour l'estimation degradee

NOMBRE_MAX_ITINERAIRES = 3
# Variantes par detour (cf. _variantes_par_detour) reservees aux trajets
# longs : en ville, les alternates de Valhalla suffisent en general, et un
# carre d'exclusion de ~900 m y couperait vite tout un quartier.
DISTANCE_MIN_DETOUR_KM = 20
# Positions (fraction de la distance de la meilleure route) ou l'on bloque un
# carre pour forcer un autre chemin -- verifie sur Douala -> Bafoussam :
# 0.3 -> par Yabassi, 0.5 -> Provinciale 17a, 0.7/0.85 -> par la N4.
# Symetrique autour de 0.5 pour que le sens retour trouve les memes corridors.
FRACTIONS_DETOUR = (0.15, 0.3, 0.5, 0.7, 0.85)
DEMI_COTE_DETOUR_DEG = 0.004  # ~450 m : carre de ~900 m, perimetre bien sous max_exclude_polygons_length (10 km)
# Un carre trop pres d'une etape la rendrait inaccessible.
DISTANCE_MIN_DETOUR_ETAPE_M = 2000
# Filtres d'une variante vs les routes deja retenues -- ordre de grandeur de
# ce que Google propose, pas mesure sur donnees reelles.
RATIO_TEMPS_MAX_VARIANTE = 1.3
PARTAGE_MAX_VARIANTE = 0.8
TAILLE_CELLULE_DEG = 0.001  # ~110 m : deux routes paralleles distinctes tombent dans des cellules differentes


def _points(trip):
    """(lon, lat) de tout le trip -- chaque leg decode separement."""
    return [point for leg in trip['legs'] for point in decoder_polyline6(leg['shape'])]


def _cellules(trip):
    """Cellules de ~110 m traversees par le trip, segments densifies pour
    que la mesure de partage ne depende pas de la densite de points de la
    geometrie (un long segment droit n'a que deux sommets)."""
    points = _points(trip)
    cellules = set()
    for (lon_a, lat_a), (lon_b, lat_b) in zip(points, points[1:]):
        pas = max(1, int(2 * max(abs(lat_b - lat_a), abs(lon_b - lon_a)) / TAILLE_CELLULE_DEG))
        for i in range(pas):
            t = i / pas
            cellules.add((
                round((lat_a + (lat_b - lat_a) * t) / TAILLE_CELLULE_DEG),
                round((lon_a + (lon_b - lon_a) * t) / TAILLE_CELLULE_DEG),
            ))
    if points:
        lon, lat = points[-1]
        cellules.add((round(lat / TAILLE_CELLULE_DEG), round(lon / TAILLE_CELLULE_DEG)))
    return cellules


def selectionner_variantes(retenus, candidats):
    """Complete `retenus` (le premier est la route recommandee, reference
    pour le temps) avec les candidats les plus rapides qui restent a au
    plus RATIO_TEMPS_MAX_VARIANTE fois son temps et partagent au plus
    PARTAGE_MAX_VARIANTE de leur trace avec chaque route deja retenue --
    une variante qui ne differe que de quelques centaines de metres n'est
    pas une vraie option. Partage aussi par ClientCorridorReference."""
    temps_max = retenus[0]['summary']['time'] * RATIO_TEMPS_MAX_VARIANTE
    selection = [(trip, _cellules(trip)) for trip in retenus]
    for candidat in sorted(candidats, key=lambda t: t['summary']['time']):
        if len(selection) >= NOMBRE_MAX_ITINERAIRES:
            break
        if candidat['summary']['time'] > temps_max:
            continue
        cellules = _cellules(candidat)
        if not cellules:
            continue
        if any(len(cellules & autres) / len(cellules) > PARTAGE_MAX_VARIANTE for _, autres in selection):
            continue
        selection.append((candidat, cellules))
    return [trip for trip, _ in selection]


def _carre(lat, lon):
    d = DEMI_COTE_DETOUR_DEG
    return [[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]


def _points_a_fractions(points, fractions):
    """(lat, lon) situes a chaque fraction de la longueur de la ligne."""
    cumul = [0.0]
    for (lon_a, lat_a), (lon_b, lat_b) in zip(points, points[1:]):
        cumul.append(cumul[-1] + distance_haversine_m((lat_a, lon_a), (lat_b, lon_b)))
    resultat = []
    indice = 1
    for fraction in fractions:
        cible = cumul[-1] * fraction
        while indice < len(cumul) - 1 and cumul[indice] < cible:
            indice += 1
        # Interpole dans le segment : un long segment droit n'a que deux sommets.
        longueur = cumul[indice] - cumul[indice - 1]
        t = (cible - cumul[indice - 1]) / longueur if longueur else 0
        (lon_a, lat_a), (lon_b, lat_b) = points[indice - 1], points[indice]
        resultat.append((lat_a + (lat_b - lat_a) * t, lon_a + (lon_b - lon_a) * t))
    return resultat


class ClientValhalla(ClientRoutage):
    TIMEOUT_S = 5
    TENTATIVES = 2

    def calculer_itineraires(self, depart, arrivee, options, cap_origine=None, alternatives=True, etapes=None):
        try:
            disjoncteur.verifier()
            if alternatives:
                trips = self._collecter_variantes(depart, arrivee, options, cap_origine, etapes)
            else:
                # alternatives=False reellement honore : un seul appel Valhalla
                # (alternates=0), jamais le deuxieme appel "shortest" de
                # _collecter_variantes -- pas seulement trips[:1] apres coup,
                # qui aurait quand meme paye le cout des variantes.
                trips = self._appeler_avec_retry(
                    depart, arrivee, options, alternates=0, cap_origine=cap_origine, etapes=etapes
                )
        except (DisjoncteurOuvert, ErreurRoutage):
            return self.replier(depart, arrivee, etapes)
        disjoncteur.reinitialiser_echecs()
        return trips

    def _collecter_variantes(self, depart, arrivee, options, cap_origine=None, etapes=None):
        """Le propre algorithme d'alternates de Valhalla est conservateur :
        il rejette toute alternative qui partage trop de trace avec la
        meilleure ou coute plus de ~1.25x (contraintes compilees, non
        reglables dans valhalla.json). Sur Douala -> Bafoussam, le tronc
        commun Douala-Melong suffit a eliminer les corridors par Dschang et
        par la N4 que Google propose.

        Trajets longs : on bloque un petit carre sur la meilleure route a
        plusieurs endroits (exclude_polygons) pour forcer Valhalla a trouver
        un autre chemin -- appels en parallele, cf. _variantes_par_detour.
        Trajets courts : un appel avec l'objectif distance (shortest) plutot
        que temps. Dans les deux cas, selectionner_variantes ecarte les
        candidats trop lents ou quasi identiques a une route deja retenue."""
        trips = self._appeler_avec_retry(
            depart, arrivee, options, alternates=2, cap_origine=cap_origine, etapes=etapes
        )
        if len(trips) >= NOMBRE_MAX_ITINERAIRES:
            return trips[:NOMBRE_MAX_ITINERAIRES]

        if trips[0]['summary']['length'] >= DISTANCE_MIN_DETOUR_KM:
            candidats = self._variantes_par_detour(trips[0], depart, arrivee, options, cap_origine, etapes)
        else:
            options_distance = copy.deepcopy(options)
            costing = options_distance.get('costing', 'auto')
            options_distance.setdefault('costing_options', {}).setdefault(costing, {})['shortest'] = True
            candidats = self._appeler_variante(depart, arrivee, options_distance, cap_origine, etapes)

        return selectionner_variantes(trips, candidats)

    def _variantes_par_detour(self, meilleur, depart, arrivee, options, cap_origine=None, etapes=None):
        centres = [
            centre for centre in _points_a_fractions(_points(meilleur), FRACTIONS_DETOUR)
            if all(distance_haversine_m(centre, etape) >= DISTANCE_MIN_DETOUR_ETAPE_M for etape in etapes or [])
        ]
        if not centres:
            return []

        def appeler(centre):
            options_detour = copy.deepcopy(options)
            options_detour['exclude_polygons'] = [*options_detour.get('exclude_polygons', []), _carre(*centre)]
            return self._appeler_variante(depart, arrivee, options_detour, cap_origine, etapes)

        with ThreadPoolExecutor(max_workers=len(centres)) as executeur:
            return [trip for trips in executeur.map(appeler, centres) for trip in trips]

    def _appeler_variante(self, depart, arrivee, options, cap_origine=None, etapes=None):
        """Un seul essai, hors disjoncteur : une variante est un bonus. Un
        carre d'exclusion peut legitimement couper le seul chemin possible
        (pont, col) -- Valhalla repond alors 400 "no path", qui ne doit pas
        compter comme une panne du moteur."""
        try:
            return self._appeler(depart, arrivee, options, 0, cap_origine, etapes)
        except requests.RequestException:
            return []

    def replier(self, depart, arrivee, etapes=None):
        # Import local : evite un cycle (trips.polyline n'a pas besoin de
        # connaitre trips.services, seul ce module a besoin des deux).
        from trips.polyline import encoder_polyline6

        points = [depart, *(etapes or []), arrivee]
        legs = []
        distance_totale_m = 0
        for point_a, point_b in zip(points, points[1:]):
            distance_m = distance_haversine_m(point_a, point_b)
            distance_totale_m += distance_m
            shape = encoder_polyline6([(point_a[1], point_a[0]), (point_b[1], point_b[0])])
            legs.append({'shape': shape, 'maneuvers': []})

        duree_s = distance_totale_m / (VITESSE_REPLI_KMH * 1000 / 3600)
        return [{
            'summary': {'length': round(distance_totale_m / 1000, 2), 'time': round(duree_s)},
            'legs': legs,
            'degrade': True,
        }]

    def _appeler_avec_retry(self, depart, arrivee, options, alternates, cap_origine=None, etapes=None):
        derniere_erreur = None
        for _ in range(self.TENTATIVES):
            try:
                return self._appeler(depart, arrivee, options, alternates, cap_origine, etapes)
            except requests.RequestException as exc:
                derniere_erreur = exc
        disjoncteur.enregistrer_echec()
        raise ErreurRoutage(str(derniere_erreur))

    def _appeler(self, depart, arrivee, options, alternates, cap_origine=None, etapes=None):
        origine = {'lat': depart[0], 'lon': depart[1]}
        if cap_origine is not None:
            # cf. https://valhalla.github.io/valhalla/api/turn-by-turn/api-reference/#locations
            # -- heading_tolerance laisse au defaut Valhalla (60), pas mesure sur donnees reelles.
            origine['heading'] = cap_origine

        locations = [origine]
        locations += [{'lat': lat, 'lon': lon} for lat, lon in (etapes or [])]
        locations.append({'lat': arrivee[0], 'lon': arrivee[1]})

        payload = {
            'locations': locations,
            'units': 'kilometers',
            'language': 'fr-FR',
            'alternates': alternates,
            **options,
        }
        reponse = requests.post(f'{settings.VALHALLA_URL}/route', json=payload, timeout=self.TIMEOUT_S)
        reponse.raise_for_status()
        data = reponse.json()
        return [data['trip']] + [alt['trip'] for alt in data.get('alternates', [])]
