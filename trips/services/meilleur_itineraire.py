"""
Meilleur itineraire en cours de route : pendant la navigation, l'appli envoie
sa position toutes les 1-2 min (POST /api/trips/{id}/better-route/). On
recalcule les itineraires depuis cette position avec le trafic temps reel
(deja injecte dans Valhalla, cf. trafic_temps_reel.py), on reconnait parmi
eux celui que le conducteur suit deja, et on propose le plus rapide s'il fait
gagner assez de temps. Le conducteur accepte ou ignore : jamais de
changement impose (POST /api/trips/{id}/switch-route/ pour accepter).
"""

from django.contrib.gis.geos import LineString, Point

from ..polyline import decoder_polyline6
from .geo import distance_haversine_m
from .service_itineraire import ServiceItineraire

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

BETTER_FOUND = 'BETTER_FOUND'
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


def chercher_meilleur(trajet, position, utilisateur, cap=None, etapes_restantes=None):
    """position : (lat, lon). Retourne un dict :
      reason : BETTER_FOUND | NO_BETTER | NO_ALTERNATIVE | OFF_ROUTE | NO_ROUTE
      better_route : candidat (forme ServiceItineraire) si BETTER_FOUND
      current_duration_s, new_duration_s, time_saved_s."""
    resultat = {'reason': NO_BETTER, 'better_route': None, 'current_duration_s': None,
                'new_duration_s': None, 'time_saved_s': None}
    candidats = ServiceItineraire().calculer(
        depart=position,
        arrivee=(trajet.position_destination.y, trajet.position_destination.x),
        utilisateur=utilisateur, cap_origine=cap, alternatives=True, etapes=etapes_restantes or [],
        type_vehicule=trajet.type_vehicule or None,
    )
    if not candidats:
        return {**resultat, 'reason': NO_ROUTE}
    if trajet.geometrie is None or len(candidats) < 2:
        return {**resultat, 'reason': NO_ALTERNATIVE}

    reference = LineString(trajet.geometrie.coords, srid=4326)
    scores = [(recouvrement(c, reference), c) for c in candidats]
    score_actuel, actuel = max(scores, key=lambda s: s[0])
    if score_actuel < SEUIL_RECOUVREMENT:
        # Le conducteur a quitte sa route : c'est un recalcul classique
        # (routes/calculate), pas une proposition de "meilleur" itineraire.
        return {**resultat, 'reason': OFF_ROUTE}

    meilleur = min(candidats, key=duree_effective)
    actuelle, nouvelle = duree_effective(actuel), duree_effective(meilleur)
    resultat.update(current_duration_s=actuelle, new_duration_s=nouvelle)
    gain = actuelle - nouvelle
    if meilleur is actuel or gain < GAIN_MIN_S or gain < GAIN_MIN_RATIO * actuelle:
        return resultat
    return {**resultat, 'reason': BETTER_FOUND, 'better_route': meilleur, 'time_saved_s': gain}


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
