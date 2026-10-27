"""
Du recalage Valhalla (trace_attributes : edges + matched_points) aux tranches
de route : creation des Troncon, enregistrement des passages (vitesses) et
rattachement des signalements.
"""

import hashlib
import logging
import math
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

import h3
from django.conf import settings
from django.contrib.gis.geos import LineString, Polygon
from django.db.models import Q
from django.utils import timezone

from ..models import TAILLE_TRONCON_M, PassageTroncon, SignalementTroncon, Troncon

logger = logging.getLogger(__name__)

# Attributs demandes a trace_attributes (cf. trips/services/client_meili.py).
ATTRIBUTS_TRONCON = [
    'edge.id', 'edge.length', 'edge.speed', 'edge.way_id', 'edge.road_class', 'edge.names',
    'edge.roundabout', 'edge.toll', 'node.traffic_signal', 'node.type',
    'matched.point', 'matched.type', 'matched.edge_index', 'matched.distance_along_edge',
]

VITESSE_MAX_PLAUSIBLE_KMH = 180
MARGE_DEPART_ARRIVEE_M = 200  # depart, stationnement : on y roule au pas quelle que soit la route
RAYON_BOUCHON_M = 150         # passage pres d'un embouteillage/accident/route barree actif : ecarte
TYPES_BOUCHON = ('EMBOUTEILLAGE', 'ACCIDENT', 'ROUTE_BARREE')
SOUS_TYPES_BOUCHON = ('ROUTE_BARREE',)  # VOIE_BLOQUEE/ROUTE_BARREE


def identifiant(arete, index):
    return f'{arete}:{index}'


def _index(offset_m):
    return max(int(offset_m // TAILLE_TRONCON_M), 0)


def classe_attendue(classe_route):
    """road_class Valhalla -> URBAIN / NATIONALE / AUTOROUTE (vitesse attendue)."""
    if classe_route == 'motorway':
        return 'AUTOROUTE'
    if classe_route == 'trunk':
        return 'NATIONALE'
    return 'URBAIN'


def _ralentissement_normal(arete):
    noeud = arete.get('end_node') or {}
    return bool(arete.get('roundabout') or arete.get('toll') or noeud.get('traffic_signal') or noeud.get('type') == 'toll_booth')


def empreinte_conducteur(utilisateur_id):
    """Compte les conducteurs distincts sans jamais stocker qui ils sont."""
    return hashlib.sha256(f'{settings.SECRET_KEY}:troncon:{utilisateur_id}'.encode()).hexdigest()[:16]


def assurer_troncon(arete, index, points):
    """points : [(offset_m, lat, lon), ...] observes dans la tranche `index`
    (au moins 1). Cree la tranche, ou etend sa geometrie si ces points la
    couvrent davantage."""
    tid = identifiant(arete['id'], index)
    points = sorted(points)
    debut, fin = points[0], points[-1]
    if debut[0] == fin[0]:
        fin = (fin[0] + 0.5, fin[1] + 0.000005, fin[2])  # point isole : geometrie minimale
    troncon = Troncon.objects.filter(id=tid).first()
    if troncon is None:
        milieu = ((debut[1] + fin[1]) / 2, (debut[2] + fin[2]) / 2)
        return Troncon.objects.create(
            id=tid, arete=arete['id'], index=index, way_id=arete.get('way_id'),
            classe_route=arete.get('road_class') or '', nom_voie=', '.join(arete.get('names') or [])[:255],
            geometrie=LineString([(debut[2], debut[1]), (fin[2], fin[1])], srid=4326),
            debut_m=debut[0], fin_m=fin[0],
            cellule_h3_res8=h3.str_to_int(h3.latlng_to_cell(milieu[0], milieu[1], 8)),
            ralentissement_normal=_ralentissement_normal(arete) and index == _index(arete['length'] * 1000 - 1),
        )
    coords = list(troncon.geometrie.coords)
    champs = []
    if debut[0] < troncon.debut_m:
        coords[0], troncon.debut_m = (debut[2], debut[1]), debut[0]
        champs.append('debut_m')
    if fin[0] > troncon.fin_m:
        coords[-1], troncon.fin_m = (fin[2], fin[1]), fin[0]
        champs.append('fin_m')
    if champs:
        troncon.geometrie = LineString(coords, srid=4326)
        troncon.save(update_fields=champs + ['geometrie'])
    return troncon


def _offset_m(arete, point):
    return (point.get('distance_along_edge') or 0) * (arete.get('length') or 0) * 1000


def _distance_m(a, b):
    """a, b : (lat, lon)."""
    dlat = (a[0] - b[0]) * 111_320
    dlon = (a[1] - b[1]) * 111_320 * math.cos(math.radians(a[0]))
    return math.hypot(dlat, dlon)


def _heure_creuse(horodatage, reglages):
    from trips.models import FUSEAU_TRAFIC

    heure = horodatage.astimezone(FUSEAU_TRAFIC).hour
    return heure >= reglages['heure_creuse_debut'] or heure < reglages['heure_creuse_fin']


def _bouchons(points_latlon, debut, fin):
    """Signalements de bouchon actifs pendant [debut, fin] pres du lot : [(lat, lon, cree, expire)]."""
    from community.models import Incident

    if not points_latlon:
        return []
    lats = [p[0] for p in points_latlon]
    lons = [p[1] for p in points_latlon]
    marge = 0.002
    zone = Polygon.from_bbox((min(lons) - marge, min(lats) - marge, max(lons) + marge, max(lats) + marge))
    zone.srid = 4326
    incidents = (
        Incident.objects.filter(position__intersects=zone, cree_le__lte=fin, expire_le__gte=debut)
        .filter(Q(type__in=TYPES_BOUCHON) | Q(sous_type__in=SOUS_TYPES_BOUCHON))
        .exclude(statut='RETIRE')
    )
    return [(i.position.y, i.position.x, i.cree_le, i.expire_le) for i in incidents]


def enregistrer_passages(trajet_id, edges, matched_points, paires, reglages):
    """paires : [(id_message, evenement)] tries par horodatage, alignes sur
    matched_points (cf. ConsommateurPositions). Un PassageTroncon par tranche
    parcourue (vitesse = distance / duree sur la tranche). Retourne le nombre
    de passages enregistres."""
    from trips.models import Trajet

    try:
        uuid.UUID(str(trajet_id))
    except ValueError:
        return 0
    trajet = Trajet.objects.filter(id=trajet_id).values(
        'utilisateur_id', 'position_origine', 'position_destination', 'type_vehicule',
    ).first()
    if trajet is None or not edges:
        return 0
    extremites = [
        (p.y, p.x) for p in (trajet['position_origine'], trajet['position_destination']) if p is not None
    ]
    horodatages = [datetime.fromisoformat(evt['horodatage']) for _, evt in paires]
    positions = [(evt['lat'], evt['lon']) for _, evt in paires]
    bouchons = _bouchons(positions, min(horodatages), max(horodatages))

    par_tranche = defaultdict(lambda: {'distance_m': 0.0, 'duree_s': 0.0, 'points': [], 'debut': None, 'arete': None})
    for i in range(len(matched_points) - 1):
        actuel, suivant = matched_points[i], matched_points[i + 1]
        index_arete = actuel.get('edge_index')
        if index_arete is None or suivant.get('edge_index') != index_arete:
            continue
        duree_s = (horodatages[i + 1] - horodatages[i]).total_seconds()
        arete = edges[index_arete]
        a, b = _offset_m(arete, actuel), _offset_m(arete, suivant)
        if duree_s <= 0 or b - a <= 0:
            continue
        if any(_distance_m(positions[i], e) < MARGE_DEPART_ARRIVEE_M for e in extremites):
            continue
        if any(
            _distance_m(positions[i], (lat, lon)) < RAYON_BOUCHON_M and cree <= horodatages[i] <= expire
            for lat, lon, cree, expire in bouchons
        ):
            continue
        milieu = (a + b) / 2
        cle = (arete['id'], _index(milieu))
        tranche = par_tranche[cle]
        tranche['distance_m'] += b - a
        tranche['duree_s'] += duree_s
        tranche['arete'] = arete
        tranche['debut'] = tranche['debut'] or horodatages[i]
        for point, offset in ((actuel, a), (suivant, b)):
            if 'lat' in point and _index(offset) == cle[1]:
                tranche['points'].append((offset, point['lat'], point['lon']))

    conducteur = empreinte_conducteur(trajet['utilisateur_id'])
    nombre = 0
    for (arete_id, index), tranche in par_tranche.items():
        vitesse = tranche['distance_m'] / tranche['duree_s'] * 3.6
        if vitesse > VITESSE_MAX_PLAUSIBLE_KMH or not tranche['points']:
            continue
        troncon = assurer_troncon(tranche['arete'], index, tranche['points'])
        PassageTroncon.objects.create(
            troncon=troncon, conducteur=conducteur, trajet_id=trajet_id,
            type_vehicule=trajet['type_vehicule'] or '', vitesse_kmh=round(vitesse, 1),
            horodatage=tranche['debut'], heure_creuse=_heure_creuse(tranche['debut'], reglages),
        )
        nombre += 1
    return nombre


def rattacher_signalement(incident):
    """Recale la position (et le cap) du signalement sur sa tranche via
    trace_attributes : deux points, le second 20 m plus loin dans le sens du
    cap. Enregistre SignalementTroncon (troncon vide si hors route)."""
    from trips.services.client_meili import ErreurMeili, tracer
    from trips.services.disjoncteur import DisjoncteurOuvert

    lat, lon = incident.position.y, incident.position.x
    cap = math.radians(incident.cap if incident.cap is not None else 0)
    suivant = (lat + 20 * math.cos(cap) / 111_320, lon + 20 * math.sin(cap) / (111_320 * math.cos(math.radians(lat))))
    try:
        edges, matched = tracer(
            [{'lat': lat, 'lon': lon, 'time': 0}, {'lat': suivant[0], 'lon': suivant[1], 'time': 2}],
            attributs=ATTRIBUTS_TRONCON,
        )
    except (ErreurMeili, DisjoncteurOuvert) as exc:
        logger.info('Rattachement du signalement %s reporte : %s', incident.id, exc)
        return None  # rien d'enregistre : retente a la prochaine passe
    troncon = None
    if matched and matched[0].get('edge_index') is not None and edges:
        arete = edges[matched[0]['edge_index']]
        offset = _offset_m(arete, matched[0])
        points = [
            (_offset_m(arete, p), p['lat'], p['lon']) for p in matched
            if p.get('edge_index') == matched[0]['edge_index'] and 'lat' in p
            and _index(_offset_m(arete, p)) == _index(offset)
        ] or [(offset, lat, lon)]
        troncon = assurer_troncon(arete, _index(offset), points)
    SignalementTroncon.objects.update_or_create(incident=incident, defaults={'troncon': troncon})
    return troncon


def purger_passages(jours):
    limite = timezone.now() - timedelta(days=jours)
    supprimes, _ = PassageTroncon.objects.filter(horodatage__lt=limite).delete()
    return supprimes

