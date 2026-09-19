"""Petits utilitaires geometriques partages entre plusieurs clients de
routage (pas de dependance GEOS/GIS ici -- calculs sur de simples tuples
(lat, lon), utilisables sans instance de modele)."""

import math


def distance_haversine_m(depart, arrivee):
    lat1, lon1 = map(math.radians, depart)
    lat2, lon2 = map(math.radians, arrivee)
    d_lat = lat2 - lat1
    d_lon = lon2 - lon1
    a = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))
