"""
Lieux affiches sur la carte de l'application : dans la zone visible
(GET /api/places/map/) et le long de l'itineraire pendant la navigation
(POST /api/places/along-route/).

Sont renvoyes : les Lieu APPROUVES proposes par les utilisateurs ou ajoutes
par le back-office, et les Etablissement visible_sur_carte. Les lieux
importes d'OpenStreetMap (des dizaines de milliers) restent accessibles par
la recherche seulement : les afficher noierait la carte.
"""

from django.contrib.gis.geos import LineString, Point, Polygon
from django.contrib.gis.measure import D

from trips.services.geo import distance_haversine_m
from trips.services.zones_vitesse import _longueurs_cumulees, _plan_vers_metres

from ..models import Etablissement, Lieu, SourceLieu, StatutLieu
from .garages import ajouter_notes

# Au-dela, la reponse serait illisible sur un ecran de telephone : l'appli
# zoome pour en voir davantage.
MAX_RESULTATS = 300
SOURCES_AFFICHEES = [SourceLieu.UTILISATEUR, SourceLieu.BACK_OFFICE]


def _lieux(categories):
    lieux = Lieu.objects.filter(statut=StatutLieu.APPROUVE, source__in=SOURCES_AFFICHEES)
    if categories:
        lieux = lieux.filter(categorie__in=categories)
    return lieux


def _etablissements(categories):
    etablissements = Etablissement.objects.filter(visible_sur_carte=True)
    if categories:
        # Les types d'etablissement (HOPITAL, GARAGE) se filtrent comme des categories.
        etablissements = etablissements.filter(type__in=categories)
    return etablissements


def point_lieu(lieu):
    return {
        'id': str(lieu.id), 'kind': 'place', 'name': lieu.nom, 'category': lieu.categorie,
        'category_label': lieu.categorie, 'address': lieu.adresse, 'neighborhood': lieu.quartier or '',
        'city': lieu.ville, 'lat': lieu.position.y, 'lon': lieu.position.x,
        'phone': lieu.telephone, 'position_approximate': lieu.position_approximative,
    }


def point_etablissement(etablissement):
    return {
        'id': str(etablissement.id), 'kind': 'establishment', 'name': etablissement.nom,
        'category': etablissement.type, 'category_label': etablissement.get_type_display(),
        'address': etablissement.adresse, 'neighborhood': '', 'city': etablissement.ville,
        'lat': etablissement.position.y, 'lon': etablissement.position.x,
        'phone': etablissement.telephone, 'position_approximate': False,
    }


def lieux_dans_zone(min_lat, min_lon, max_lat, max_lon, categories=None):
    """Lieux et etablissements dans le rectangle ; tronque a MAX_RESULTATS.
    Retourne (points, tronque)."""
    rectangle = Polygon.from_bbox((min_lon, min_lat, max_lon, max_lat))
    rectangle.srid = 4326
    points = [point_etablissement(e) for e in _etablissements(categories).filter(position__intersects=rectangle)[:MAX_RESULTATS]]
    reste = MAX_RESULTATS - len(points)
    lieux = list(_lieux(categories).filter(position__intersects=rectangle).order_by('-score_popularite', 'nom')[: reste + 1])
    tronque = len(lieux) > reste
    points += [point_lieu(l) for l in lieux[:reste]]
    return ajouter_notes(points), tronque


def lieux_sur_trajet(points_trajet, buffer_m, categories=None):
    """points_trajet : (lon, lat) de l'itineraire (decoder_polyline6). Chaque
    lieu a moins de buffer_m du trace, avec position_m (metres depuis le
    debut du trajet, pour annoncer "dans 2 km") et distance_m (ecart au
    trace), tries par position_m."""
    ligne = LineString(points_trajet, srid=4326)
    cumul_plan, cumul_m = _longueurs_cumulees(points_trajet)
    candidats = [
        (e.position, point_etablissement(e))
        for e in _etablissements(categories).filter(position__dwithin=(ligne, D(m=buffer_m)))
    ] + [
        (l.position, point_lieu(l))
        for l in _lieux(categories).filter(position__dwithin=(ligne, D(m=buffer_m)))[: MAX_RESULTATS * 2]
    ]

    resultats = []
    for position, donnees in candidats:
        d_plan = ligne.project(Point(position.x, position.y, srid=4326))
        projete = ligne.interpolate(d_plan)
        ecart = distance_haversine_m((position.y, position.x), (projete.y, projete.x))
        if ecart > buffer_m:
            continue
        donnees['position_m'] = round(_plan_vers_metres(d_plan, cumul_plan, cumul_m))
        donnees['distance_m'] = round(ecart)
        resultats.append(donnees)
    resultats.sort(key=lambda p: p['position_m'])
    return ajouter_notes(resultats[:MAX_RESULTATS])


def categories_affichees():
    """Categories presentes sur la carte (pour les filtres de l'appli), les
    plus frequentes d'abord."""
    from django.db.models import Count

    lieux = (
        _lieux(None).exclude(categorie='').values('categorie').annotate(total=Count('pk')).order_by('-total')
    )
    etablissements = (
        Etablissement.objects.filter(visible_sur_carte=True).values('type').annotate(total=Count('pk'))
    )
    libelles = dict(Etablissement._meta.get_field('type').choices)
    return [
        {'category': e['type'], 'label': libelles.get(e['type'], e['type']), 'count': e['total'], 'kind': 'establishment'}
        for e in etablissements
    ] + [
        {'category': l['categorie'], 'label': l['categorie'], 'count': l['total'], 'kind': 'place'} for l in lieux
    ]
