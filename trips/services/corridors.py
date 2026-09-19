"""Detection de correspondance entre un trajet demande et un CorridorReference
connu (cf. ClientCorridorReference qui consomme le resultat). Verification en
Python (haversine) plutot qu'une requete GIS : la table est forcement petite
(une poignee de corridors bien connus, pas une donnee de masse), pas la peine
d'un index spatial pour ca."""

from ..models import CorridorReference
from .geo import distance_haversine_m


def trouver_corridor(depart, arrivee):
    """depart/arrivee : (lat, lon). Retourne le CorridorReference actif dont
    les deux ancrages sont chacun a moins de rayon_ancrage_m de depart/arrivee
    (dans un sens ou l'autre -- le corridor est bidirectionnel), ou None si
    aucun ne correspond."""
    for corridor in CorridorReference.objects.filter(actif=True):
        point_depart_corridor = (corridor.ancrage_depart.y, corridor.ancrage_depart.x)
        point_arrivee_corridor = (corridor.ancrage_arrivee.y, corridor.ancrage_arrivee.x)

        # Sens aller : depart pres de l'ancrage de depart, arrivee pres de l'ancrage d'arrivee.
        if (
            distance_haversine_m(depart, point_depart_corridor) <= corridor.rayon_ancrage_m
            and distance_haversine_m(arrivee, point_arrivee_corridor) <= corridor.rayon_ancrage_m
        ):
            return corridor

        # Sens retour : l'inverse.
        if (
            distance_haversine_m(depart, point_arrivee_corridor) <= corridor.rayon_ancrage_m
            and distance_haversine_m(arrivee, point_depart_corridor) <= corridor.rayon_ancrage_m
        ):
            return corridor

    return None
