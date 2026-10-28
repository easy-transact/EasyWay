"""
Risque des itineraires : choisir la route la plus SURE, pas seulement la plus
rapide. retards_signalements.py traduit un signalement en temps perdu (un
accident coute 1-2 min) : ca ne fait presque jamais changer de route, et
police/nid-de-poule/danger n'y coutent rien. Ici, chaque danger sur la route
(dans le bon sens) vaut des POINTS de risque :

    points = gravite(type, sous-type) x confiance x facteur vehicule x facteur nuit

et le choix se fait sur  cout = duree + risque x SECONDES_PAR_POINT,
jamais au-dela de ECART_MAX (une route "sure" 40 min plus longue n'est pas
une proposition). Plusieurs signalements au meme endroit = souvent le meme
danger : dans un groupe de GROUPE_M, le plus grave compte entierement, les
autres a PART_DOUBLON. La duree affichee reste la duree reelle.

Valeurs de depart, a caler avec les acceptations reelles (better-route).
"""

from datetime import timedelta

from django.contrib.gis.geos import LineString, Point
from django.utils import timezone

from community.models import Incident, SousTypeIncident as S, TypeIncident as T

from ..models import FUSEAU_TRAFIC
from ..polyline import decoder_polyline6
from . import retards_signalements as rs
from .geo import distance_haversine_m

# Gravite 0-10 par (type, sous-type). Police/radar : 0, on ne fait pas faire
# de detour pour eviter un controle.
GRAVITE = {
    (T.ACCIDENT, S.CARAMBOLAGE): 10,
    (T.ACCIDENT, S.ACCIDENT): 8,
    (T.ACCIDENT, S.SENS_INVERSE): 8,
    (T.POLICE, S.SENS_INVERSE): 8,
    (T.VOIE_BLOQUEE, S.ROUTE_BARREE): 9,
    (T.ROUTE_BARREE, S.VOIE_BLOQUEE): 9,
    (T.MAUVAIS_TEMPS, S.INONDATION): 9,
    (T.DANGER, S.OBJET): 6,
    (T.MAUVAIS_TEMPS, S.ROUTE_GLISSANTE): 5,
    (T.DANGER, S.VEHICULE_ARRETE): 4,
    (T.DANGER, S.TRAVAUX): 4,
    (T.MAUVAIS_TEMPS, S.BROUILLARD): 4,
    (T.MAUVAIS_TEMPS, S.MAUVAIS_TEMPS): 3,
    (T.DANGER, S.FEU_TRICOLORE_HORS_SERVICE): 3,
    (T.DANGER, S.DANGER): 3,
    (T.DANGER, S.NID_DE_POULE): 2,
    (T.VOIE_BLOQUEE, S.UNE_VOIE_REDUITE): 2,
    (T.ROUTE_BARREE, S.VOIE_DE_GAUCHE): 2,
    (T.ROUTE_BARREE, S.VOIE_DE_DROITE): 2,
    (T.ROUTE_BARREE, S.VOIE_DU_MILIEU): 2,
    (T.EMBOUTEILLAGE, S.EMBOUTEILLAGE): 1,
    (T.EMBOUTEILLAGE, S.IMPORTANT): 1,
    (T.EMBOUTEILLAGE, S.A_L_ARRET): 1,
}
GRAVITE_SANS_SOUS_TYPE = {
    T.VOIE_BLOQUEE: 9, T.ROUTE_BARREE: 9, T.ACCIDENT: 8, T.DANGER: 3,
    T.MAUVAIS_TEMPS: 3, T.EMBOUTEILLAGE: 1,
}
# Une moto tombe la ou une voiture ralentit.
FACTEUR_MOTO = {S.ROUTE_GLISSANTE: 1.5, S.NID_DE_POULE: 1.5, S.INONDATION: 1.5, S.OBJET: 1.5}
FACTEUR_MOTO_ETAT_ROUTE = 1.5
FACTEUR_NUIT = 1.5            # 19 h - 6 h, heure de Douala
# Etat des routes : points par km.
POINTS_KM = {'bad_m': 1.0, 'degraded_m': 0.3}
POINTS_KM_INONDABLE_SOUS_LA_PLUIE = 2.0

GRAVITE_SERIEUSE = 6          # danger qui justifie de proposer un contournement
CONFIANCE_CONTOURNEMENT = 0.5  # ... s'il est assez sur (confirme ou valide)
DISTANCE_MIN_CONTOURNEMENT_M = 300  # plus pres : trop tard pour changer de route, on alerte
GROUPE_M = 200
PART_DOUBLON = 0.3

SECONDES_PAR_POINT = 60       # 1 point de risque = 1 min de trajet en plus acceptee
EGALITE_COUT_S = 120          # a moins de 2 points d'ecart, on garde la plus rapide
ECART_MIN_S = 300             # detour accepte : 25 % de la duree, entre 5 et 10 min
ECART_MAX_S = 600
PART_ECART = 0.25

FAIBLE, MOYEN, ELEVE = 'LOW', 'MEDIUM', 'HIGH'
SEUILS_NIVEAU = ((6, ELEVE), (2, MOYEN))


def gravite_de(incident):
    if not incident.sous_type:
        return GRAVITE_SANS_SOUS_TYPE.get(incident.type, 0)
    return GRAVITE.get((incident.type, incident.sous_type), 0)


def de_nuit(moment=None):
    heure = (moment or timezone.now()).astimezone(FUSEAU_TRAFIC).hour
    return heure >= 19 or heure < 6


def niveau(points):
    for seuil, nom in SEUILS_NIVEAU:
        if points >= seuil:
            return nom
    return FAIBLE


def duree_effective(candidat):
    return candidat.get('duree_avec_trafic') or candidat['duree']


def ecart_max_s(duree_reference):
    return min(max(PART_ECART * duree_reference, ECART_MIN_S), ECART_MAX_S)


def _dans_le_sens(incident, points, position):
    if incident.cap is None:
        return True
    cap_trace = rs._cap_du_trace(points, position)
    if cap_trace is None:
        return True
    return abs((incident.cap - cap_trace + 180) % 360 - 180) <= rs.ECART_CAP_MAX


def incidents_autour(candidats):
    coords = [(lat, lon) for c in candidats for lon, lat in decoder_polyline6(c['geometrie'])[::20]]
    if not coords:
        return []
    return list(rs._incidents_actifs(rs._rectangle(coords)))


def dangers_sur(points, distance_totale_m, duree_s, incidents, costing, nuit):
    """Dangers (dicts) sur la ligne `points` ((lon, lat)), dans l'ordre du
    trajet, groupes : `points` deja reduits pour les doublons du groupe.
    Un signalement qui aura expire avant que le conducteur y arrive ne compte pas."""
    if len(points) < 2:
        return []
    ligne = LineString(points, srid=4326)
    vitesse = max(distance_totale_m / max(duree_s, 1), 1.0)
    maintenant = timezone.now()
    moto = costing == 'motorcycle'
    trouves = []
    for incident in incidents:
        gravite = gravite_de(incident)
        if gravite <= 0:
            continue
        position = Point(incident.position.x, incident.position.y, srid=4326)
        if ligne.distance(position) * rs.METRES_PAR_DEGRE > rs.COULOIR_M:
            continue
        if not _dans_le_sens(incident, points, position):
            continue
        distance_m = ligne.project_normalized(position) * distance_totale_m
        if incident.expire_le <= maintenant + timedelta(seconds=distance_m / vitesse):
            continue
        fiabilite = rs.confiance(incident)
        valeur = gravite * fiabilite
        if moto:
            valeur *= FACTEUR_MOTO.get(incident.sous_type, 1.0)
        if nuit:
            valeur *= FACTEUR_NUIT
        if valeur <= 0:
            continue
        trouves.append({
            'id': str(incident.id), 'type': incident.type, 'subtype': incident.sous_type or '',
            'distance_from_start_m': round(distance_m), 'severity': gravite,
            'confidence': round(fiabilite, 2), 'points': valeur,
        })
    trouves.sort(key=lambda d: d['distance_from_start_m'])

    # Groupes : un danger a moins de GROUPE_M du precedent en fait partie.
    groupes, courant = [], []
    for danger in trouves:
        if courant and danger['distance_from_start_m'] - courant[-1]['distance_from_start_m'] > GROUPE_M:
            groupes.append(courant)
            courant = []
        courant.append(danger)
    if courant:
        groupes.append(courant)
    for groupe in groupes:
        for rang, danger in enumerate(sorted(groupe, key=lambda d: -d['points'])):
            danger['points'] = round(danger['points'] * (1 if rang == 0 else PART_DOUBLON), 2)
    return trouves


def _points_etat_route(candidat, costing, pluie):
    etat = candidat.get('etat_route')
    if not etat:
        return 0.0
    points = sum(etat.get(cle, 0) / 1000 * valeur for cle, valeur in POINTS_KM.items())
    if pluie:
        points += etat.get('flood_prone_m', 0) / 1000 * POINTS_KM_INONDABLE_SOUS_LA_PLUIE
    return points * (FACTEUR_MOTO_ETAT_ROUTE if costing == 'motorcycle' else 1.0)


def evaluer(candidats, costing, incidents=None):
    """Ajoute a chaque candidat : risque (points), niveau_risque, risques
    (dangers sur la route). Recalcule a chaque appel, jamais mis en cache."""
    if incidents is None:
        incidents = incidents_autour(candidats)
    nuit = de_nuit()
    # Pluie signalee dans la zone : les troncons inondables deviennent un risque.
    pluie = any(i.type == T.MAUVAIS_TEMPS for i in incidents)
    for candidat in candidats:
        if candidat.get('degrade'):
            dangers = []
        else:
            dangers = dangers_sur(
                decoder_polyline6(candidat['geometrie']), candidat['distance'],
                duree_effective(candidat), incidents, costing, nuit,
            )
        total = sum(d['points'] for d in dangers) + _points_etat_route(candidat, costing, pluie)
        candidat['risque'] = round(total, 1)
        candidat['niveau_risque'] = niveau(total)
        candidat['risques'] = dangers
    return candidats


def cout(candidat, penalite_s=0):
    return duree_effective(candidat) + candidat.get('risque', 0) * SECONDES_PAR_POINT + penalite_s


def classer(candidats, penalite=None):
    """Classe par cout (duree + risque, + penalite(candidat) si fournie, cf.
    etat_routes evitement). Une route plus longue que la plus rapide de plus
    de ecart_max_s n'est jamais recommandee ; a moins de EGALITE_COUT_S de
    cout, la plus rapide reste devant. Pose raison_choix sur la recommandee."""
    if not candidats:
        return candidats
    penalites = {id(c): (penalite(c) if penalite else 0) for c in candidats}
    rapide = min(candidats, key=duree_effective)
    limite = duree_effective(rapide) + ecart_max_s(duree_effective(rapide))

    def cle(c):
        hors_limite = duree_effective(c) > limite
        return (hors_limite, duree_effective(c) if hors_limite else cout(c, penalites[id(c)]))

    candidats.sort(key=cle)
    choisi = candidats[0]
    if choisi is not rapide and cout(rapide, penalites[id(rapide)]) - cout(choisi, penalites[id(choisi)]) < EGALITE_COUT_S:
        candidats.remove(rapide)
        candidats.insert(0, rapide)
        choisi = rapide

    for index, c in enumerate(candidats):
        c['est_recommande'] = index == 0
        c['libelle'] = 'Itineraire recommande' if index == 0 else f'Itineraire {index + 1}'
        c['raison_choix'] = None
    if choisi is rapide:
        choisi['raison_choix'] = {'code': 'FASTEST', 'extra_time_s': 0, 'avoided': []}
    else:
        sur_choisi = {d['id'] for d in choisi.get('risques', [])}
        evites = [
            {'type': d['type'], 'subtype': d['subtype'], 'severity': d['severity']}
            for d in rapide.get('risques', []) if d['id'] not in sur_choisi
        ]
        choisi['raison_choix'] = {
            'code': 'SAFER' if choisi.get('risque', 0) < rapide.get('risque', 0) else 'BETTER_ROAD',
            'extra_time_s': duree_effective(choisi) - duree_effective(rapide),
            'avoided': evites,
        }
    return candidats


def a_contourner(candidat, extremites):
    """Positions (lat, lon) des dangers serieux et assez surs de la route,
    hors des 300 premiers metres et loin des extremites/etapes : ceux pour
    lesquels on demande a Valhalla une route qui les evite."""
    ids = {d['id'] for d in candidat.get('risques', [])
           if d['severity'] >= GRAVITE_SERIEUSE and d['confidence'] >= CONFIANCE_CONTOURNEMENT
           and d['distance_from_start_m'] >= DISTANCE_MIN_CONTOURNEMENT_M}
    if not ids:
        return []
    positions = []
    for incident in Incident.objects.filter(id__in=ids):
        lat, lon = incident.position.y, incident.position.x
        if all(distance_haversine_m((lat, lon), e) >= rs.MARGE_EXTREMITES_M for e in extremites):
            positions.append((lat, lon))
    return positions


def fermetures_devant(points, costing):
    """Fermetures confirmees (route barree, inondation) sur la ligne `points`
    ((lon, lat)), dans le bon sens : le reste de la route suivie est coupe."""
    if len(points) < 2:
        return []
    ligne = LineString(points, srid=4326)
    rectangle = rs._rectangle([(lat, lon) for lon, lat in points[::20] + [points[-1]]])
    fermetures = []
    for incident in rs._incidents_actifs(rectangle):
        effet = rs.effet_de(incident)
        if effet is None or not rs.fermeture_confirmee(incident, effet, costing):
            continue
        position = Point(incident.position.x, incident.position.y, srid=4326)
        if ligne.distance(position) * rs.METRES_PAR_DEGRE > rs.COULOIR_M or not _dans_le_sens(incident, points, position):
            continue
        fermetures.append({
            'id': str(incident.id), 'type': incident.type, 'subtype': incident.sous_type or '',
            'distance_from_start_m': round(ligne.project(position) * rs.METRES_PAR_DEGRE),
            'severity': gravite_de(incident), 'confidence': round(rs.confiance(incident), 2),
            'points': gravite_de(incident),
        })
    return sorted(fermetures, key=lambda d: d['distance_from_start_m'])
