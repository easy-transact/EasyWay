"""
Meilleur itineraire en cours de route : pendant la navigation, l'appli envoie
sa position toutes les 1-2 min (POST /api/trips/{id}/better-route/). On
recalcule les itineraires depuis cette position avec le trafic temps reel
(deja injecte dans Valhalla, cf. trafic_temps_reel.py), on reconnait parmi
eux celui que le conducteur suit deja, et on propose : un changement oblige
si la route est barree devant, le plus rapide s'il fait gagner assez de temps
sans plus de risque, ou le plus sur s'il evite un danger serieux devant
(cf. risque_itineraire). Le conducteur accepte ou ignore : jamais de
changement impose (POST /api/trips/{id}/switch-route/ pour accepter).
"""

import time

from django.contrib.gis.geos import LineString, Point
from django.core.cache import cache

from ..polyline import decoder_polyline6
from . import risque_itineraire as risque
from .geo import distance_haversine_m
from .service_itineraire import COSTING_DEFAUT, COSTING_PAR_VEHICULE, ServiceItineraire

# Gain minimal pour deranger le conducteur : les deux conditions a la fois
# (2 min sur 10 min de trajet = 20 %, mais 2 min sur 2 h ne vaut pas un
# changement de route). Valeurs pas mesurees sur donnees reelles, a ajuster.
GAIN_MIN_S = 120
GAIN_MIN_RATIO = 0.10
# Un candidat "est" la route suivie si au moins 70 % de ses points sont a
# moins de 40 m de la geometrie du trajet.
COULOIR_M = 40
SEUIL_RECOUVREMENT = 0.7
# Echantillonnage des points d'un candidat pour le calcul de recouvrement.
NB_POINTS_ECHANTILLON = 60
METRES_PAR_DEGRE = 111_320

# "Plus sur" (cf. risque_itineraire) : le cout (duree + risque) doit baisser
# d'au moins 3 min-equivalent ET 15 %, en evitant au moins un danger serieux.
BAISSE_MIN_COUT_S = 180
BAISSE_MIN_RATIO = 0.15
# Une route plus rapide n'est proposee que si elle n'ajoute pas plus d'1 point de risque.
TOLERANCE_RISQUE = 1
# Stabilite : une route n'est proposee que si elle sort meilleure a deux
# evaluations de suite (sinon deux routes de cout voisin alternent a chaque
# appel), et une route deja proposee (donc refusee si le trajet ne l'a pas
# prise) ne revient pas avant 15 min -- sauf danger aggrave entre-temps.
# Jamais de delai pour une route barree.
ATTENTE_MAX_S = 5 * 60        # au-dela, la premiere evaluation est perimee
REFUS_S = 15 * 60
AGGRAVATION = 1.5             # un danger revient s'il vaut 50 % de points en plus
CLE_PROPOSITIONS = 'better-route:propositions:'

REROUTE_REQUIRED = 'REROUTE_REQUIRED'
BETTER_FOUND = 'BETTER_FOUND'
SAFER_FOUND = 'SAFER_FOUND'
HAZARD_AHEAD = 'HAZARD_AHEAD'
NO_BETTER = 'NO_BETTER'
NO_ALTERNATIVE = 'NO_ALTERNATIVE'
OFF_ROUTE = 'OFF_ROUTE'
NO_ROUTE = 'NO_ROUTE'


def duree_effective(candidat):
    return candidat['duree_avec_trafic'] or candidat['duree']


def recouvrement(candidat, reference):
    """Part des points du candidat a moins de COULOIR_M de la ligne de
    reference (geometrie du trajet en cours)."""
    points = decoder_polyline6(candidat['geometrie'])
    if len(points) > NB_POINTS_ECHANTILLON:
        pas = len(points) / NB_POINTS_ECHANTILLON
        points = [points[int(i * pas)] for i in range(NB_POINTS_ECHANTILLON)]
    proches = sum(1 for lon, lat in points if reference.distance(Point(lon, lat)) * METRES_PAR_DEGRE <= COULOIR_M)
    return proches / len(points)


def _dangers_serieux(candidat):
    return [d for d in candidat.get('risques') or [] if d['severity'] >= risque.GRAVITE_SERIEUSE]


def _echantillon(route):
    points = decoder_polyline6(route['geometrie'])
    if len(points) > NB_POINTS_ECHANTILLON:
        pas = len(points) / NB_POINTS_ECHANTILLON
        points = [points[int(i * pas)] for i in range(NB_POINTS_ECHANTILLON)]
    return points


def _meme_route(route, coords):
    return len(coords) >= 2 and recouvrement(route, LineString(coords, srid=4326)) >= SEUIL_RECOUVREMENT


def _stable(trajet, route, evites):
    """(proposer maintenant, en attente d'une 2e evaluation). Memoire par
    trajet dans le cache : {'attente': {coords, le}, 'proposees': [{coords, le, dangers}]}."""
    cle = f'{CLE_PROPOSITIONS}{trajet.id}'
    memoire = cache.get(cle) or {}
    maintenant = time.time()
    proposees = [p for p in memoire.get('proposees', []) if maintenant - p['le'] < REFUS_S]
    for deja in proposees:
        aggrave = any(d['id'] not in deja['dangers'] or d['points'] >= deja['dangers'][d['id']] * AGGRAVATION
                      for d in evites)
        if _meme_route(route, deja['coords']) and not aggrave:
            return False, False

    attente = memoire.get('attente')
    if attente and maintenant - attente['le'] <= ATTENTE_MAX_S and _meme_route(route, attente['coords']):
        proposees.append({'coords': _echantillon(route), 'le': maintenant,
                          'dangers': {d['id']: d['points'] for d in evites}})
        cache.set(cle, {'proposees': proposees}, timeout=REFUS_S)
        return True, False
    cache.set(cle, {'proposees': proposees, 'attente': {'coords': _echantillon(route), 'le': maintenant}},
              timeout=REFUS_S)
    return False, True


def troncon_restant(coords, position):
    """Partie de la geometrie (liste (lon, lat)) qui reste a parcourir depuis
    position (lat, lon)."""
    parcouru = troncon_parcouru(coords, position)
    return [(position[1], position[0]), *coords[len(parcouru):]] if coords else []


# Forme publique (contrat mobile, section 19) : reason reste dans les valeurs
# que les versions installees de l'appli connaissent ; action/cause disent quoi faire.
PUBLIC = {
    REROUTE_REQUIRED: (BETTER_FOUND, 'IMPOSE', 'ROAD_CLOSED'),
    BETTER_FOUND: (BETTER_FOUND, 'PROPOSE', 'FASTER'),
    SAFER_FOUND: (BETTER_FOUND, 'PROPOSE', 'SAFER'),
    HAZARD_AHEAD: (NO_BETTER, 'ALERT', None),
}


def chercher_meilleur(trajet, position, utilisateur, cap=None, etapes_restantes=None, eviter=None):
    """position : (lat, lon). Retourne un dict :
      reason : BETTER_FOUND | NO_BETTER | NO_ALTERNATIVE | OFF_ROUTE | NO_ROUTE
      action : IMPOSE | PROPOSE | ALERT | NONE ; cause : ROAD_CLOSED | FASTER | SAFER | None
      better_route : candidat (forme ServiceItineraire) si IMPOSE ou PROPOSE
      alert : danger serieux le plus proche devant ({incident_id, type, subtype,
               distance_m, severity}) ou None
      current_duration_s, new_duration_s, time_saved_s (negatif : plus long),
      current_risk_score, hazards_ahead, avoided_hazards,
      pending_confirmation (route meilleure trouvee, proposee si elle l'est
      encore a l'appel suivant)."""
    interne = _decider(trajet, position, utilisateur, cap, etapes_restantes, eviter)
    raison, action, cause = PUBLIC.get(interne['reason'], (interne['reason'], 'NONE', None))
    devant = interne['hazards_ahead']
    alerte = devant[0] if devant else None
    return {
        **interne, 'reason': raison, 'action': action, 'cause': cause,
        'alert': alerte and {
            'incident_id': alerte['id'], 'type': alerte['type'], 'subtype': alerte['subtype'],
            'distance_m': alerte['distance_from_start_m'], 'severity': alerte['severity'],
        },
    }


def _decider(trajet, position, utilisateur, cap, etapes_restantes, eviter):
    resultat = {'reason': NO_BETTER, 'better_route': None, 'current_duration_s': None,
                'new_duration_s': None, 'time_saved_s': None, 'current_risk_score': None,
                'hazards_ahead': [], 'avoided_hazards': [], 'pending_confirmation': False}
    candidats = ServiceItineraire().calculer(
        depart=position,
        arrivee=(trajet.position_destination.y, trajet.position_destination.x),
        utilisateur=utilisateur, cap_origine=cap, alternatives=True, etapes=etapes_restantes or [],
        type_vehicule=trajet.type_vehicule or None, eviter=eviter or [],
    )
    if not candidats:
        return {**resultat, 'reason': NO_ROUTE}
    if trajet.geometrie is None:
        return {**resultat, 'reason': NO_ALTERNATIVE}

    # Route barree confirmee devant sur la route suivie : les candidats la
    # contournent deja (exclue du calcul) -- changement necessaire, meme plus long.
    costing = COSTING_PAR_VEHICULE.get(trajet.type_vehicule or utilisateur.type_vehicule, COSTING_DEFAUT)
    fermetures = risque.fermetures_devant(troncon_restant(list(trajet.geometrie.coords), position), costing)
    if fermetures:
        nouvelle = candidats[0]
        return {
            **resultat, 'reason': REROUTE_REQUIRED, 'better_route': nouvelle,
            'new_duration_s': duree_effective(nouvelle), 'hazards_ahead': fermetures, 'avoided_hazards': fermetures,
        }

    reference = LineString(trajet.geometrie.coords, srid=4326)
    scores = [(recouvrement(c, reference), c) for c in candidats]
    score_actuel, actuel = max(scores, key=lambda s: s[0])
    if score_actuel < SEUIL_RECOUVREMENT:
        # Le conducteur a quitte sa route : c'est un recalcul classique
        # (routes/calculate), pas une proposition de "meilleur" itineraire.
        return {**resultat, 'reason': OFF_ROUTE}

    actuelle = duree_effective(actuel)
    devant = _dangers_serieux(actuel)
    resultat.update(current_duration_s=actuelle, current_risk_score=actuel.get('risque', 0), hazards_ahead=devant)
    autres = [c for c in candidats if c is not actuel]
    if not autres:
        return {**resultat, 'reason': HAZARD_AHEAD if devant else NO_ALTERNATIVE}

    propositions = []
    # 1. Plus rapide, sans etre plus risquee.
    rapide = min(autres, key=duree_effective)
    gain = actuelle - duree_effective(rapide)
    if (gain >= GAIN_MIN_S and gain >= GAIN_MIN_RATIO * actuelle
            and rapide.get('risque', 0) <= actuel.get('risque', 0) + TOLERANCE_RISQUE):
        sur_rapide = {d['id'] for d in rapide.get('risques') or []}
        propositions.append((BETTER_FOUND, rapide, [d for d in devant if d['id'] not in sur_rapide]))

    # 2. Plus sure : evite un danger serieux encore assez loin pour changer de
    # route, pour un cout (duree + risque) nettement plus bas, detour borne.
    contournables = [d for d in devant if d['distance_from_start_m'] >= risque.DISTANCE_MIN_CONTOURNEMENT_M]
    if contournables:
        limite = actuelle + risque.ecart_max_s(actuelle)
        sure = min((c for c in autres if duree_effective(c) <= limite), key=risque.cout, default=None)
        if sure is not None:
            sur_sure = {d['id'] for d in sure.get('risques') or []}
            evites = [d for d in contournables if d['id'] not in sur_sure]
            baisse = risque.cout(actuel) - risque.cout(sure)
            if evites and baisse >= BAISSE_MIN_COUT_S and baisse >= BAISSE_MIN_RATIO * risque.cout(actuel):
                propositions.append((SAFER_FOUND, sure, evites))

    for raison, route, evites in propositions:
        maintenant, en_attente = _stable(trajet, route, evites)
        if maintenant:
            return {**resultat, 'reason': raison, 'better_route': route, 'new_duration_s': duree_effective(route),
                    'time_saved_s': actuelle - duree_effective(route), 'avoided_hazards': evites}
        if en_attente:
            resultat['pending_confirmation'] = True
            break

    # 3. Rien a proposer (maintenant) : on previent quand meme du danger.
    return {**resultat, 'reason': HAZARD_AHEAD} if devant else resultat


ALTERNATIVES_FOUND = 'ALTERNATIVES_FOUND'


def chercher_autres(trajet, position, utilisateur, cap=None, etapes_restantes=None, eviter=None):
    """Le conducteur DEMANDE un autre itineraire ("je veux une autre route") :
    contrairement a chercher_meilleur, aucune condition de gain -- toutes les
    routes differentes de celle suivie, plus lentes comprises, de la plus
    rapide a la plus lente. Retourne {reason, routes, current_duration_s},
    chaque route portant ecart_s (difference de duree avec la route suivie,
    positive = plus lente ; None si la route suivie n'est pas reconnue).
      reason : ALTERNATIVES_FOUND | NO_ALTERNATIVE | NO_ROUTE"""
    candidats = ServiceItineraire().calculer(
        depart=position,
        arrivee=(trajet.position_destination.y, trajet.position_destination.x),
        utilisateur=utilisateur, cap_origine=cap, alternatives=True, etapes=etapes_restantes or [],
        type_vehicule=trajet.type_vehicule or None, eviter=eviter or [],
    )
    if not candidats:
        return {'reason': NO_ROUTE, 'routes': [], 'current_duration_s': None}

    actuelle = None
    autres = candidats
    if trajet.geometrie is not None:
        reference = LineString(trajet.geometrie.coords, srid=4326)
        scores = [(recouvrement(c, reference), c) for c in candidats]
        score_actuel, suivie = max(scores, key=lambda s: s[0])
        if score_actuel >= SEUIL_RECOUVREMENT:
            actuelle = duree_effective(suivie)
            autres = [c for score, c in scores if score < SEUIL_RECOUVREMENT]
    autres = sorted(autres, key=duree_effective)
    for route in autres:
        route['ecart_s'] = duree_effective(route) - actuelle if actuelle is not None else None
    return {
        'reason': ALTERNATIVES_FOUND if autres else NO_ALTERNATIVE,
        'routes': autres,
        'current_duration_s': actuelle,
    }


def troncon_parcouru(coords, position):
    """Partie deja parcourue de la geometrie (liste (lon, lat)) jusqu'au point
    le plus proche de position (lat, lon) -- gardee quand le conducteur change
    de route, pour que le trace du trajet reste complet."""
    if len(coords) < 2:
        return []
    lat, lon = position
    meilleur_i, meilleure_d = 0, float('inf')
    for i, (lon_i, lat_i) in enumerate(coords):
        d = distance_haversine_m((lat, lon), (lat_i, lon_i))
        if d < meilleure_d:
            meilleur_i, meilleure_d = i, d
    return list(coords[: meilleur_i + 1])
