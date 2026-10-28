"""
Correction des durees en ville. Les vitesses par defaut de Valhalla sont
celles d'un reseau fluide : Akwa Palace -> Total Bonateki (3,2 km) annonce
3 min, soit 63 km/h de moyenne en plein Douala (verifie le 28/10). Le trafic
mesure (service_trafic) ne corrige que les tronçons ou assez de conducteurs
EasyWay roulent -- rare pour l'instant.

En attendant des profils de vitesse construits sur l'historique, on applique
un facteur aux portions situees dans une agglomeration, selon l'heure (heure
de Douala). Les parties deja mesurees par le trafic ne sont pas corrigees
une seconde fois (cf. service_trafic : duree_mesuree_s / duree_valhalla_s).

Valeurs de depart, a caler sur les trajets reels : PrevisionItineraire garde
la duree annoncee et le trajet lance, comparable a sa duree_reelle.
"""

from ..models import FUSEAU_TRAFIC
from ..polyline import decoder_polyline6
from .geo import distance_haversine_m

# (nom, lat, lon, rayon en km) : centre et etendue de l'agglomeration.
AGGLOMERATIONS = [
    ('Douala', 4.0500, 9.7200, 13),
    ('Yaounde', 3.8667, 11.5167, 13),
]

FACTEUR_POINTE = 3.0    # jours ouvres, 6 h - 9 h et 16 h - 20 h
FACTEUR_JOURNEE = 2.0   # le reste de la journee
FACTEUR_NUIT = 1.4      # 21 h - 6 h


def facteur(moment):
    """Facteur multiplicatif des durees Valhalla en agglomeration, a `moment` (aware)."""
    local = moment.astimezone(FUSEAU_TRAFIC)
    heure = local.hour
    if heure >= 21 or heure < 6:
        return FACTEUR_NUIT
    if local.weekday() < 5 and (6 <= heure < 9 or 16 <= heure < 20):
        return FACTEUR_POINTE
    return FACTEUR_JOURNEE


def en_agglomeration(lat, lon):
    return any(distance_haversine_m((lat, lon), (c_lat, c_lon)) <= rayon * 1000 for _, c_lat, c_lon, rayon in AGGLOMERATIONS)


def _points_des_manoeuvres(geometrie, distances):
    """(lat, lon) du debut de chaque manoeuvre, par distance cumulee le long du trace."""
    points = [(lat, lon) for lon, lat in decoder_polyline6(geometrie)]
    if not points:
        return [None] * len(distances)
    cumul = [0.0]
    for a, b in zip(points, points[1:]):
        cumul.append(cumul[-1] + distance_haversine_m(a, b))
    resultat, i, parcouru = [], 0, 0.0
    for distance in distances:
        while i < len(cumul) - 1 and cumul[i] < parcouru:
            i += 1
        resultat.append(points[i])
        parcouru += distance
    return resultat


def corriger(candidat, trafic, moment=None):
    """Corrige sur place duree, duree_avec_trafic et la duree des manoeuvres
    d'un candidat (forme ServiceItineraire._normaliser_trip)."""
    from django.conf import settings
    from django.utils import timezone

    manoeuvres = candidat.get('manoeuvres') or []
    if not settings.CORRECTION_DUREE_VILLE or candidat.get('degrade') or not manoeuvres:
        return candidat
    f = facteur(moment or timezone.now())
    points = _points_des_manoeuvres(candidat['geometrie'], [m['distance'] for m in manoeuvres])
    brute = corrigee = 0
    for manoeuvre, point in zip(manoeuvres, points):
        brute += manoeuvre['duree']
        if point is not None and en_agglomeration(*point):
            manoeuvre['duree'] = round(manoeuvre['duree'] * f)
        corrigee += manoeuvre['duree']
    if not brute or corrigee == brute:
        return candidat
    ratio = corrigee / brute
    candidat['duree'] = round(candidat['duree'] * ratio)
    if candidat.get('duree_avec_trafic') is not None:
        mesuree, valhalla = trafic.get('duree_mesuree_s'), trafic.get('duree_valhalla_s')
        if mesuree is not None and valhalla is not None:
            # Seule la partie non mesuree (vitesse Valhalla) est corrigee.
            candidat['duree_avec_trafic'] = round(mesuree + valhalla * ratio)
        else:
            candidat['duree_avec_trafic'] = round(candidat['duree_avec_trafic'] * ratio)
    return candidat

