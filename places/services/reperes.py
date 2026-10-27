"""
Points de repere dans le guidage (spec du 26/10) : pour chaque manoeuvre
d'un itineraire, un lieu nomme visible a moins de RAYON_M du point de
manoeuvre -- "au carrefour Ndokoti", "au niveau de Total Bonateki".

Regle d'or : absent plutot que douteux. Ecartes : lieux non approuves, a
position approximative (centre de quartier, cf. services/positions.py),
aux noms illisibles, et ceux qu'on ne voit pas en arrivant (projetes sur le
trace apres la manoeuvre). Rien pour le depart ni l'arrivee.
"""

import math

from django.contrib.gis.geos import MultiPoint, Point
from django.contrib.gis.measure import D

from trips.polyline import decoder_polyline6
from trips.services.geo import distance_haversine_m

from ..models import Etablissement, Lieu, StatutLieu, TypeEtablissement
from ..utils import normaliser

RAYON_M = 60
TOLERANCE_APRES_M = 15  # au carrefour meme : le lieu peut etre juste apres le point
LONGUEUR_NOM_MAX = 40
# Types de manoeuvre Valhalla : depart (1-3) et arrivee (4-6).
TYPES_EXCLUS = {'1', '2', '3', '4', '5', '6'}

# Nature -> mots de la categorie normalisee (minuscules, sans accents), par
# ordre de priorite. Une categorie qui COMMENCE par un de ces mots ("marche"
# ne doit pas attraper "supermarche").
PRIORITES = [
    ('CARREFOUR', ('carrefour', 'rond-point', 'rond point', 'roundabout')),
    ('STATION', ('station-service', 'station service', 'carburant', 'fuel')),
    ('CULTE', ('eglise', 'mosquee', 'lieu de culte', 'cathedrale', 'chapelle', 'temple', 'place_of_worship')),
    ('SANTE', ('hopital', 'clinique', 'centre de sante', 'pharmacie', 'hospital', 'clinic', 'pharmacy')),
    ('MARCHE', ('marche', 'marketplace')),
    ('AUTRE', ('banque', 'ecole', 'college', 'lycee', 'universite', 'hotel', 'supermarche', 'bank', 'school')),
]
RANG = {nature: i for i, (nature, _) in enumerate(PRIORITES)}
PREFIXES_CARREFOUR = ('carrefour', 'rond-point', 'rond point')


def nature(categorie, nom):
    """CARREFOUR/STATION/... ou None si le lieu n'est pas un repere."""
    if normaliser(nom or '').startswith(PREFIXES_CARREFOUR):
        return 'CARREFOUR'
    categorie = normaliser(categorie or '')
    if 'taxi' in categorie:
        return None
    for nom_nature, mots in PRIORITES:
        if any(categorie.startswith(mot) for mot in mots):
            return nom_nature
    return None


def nom_lisible(nom):
    nom = (nom or '').strip()
    return bool(nom) and len(nom) <= LONGUEUR_NOM_MAX and ';' not in nom


class Trace:
    """Geometrie de l'itineraire avec distances cumulees (metres)."""

    def __init__(self, geometrie):
        self.points = [(lat, lon) for lon, lat in decoder_polyline6(geometrie)]
        self.cumul = [0.0]
        for a, b in zip(self.points, self.points[1:]):
            self.cumul.append(self.cumul[-1] + distance_haversine_m(a, b))

    def point_a(self, distance):
        """(lat, lon) a `distance` metres du depart, le long du trace."""
        for i in range(1, len(self.points)):
            if self.cumul[i] >= distance:
                longueur = self.cumul[i] - self.cumul[i - 1]
                t = (distance - self.cumul[i - 1]) / longueur if longueur else 0
                (lat1, lon1), (lat2, lon2) = self.points[i - 1], self.points[i]
                return lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1)
        return self.points[-1]

    def abscisse(self, lat, lon, autour, fenetre=250):
        """Distance depuis le depart du point du trace le plus proche de
        (lat, lon), cherche a +/- fenetre metres de `autour`."""
        meilleure, abscisse = math.inf, autour
        echelle = math.cos(math.radians(lat))
        for i in range(1, len(self.points)):
            if self.cumul[i] < autour - fenetre or self.cumul[i - 1] > autour + fenetre:
                continue
            (lat1, lon1), (lat2, lon2) = self.points[i - 1], self.points[i]
            dx, dy = (lon2 - lon1) * echelle, lat2 - lat1
            norme = dx * dx + dy * dy
            t = 0 if not norme else max(0, min(1, ((lon - lon1) * echelle * dx + (lat - lat1) * dy) / norme))
            proj = (lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1))
            ecart = distance_haversine_m((lat, lon), proj)
            if ecart < meilleure:
                meilleure = ecart
                abscisse = self.cumul[i - 1] + t * (self.cumul[i] - self.cumul[i - 1])
        return abscisse


def _candidats(points):
    """Lieux et etablissements reperes a moins de RAYON_M d'un des points :
    [(nature, nom, lat, lon)]."""
    zone = MultiPoint([Point(lon, lat, srid=4326) for lat, lon in points], srid=4326)
    lieux = Lieu.objects.filter(
        statut=StatutLieu.APPROUVE, position_approximative=False, position__dwithin=(zone, D(m=RAYON_M)),
    ).only('nom', 'categorie', 'position')
    resultat = [(nature(l.categorie, l.nom), l.nom.strip(), l.position.y, l.position.x) for l in lieux]
    hopitaux = Etablissement.objects.filter(
        type=TypeEtablissement.HOPITAL, visible_sur_carte=True, position__dwithin=(zone, D(m=RAYON_M)),
    )
    resultat += [('SANTE', e.nom.strip(), e.position.y, e.position.x) for e in hopitaux]
    return [c for c in resultat if c[0] and nom_lisible(c[1])]


def ajouter_reperes(candidat):
    """Ajoute 'repere' ({name, kind} ou None) a chaque manoeuvre du candidat
    (forme ServiceItineraire._normaliser_trip)."""
    manoeuvres = candidat.get('manoeuvres') or []
    for m in manoeuvres:
        m['repere'] = None
    if candidat.get('degrade') or not candidat.get('geometrie'):
        return candidat
    trace = Trace(candidat['geometrie'])
    if len(trace.points) < 2:
        return candidat

    # Point de chaque manoeuvre : distance cumulee des manoeuvres precedentes
    # (valable aussi pour un itineraire assemble, cf. ClientCorridorReference).
    positions, cumul = [], 0.0
    for m in manoeuvres:
        positions.append(cumul)
        cumul += m.get('distance') or 0
    a_traiter = [i for i, m in enumerate(manoeuvres) if str(m.get('type')) not in TYPES_EXCLUS]
    if not a_traiter:
        return candidat
    points = {i: trace.point_a(positions[i]) for i in a_traiter}
    lieux = _candidats(list(points.values()))

    precedent = None
    for i in a_traiter:
        point = points[i]
        meilleur = None
        for nature_lieu, nom, lat, lon in lieux:
            distance = distance_haversine_m(point, (lat, lon))
            if distance > RAYON_M:
                continue
            # Visible en arrivant : avant la manoeuvre ou au carrefour meme.
            if trace.abscisse(lat, lon, positions[i]) > positions[i] + TOLERANCE_APRES_M:
                continue
            cle = (RANG[nature_lieu], distance)
            if meilleur is None or cle < meilleur[0]:
                meilleur = (cle, nom, nature_lieu)
        if meilleur is None:
            precedent = None
            continue
        if precedent == meilleur[1]:
            continue  # meme repere que la manoeuvre d'avant : seulement sur la premiere
        manoeuvres[i]['repere'] = {'name': meilleur[1], 'kind': meilleur[2]}
        precedent = meilleur[1]
    return candidat
