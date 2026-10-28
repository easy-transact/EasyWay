"""
Retard des signalements sur les itineraires (spec ITINERAIRES_VOITURE_MOTO,
etape 1) : chaque signalement actif situe sur un itineraire, dans le bon
sens, ajoute un retard a sa duree ; les fermetures confirmees sont exclues
du calcul ; les candidats sont ensuite reclasses par duree finale.

Principe : un signalement = une vitesse imposee sur une longueur de route
en amont du point. retard = longueur x (1/vitesse imposee - 1/vitesse normale),
la vitesse normale etant approchee par la vitesse moyenne de l'itineraire
(pas de vitesse par troncon disponible ici).

Toujours decider sur la PAIRE (type, sous-type) : le type ROUTE_BARREE
propose des voies bloquees, et la vraie route barree est un sous-type de
VOIE_BLOQUEE ; SENS_INVERSE existe sous POLICE et sous ACCIDENT.
"""

import math
from dataclasses import dataclass

from django.contrib.gis.geos import LineString, Point, Polygon

from django.utils import timezone

from community.models import Incident, SousTypeIncident as S, StatutIncident, TypeIncident as T

from ..polyline import decoder_polyline6
from .geo import distance_haversine_m

# Valeurs de depart de la spec (section 4), a caler avec les donnees.
VITESSE = 'vitesse'      # vitesse imposee (km/h) sur `longueur_m`
FACTEUR = 'facteur'      # vitesse normale x facteur sur `longueur_m`
FERMETURE = 'fermeture'  # route fermee si confirmee, sinon `vitesse` sur `longueur_m`


@dataclass(frozen=True)
class Effet:
    mecanisme: str
    valeur: float          # km/h (VITESSE, FERMETURE non confirmee) ou facteur (FACTEUR)
    longueur_m: int
    part_moto: float       # part du retard voiture subie par une moto
    fermeture_moto_immediate: bool = False


EFFETS = {
    (T.EMBOUTEILLAGE, S.EMBOUTEILLAGE): Effet(VITESSE, 25, 300, 0.4),
    (T.EMBOUTEILLAGE, S.IMPORTANT): Effet(VITESSE, 15, 500, 0.4),
    (T.EMBOUTEILLAGE, S.A_L_ARRET): Effet(VITESSE, 5, 500, 0.4),
    (T.ACCIDENT, S.ACCIDENT): Effet(VITESSE, 10, 300, 0.5),
    (T.ACCIDENT, S.CARAMBOLAGE): Effet(VITESSE, 5, 500, 0.5),
    (T.ACCIDENT, S.SENS_INVERSE): Effet(VITESSE, 20, 200, 0.5),
    (T.ROUTE_BARREE, S.VOIE_DE_GAUCHE): Effet(VITESSE, 15, 200, 0.3),
    (T.ROUTE_BARREE, S.VOIE_DE_DROITE): Effet(VITESSE, 15, 200, 0.3),
    (T.ROUTE_BARREE, S.VOIE_DU_MILIEU): Effet(VITESSE, 15, 200, 0.3),
    (T.ROUTE_BARREE, S.VOIE_BLOQUEE): Effet(FERMETURE, 10, 300, 1.0),
    (T.VOIE_BLOQUEE, S.UNE_VOIE_REDUITE): Effet(VITESSE, 15, 300, 0.4),
    (T.VOIE_BLOQUEE, S.ROUTE_BARREE): Effet(FERMETURE, 10, 300, 1.0),
    (T.DANGER, S.VEHICULE_ARRETE): Effet(VITESSE, 20, 100, 0.5),
    (T.DANGER, S.OBJET): Effet(VITESSE, 20, 100, 0.5),
    (T.DANGER, S.TRAVAUX): Effet(VITESSE, 20, 200, 0.5),
    (T.DANGER, S.FEU_TRICOLORE_HORS_SERVICE): Effet(VITESSE, 10, 100, 0.5),
    (T.MAUVAIS_TEMPS, S.MAUVAIS_TEMPS): Effet(FACTEUR, 0.7, 1000, 1.0),
    (T.MAUVAIS_TEMPS, S.ROUTE_GLISSANTE): Effet(FACTEUR, 0.7, 1000, 1.0),
    (T.MAUVAIS_TEMPS, S.BROUILLARD): Effet(FACTEUR, 0.7, 1000, 1.0),
    (T.MAUVAIS_TEMPS, S.INONDATION): Effet(FERMETURE, 5, 300, 1.0, fermeture_moto_immediate=True),
    # Volontairement absents (aucun effet) : POLICE et RADAR (on ne fait pas
    # faire de detour pour eviter un controle), DANGER/DANGER, DANGER/NID_DE_POULE
    # (releve de l'etat des routes).
}

COULOIR_M = 30             # signalement a moins de 30 m de la ligne du trajet
ECART_CAP_MAX = 45         # degres entre le cap du signalement et le sens du trajet
MARGE_EXTREMITES_M = 100   # jamais de fermeture pres du depart ou de l'arrivee
MARGE_RECHERCHE_DEG = 0.045  # ~5 km autour du rectangle depart-arrivee
EGALITE_S = 60             # a moins d'1 min d'ecart, departage par nb de signalements puis distance
SEUIL_CONFIANCE = 3        # cf. community.models.SEUIL_CONFIANCE_VALIDATION
METRES_PAR_DEGRE = 111_320


def confiance(incident):
    """Probabilite (0-1) qu'un signalement soit encore la : confiance des
    votes x usure dans le temps. score_confiance est une SOMME de poids de
    votes (3 = valide), pas une probabilite : ramenee a [0, 1] en comptant
    l'auteur pour 1 (un signalement tout neuf vaut 1/4, valide il vaut 1)."""
    votes = min(max((1 + float(incident.score_confiance)) / (1 + SEUIL_CONFIANCE), 0.0), 1.0)
    if incident.valide_par_staff:
        votes = 1.0  # valide par la moderation : plus de doute sur sa presence
    maintenant = timezone.now()
    if incident.expire_le <= maintenant:
        return 0.0
    vie_totale = (incident.expire_le - incident.cree_le).total_seconds()
    ecoulee = (maintenant - incident.cree_le).total_seconds()
    if vie_totale <= 0 or ecoulee <= vie_totale / 2:
        usure = 1.0
    else:
        usure = max(0.0, (vie_totale - ecoulee) / (vie_totale / 2))
    return votes * usure


def fermeture_confirmee(incident, effet, costing):
    """Deux auteurs distincts (un doublon corrobore = une confirmation) ou un
    auteur et une confirmation, ou une validation par la moderation (back-office).
    Une inondation ferme la route d'une moto des le premier signalement."""
    if effet.mecanisme != FERMETURE:
        return False
    if effet.fermeture_moto_immediate and costing == 'motorcycle':
        return True
    return incident.confirmations >= 1 or incident.valide_par_staff


def _incidents_actifs(rectangle):
    return Incident.objects.filter(
        statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE],
        expire_le__gt=timezone.now(),
        position__intersects=rectangle,
    )


def _rectangle(points_latlon):
    lats = [p[0] for p in points_latlon]
    lons = [p[1] for p in points_latlon]
    rectangle = Polygon.from_bbox((
        min(lons) - MARGE_RECHERCHE_DEG, min(lats) - MARGE_RECHERCHE_DEG,
        max(lons) + MARGE_RECHERCHE_DEG, max(lats) + MARGE_RECHERCHE_DEG,
    ))
    rectangle.srid = 4326
    return rectangle


def fermetures_a_eviter(depart, arrivee, etapes, costing):
    """Positions (lat, lon) des fermetures confirmees a exclure du calcul
    (exclude_locations), jamais a moins de 100 m du depart, de l'arrivee ou
    d'une etape (le calcul deviendrait impossible)."""
    extremites = [depart, arrivee, *(etapes or [])]
    exclusions = []
    for incident in _incidents_actifs(_rectangle(extremites)):
        effet = EFFETS.get((incident.type, incident.sous_type))
        if effet is None or not fermeture_confirmee(incident, effet, costing):
            continue
        lat, lon = incident.position.y, incident.position.x
        if any(distance_haversine_m((lat, lon), e) < MARGE_EXTREMITES_M for e in extremites):
            continue
        exclusions.append((lat, lon))
    return exclusions


def _cap(lon1, lat1, lon2, lat2):
    d_lon = math.radians(lon2 - lon1)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    x = math.sin(d_lon) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(d_lon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _cap_du_trace(points, position):
    """Sens du trajet au droit de la position : cap du segment le plus proche."""
    meilleur, distance = None, float('inf')
    for (lon1, lat1), (lon2, lat2) in zip(points, points[1:]):
        segment = LineString([(lon1, lat1), (lon2, lat2)], srid=4326)
        d = segment.distance(position)
        if d < distance:
            meilleur, distance = (lon1, lat1, lon2, lat2), d
    return _cap(*meilleur) if meilleur else None


def _retard_s(effet, vitesse_normale_ms):
    longueur = effet.longueur_m
    if effet.mecanisme == FACTEUR:
        return longueur / vitesse_normale_ms * (1 / effet.valeur - 1)
    vitesse_imposee = effet.valeur / 3.6
    if vitesse_imposee >= vitesse_normale_ms:
        return 0.0
    return longueur * (1 / vitesse_imposee - 1 / vitesse_normale_ms)


def appliquer(candidats, costing, trafic_en_direct):
    """Ajoute a chaque candidat (forme ServiceItineraire._normaliser_trip) :
    delai_signalements (s), signalements_appliques, et ajuste
    duree_avec_trafic = duree (ou duree trafic) + delai. Puis reclasse et
    marque est_recommande sur le premier.

    trafic_en_direct : les embouteillages ACTIFS sont deja ecrits en vitesses
    dans traffic.tar (cf. trafic_temps_reel.py) -- ne pas les recompter."""
    if not candidats:
        return candidats
    toutes_coords = [(lat, lon) for c in candidats for lon, lat in decoder_polyline6(c['geometrie'])[::20]]
    incidents = [
        i for i in _incidents_actifs(_rectangle(toutes_coords))
        if (i.type, i.sous_type) in EFFETS
    ]

    for candidat in candidats:
        candidat['delai_signalements'] = 0
        candidat['signalements_appliques'] = []
        if candidat.get('degrade') or not incidents:
            candidat.setdefault('duree_avec_trafic', None)
            continue
        points = decoder_polyline6(candidat['geometrie'])
        if len(points) < 2:
            continue
        ligne = LineString(points, srid=4326)
        vitesse_normale = max(candidat['distance'] / max(candidat['duree'], 1), 2.0)  # m/s
        total = 0.0
        for incident in incidents:
            effet = EFFETS[(incident.type, incident.sous_type)]
            position = Point(incident.position.x, incident.position.y, srid=4326)
            if ligne.distance(position) * METRES_PAR_DEGRE > COULOIR_M:
                continue
            if incident.cap is not None:
                cap_trace = _cap_du_trace(points, position)
                ecart = abs((incident.cap - cap_trace + 180) % 360 - 180) if cap_trace is not None else 0
                if ecart > ECART_CAP_MAX:
                    continue  # signale dans l'autre sens de circulation
            if fermeture_confirmee(incident, effet, costing):
                continue  # deja exclue du calcul (fermetures_a_eviter)
            moto = costing == 'motorcycle'
            if trafic_en_direct and incident.type == T.EMBOUTEILLAGE and incident.statut == StatutIncident.ACTIF:
                continue  # deja dans traffic.tar
            retard = _retard_s(effet, vitesse_normale) * confiance(incident) * (effet.part_moto if moto else 1.0)
            if retard < 1:
                continue
            total += retard
            candidat['signalements_appliques'].append({
                'id': str(incident.id), 'type': incident.type, 'subtype': incident.sous_type or '',
                'delay': round(retard), 'applied_as': 'delay',
            })
        candidat['delai_signalements'] = round(total)
        base = candidat['duree_avec_trafic'] or candidat['duree']
        candidat['duree_avec_trafic'] = base + candidat['delai_signalements'] if total else candidat['duree_avec_trafic']

    def cle(c):
        return c['duree_avec_trafic'] or c['duree']

    candidats.sort(key=cle)
    # Egalite a moins d'une minute : le moins de signalements, puis le plus court.
    tete = [c for c in candidats if cle(c) - cle(candidats[0]) < EGALITE_S]
    tete.sort(key=lambda c: (len(c['signalements_appliques']), c['distance']))
    candidats[:len(tete)] = tete
    for index, c in enumerate(candidats):
        c['est_recommande'] = index == 0
        c['libelle'] = 'Itineraire recommande' if index == 0 else f'Itineraire {index + 1}'
    return candidats
