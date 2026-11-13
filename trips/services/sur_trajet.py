"""
"Ce signalement est-il sur ce trace, dans ce sens, et ou ?" -- UNE seule
regle, partagee par tout ce qui pose la question :

  - le calcul d'itineraire (retards_signalements.appliquer, risque_itineraire),
  - les fermetures devant le conducteur (risque_itineraire.fermetures_devant),
  - l'affichage le long d'un trajet (community.services.incidents_actifs_par_topologie,
    repli couloir de community.views.IncidentsSurTrajetView),
  - les alertes en direct (alertes_itineraire).

Avant, chacun avait son couloir et son test de sens : un meme signalement
pouvait ralentir une route sans y etre affiche, ou l'inverse.

Regle : a moins de COULOIR_M de la ligne du trace, et -- si le signalement a
un cap -- a moins de ECART_CAP_MAX degres du sens du trace au droit du point
(sinon il est sur la chaussee d'en face). Sans cap : les deux sens.

Les distances sont approchees en degres x METRES_PAR_DEGRE (latitude du
Cameroun : ecart < 0,5 % sur la longitude, negligeable a l'echelle du couloir).
"""

import math

from django.contrib.gis.geos import LineString, Point

COULOIR_M = 30
ECART_CAP_MAX = 45
METRES_PAR_DEGRE = 111_320


def cap_segment(lon1, lat1, lon2, lat2):
    """Cap (0-360, degres depuis le nord) du segment 1 -> 2."""
    d_lon = math.radians(lon2 - lon1)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    x = math.sin(d_lon) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(d_lon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def ecart_caps(cap1, cap2):
    return abs((cap1 - cap2 + 180) % 360 - 180)


class Trace:
    """Trace (liste de (lon, lat), dans l'ordre du trajet)."""

    def __init__(self, points):
        self.points = list(points)
        self.ligne = LineString(self.points, srid=4326) if len(self.points) >= 2 else None

    @staticmethod
    def _point(position):
        if isinstance(position, Point):
            return position
        lat, lon = position
        return Point(lon, lat, srid=4326)

    def ecart_lateral_m(self, position) -> float:
        if self.ligne is None:
            return float('inf')
        return self.ligne.distance(self._point(position)) * METRES_PAR_DEGRE

    def distance_depuis_depart_m(self, position) -> float:
        """Abscisse du point projete sur le trace, depuis son debut."""
        if self.ligne is None:
            return 0.0
        return self.ligne.project(self._point(position)) * METRES_PAR_DEGRE

    def cap_au_droit(self, position):
        """Sens du trace au droit de la position : cap du segment le plus proche."""
        point = self._point(position)
        meilleur, distance = None, float('inf')
        for (lon1, lat1), (lon2, lat2) in zip(self.points, self.points[1:]):
            d = LineString([(lon1, lat1), (lon2, lat2)], srid=4326).distance(point)
            if d < distance:
                meilleur, distance = (lon1, lat1, lon2, lat2), d
        return cap_segment(*meilleur) if meilleur else None

    def dans_le_sens(self, cap, position) -> bool:
        if cap is None:
            return True
        cap_trace = self.cap_au_droit(position)
        return cap_trace is None or ecart_caps(cap, cap_trace) <= ECART_CAP_MAX

    def contient(self, position, cap=None, couloir_m=COULOIR_M) -> bool:
        """Sur le trace (couloir) et dans son sens (si cap)."""
        return self.ecart_lateral_m(position) <= couloir_m and self.dans_le_sens(cap, position)

    def contient_incident(self, incident, couloir_m=COULOIR_M) -> bool:
        return self.contient(incident.position, incident.cap, couloir_m)
