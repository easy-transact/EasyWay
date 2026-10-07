"""
Zones de vitesse le long d'un trajet (POST /api/speed-zones/along-route/) :
remplace le sondage de /api/speed-limit/ tous les 250 m cote application --
un appel par itineraire donne, pour chaque ZoneVitesse traversee, l'intervalle
[start_m, end_m] ou elle s'applique, en metres depuis le debut du trajet.

Projection en coordonnees planes (degres, GEOS) puis conversion en metres par
interpolation sur les longueurs haversine cumulees de chaque segment : la
distorsion degres/metres reste locale a un segment, negligeable a ces
latitudes, et la distance renvoyee est bien une distance routiere.
"""

import bisect
import math

from django.contrib.gis.geos import LineString, Point
from django.contrib.gis.measure import D

from ..models import ZoneVitesse
from .geo import distance_haversine_m

# Pas d'echantillonnage de la geometrie d'une zone : un echantillon tous les
# 20 m suffit a situer debut/fin de zone bien en dessous de la precision GPS
# utile pour une alerte de vitesse.
PAS_ECHANTILLON_M = 20


def zones_sur_trajet(points: list[tuple[float, float]], buffer_m: int) -> list[dict]:
    """points : (lon, lat) du trajet (decoder_polyline6). Retourne, triees par
    start_m : {zone, start_m, end_m}. Les chevauchements sont renvoyes tels
    quels -- a l'application de retenir la plus restrictive, meme regle que
    /api/speed-limit/."""
    ligne = LineString(points, srid=4326)
    zones = ZoneVitesse.objects.filter(
        actif=True,
        geometrie__isnull=False,
        geometrie__distance_lte=(ligne, D(m=buffer_m)),
    )

    cumul_plan, cumul_m = _longueurs_cumulees(points)
    resultats = []
    for zone in zones:
        positions = []
        for lon, lat in _echantillonner(zone.geometrie.coords):
            d_plan = ligne.project(Point(lon, lat, srid=4326))
            projete = ligne.interpolate(d_plan)
            if distance_haversine_m((lat, lon), (projete.y, projete.x)) <= buffer_m:
                positions.append(_plan_vers_metres(d_plan, cumul_plan, cumul_m))
        if positions:
            resultats.append({'zone': zone, 'start_m': round(min(positions)), 'end_m': round(max(positions))})

    resultats.sort(key=lambda r: (r['start_m'], r['zone'].vitesse_max_kmh))
    return resultats


def _longueurs_cumulees(points):
    cumul_plan, cumul_m = [0.0], [0.0]
    for (lon1, lat1), (lon2, lat2) in zip(points, points[1:]):
        cumul_plan.append(cumul_plan[-1] + math.hypot(lon2 - lon1, lat2 - lat1))
        cumul_m.append(cumul_m[-1] + distance_haversine_m((lat1, lon1), (lat2, lon2)))
    return cumul_plan, cumul_m


def _plan_vers_metres(d_plan, cumul_plan, cumul_m):
    i = min(max(bisect.bisect_right(cumul_plan, d_plan) - 1, 0), len(cumul_plan) - 2)
    longueur_plan = cumul_plan[i + 1] - cumul_plan[i]
    fraction = (d_plan - cumul_plan[i]) / longueur_plan if longueur_plan else 0.0
    return cumul_m[i] + fraction * (cumul_m[i + 1] - cumul_m[i])


def _echantillonner(coords):
    """Sommets de la zone + points intermediaires tous les PAS_ECHANTILLON_M :
    une zone longue dont les sommets tombent hors du couloir (ex. elle deborde
    du trajet des deux cotes) serait sinon manquee."""
    yield coords[0]
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        n = max(1, math.ceil(distance_haversine_m((lat1, lon1), (lat2, lon2)) / PAS_ECHANTILLON_M))
        for k in range(1, n + 1):
            yield lon1 + (lon2 - lon1) * k / n, lat1 + (lat2 - lat1) * k / n
