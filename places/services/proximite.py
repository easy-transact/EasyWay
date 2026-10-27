"""
Services utiles a proximite (GET /api/places/nearby/) : garages, hopitaux,
stations-service et pharmacies dans un rayon donne, du plus proche au plus
loin -- lieux approuves (import compris, contrairement a la carte qui ne
montre que les ajouts) et etablissements visibles.

Horaires d'ouverture (Lieu.horaires, stations et pharmacies) :
    {"mon": [["08:00", "20:00"]], ..., "sun": []}
    une liste de plages par jour ; [] = ferme ce jour ; ["00:00", "24:00"] =
    ouvert toute la journee ; une plage dont la fermeture precede l'ouverture
    passe minuit (["20:00", "02:00"]). {} = horaires inconnus.
"""

import re
from datetime import datetime, timedelta

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.db.models import Q

from ..models import Etablissement, Lieu, StatutLieu, TypeEtablissement
from ..utils import normaliser
from .garages import est_garage, etoiles, notes

# Type -> mots cherches dans la categorie du lieu (avec et sans accents),
# et type d'etablissement equivalent.
TYPES = {
    'garage': {'mots': ['garage', 'mécanique', 'mecanique', 'car_repair'], 'etablissement': TypeEtablissement.GARAGE},
    'hopital': {
        'mots': ['hôpital', 'hopital', 'clinique', 'centre de santé', 'centre de sante', 'hospital'],
        'etablissement': TypeEtablissement.HOPITAL,
    },
    # "Station de taxi" n'est pas une station-service.
    'station': {'mots': ['station-service', 'station service', 'carburant', 'fuel'], 'etablissement': None},
    'pharmacie': {'mots': ['pharmacie', 'pharmacy'], 'etablissement': None},
}
TYPES_AVEC_HORAIRES = ('station', 'pharmacie')

RAYON_DEFAUT_M = 2000
RAYON_MAX_M = 20000
LIMITE_DEFAUT = 50
LIMITE_MAX = 200

JOURS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
FORMAT_HEURE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$|^24:00$')


def type_de(categorie):
    """Type (garage/hopital/station/pharmacie) d'une categorie de lieu, ou None."""
    categorie = normaliser(categorie or '')
    if est_garage(categorie):
        return 'garage'
    for nom, definition in TYPES.items():
        if any(normaliser(mot) in categorie for mot in definition['mots']):
            return nom
    return None


# --- Horaires ---------------------------------------------------------------

class HorairesInvalides(ValueError):
    pass


def valider_horaires(horaires):
    """Retourne les horaires normalises ou leve HorairesInvalides."""
    if horaires in (None, {}):
        return {}
    if not isinstance(horaires, dict) or set(horaires) - set(JOURS):
        raise HorairesInvalides(f'Expected an object with keys among {", ".join(JOURS)}.')
    resultat = {}
    for jour in JOURS:
        plages = horaires.get(jour, [])
        if not isinstance(plages, list):
            raise HorairesInvalides(f'{jour}: expected a list of ["HH:MM", "HH:MM"].')
        for plage in plages:
            if (
                not isinstance(plage, list) or len(plage) != 2
                or not all(isinstance(h, str) and FORMAT_HEURE.match(h) for h in plage)
                or plage[0] == plage[1] or plage[0] == '24:00'
            ):
                raise HorairesInvalides(f'{jour}: invalid range {plage!r} (expected ["HH:MM", "HH:MM"]).')
        resultat[jour] = [list(p) for p in plages]
    return resultat


def _minutes(heure):
    h, m = heure.split(':')
    return int(h) * 60 + int(m)


def est_ouvert(horaires, moment):
    """True/False selon les horaires a `moment` (datetime local), None si
    horaires inconnus."""
    if not horaires:
        return None
    minute = moment.hour * 60 + moment.minute
    jour = JOURS[moment.weekday()]
    veille = JOURS[(moment - timedelta(days=1)).weekday()]
    for debut, fin in horaires.get(jour, []):
        d, f = _minutes(debut), _minutes(fin)
        if d < f and d <= minute < f:
            return True
        if d > f and minute >= d:  # commence aujourd'hui, finit apres minuit
            return True
    # Plage commencee la veille et qui passe minuit.
    return any(_minutes(d) > _minutes(f) and minute < _minutes(f) for d, f in horaires.get(veille, []))


# --- Recherche --------------------------------------------------------------

def _filtre_lieux(types):
    filtre = Q()
    for nom in types:
        for mot in TYPES[nom]['mots']:
            filtre |= Q(categorie__icontains=mot)
    if 'station' in types:
        filtre &= ~Q(categorie__icontains='taxi')
    return filtre


def a_proximite(lat, lon, rayon_m, types, limite, maintenant):
    """Liste triee par distance. maintenant : datetime local (Africa/Douala)."""
    centre = Point(lon, lat, srid=4326)
    lieux = (
        Lieu.objects.filter(statut=StatutLieu.APPROUVE, position__dwithin=(centre, D(m=rayon_m)))
        .filter(_filtre_lieux(types))
        .annotate(distance=Distance('position', centre))
        .order_by('distance')[:limite]
    )
    types_etablissement = [TYPES[t]['etablissement'] for t in types if TYPES[t]['etablissement']]
    etablissements = (
        Etablissement.objects.filter(
            visible_sur_carte=True, type__in=types_etablissement, position__dwithin=(centre, D(m=rayon_m)),
        ).annotate(distance=Distance('position', centre)).order_by('distance')[:limite]
        if types_etablissement else []
    )

    resultats = []
    for lieu in lieux:
        type_lieu = type_de(lieu.categorie)
        if type_lieu not in types:
            continue
        resultats.append({
            'id': str(lieu.id), 'kind': 'place', 'type': type_lieu, 'name': lieu.nom, 'category': lieu.categorie,
            'address': lieu.adresse, 'city': lieu.ville, 'lat': lieu.position.y, 'lon': lieu.position.x,
            'distance_m': round(lieu.distance.m), 'horaires': lieu.horaires,
            'phone': lieu.telephone, 'position_approximate': lieu.position_approximative,
        })
    for etablissement in etablissements:
        resultats.append({
            'id': str(etablissement.id), 'kind': 'establishment',
            'type': 'garage' if etablissement.type == TypeEtablissement.GARAGE else 'hopital',
            'name': etablissement.nom, 'category': etablissement.get_type_display(), 'address': etablissement.adresse,
            'city': etablissement.ville, 'lat': etablissement.position.y, 'lon': etablissement.position.x,
            'distance_m': round(etablissement.distance.m), 'horaires': {},
            'phone': etablissement.telephone, 'position_approximate': False,
        })
    # Positions sures d'abord (on peut y aller), puis les approximatives
    # (centre du quartier), chacune par distance.
    resultats.sort(key=lambda r: (r['position_approximate'], r['distance_m']))
    resultats = resultats[:limite]

    garages = [r for r in resultats if r['type'] == 'garage']
    stats = notes(
        [r['id'] for r in garages if r['kind'] == 'place'],
        [r['id'] for r in garages if r['kind'] == 'establishment'],
    )
    for r in resultats:
        horaires = r.pop('horaires')
        if r['type'] in TYPES_AVEC_HORAIRES:
            r['opening_hours'] = horaires or None
            r['is_open_now'] = est_ouvert(horaires, maintenant)
        if r['type'] == 'garage':
            note = stats.get(r['id'], {'average': None, 'count': 0})
            r['rating_average'], r['rating_count'] = note['average'], note['count']
            r['stars'] = etoiles(note['average'], note['count'])
    return resultats


def maintenant_local():
    from trips.models import FUSEAU_TRAFIC

    return datetime.now(FUSEAU_TRAFIC)
