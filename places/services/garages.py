"""
Garages notables (cf. AvisGarage) : un Lieu approuve dont la categorie
designe un garage/une mecanique (import lieux_cameroun.csv : "Garage /
mecanique" ; OSM : "car_repair" ; saisie libre du back-office : "Garage"...),
ou un Etablissement de type GARAGE.
"""

from django.db.models import Avg, Count, Q
from django.http import Http404

from ..models import AvisGarage, Etablissement, Lieu, StatutAvis, StatutLieu, TypeEtablissement
from ..utils import normaliser

# Comparaison sur la categorie normalisee (minuscules, sans accents).
MOTS_GARAGE = ('garage', 'mecanique', 'car_repair', 'car repair')


def est_garage(lieu_ou_categorie):
    """Lieu, Etablissement ou categorie (texte) -> bool."""
    if isinstance(lieu_ou_categorie, Etablissement):
        return lieu_ou_categorie.type == TypeEtablissement.GARAGE
    categorie = lieu_ou_categorie.categorie if isinstance(lieu_ou_categorie, Lieu) else lieu_ou_categorie
    categorie = normaliser(categorie or '')
    return categorie == TypeEtablissement.GARAGE.lower() or any(mot in categorie for mot in MOTS_GARAGE)


def trouver_garage(identifiant):
    """Lieu approuve de categorie garage, sinon Etablissement GARAGE ; 404
    si l'identifiant ne designe aucun garage notable."""
    lieu = Lieu.objects.filter(id=identifiant, statut=StatutLieu.APPROUVE).first()
    if lieu is not None:
        if est_garage(lieu):
            return lieu
        raise Http404('Only garages can be rated.')
    etablissement = Etablissement.objects.filter(id=identifiant).first()
    if etablissement is not None and est_garage(etablissement):
        return etablissement
    raise Http404('Garage not found.')


def filtre_cible(garage):
    """Q() des avis portant sur ce garage."""
    return Q(lieu=garage) if isinstance(garage, Lieu) else Q(etablissement=garage)


def notes(lieu_ids=(), etablissement_ids=()):
    """{id (str): {'average', 'count'}} des avis PUBLIES, en deux requetes."""
    resultat = {}
    publies = AvisGarage.objects.filter(statut=StatutAvis.PUBLIE)
    for champ, ids in (('lieu_id', lieu_ids), ('etablissement_id', etablissement_ids)):
        ids = [i for i in ids if i]
        if not ids:
            continue
        lignes = publies.filter(**{f'{champ}__in': ids}).values(champ).annotate(moyenne=Avg('note'), total=Count('pk'))
        for ligne in lignes:
            resultat[str(ligne[champ])] = {'average': round(ligne['moyenne'], 1), 'count': ligne['total']}
    return resultat


def note_de(garage):
    return notes(
        [garage.id] if isinstance(garage, Lieu) else (),
        [garage.id] if isinstance(garage, Etablissement) else (),
    ).get(str(garage.id), {'average': None, 'count': 0})


def ajouter_notes(points):
    """Points de carte (services.carte.point_lieu/point_etablissement) :
    ajoute is_garage, rating_average et rating_count (null/0 hors garages)."""
    garages = [p for p in points if est_garage(p['category'])]
    stats = notes(
        [p['id'] for p in garages if p['kind'] == 'place'],
        [p['id'] for p in garages if p['kind'] == 'establishment'],
    )
    for point in points:
        point['is_garage'] = est_garage(point['category'])
        note = stats.get(point['id']) if point['is_garage'] else None
        point['rating_average'] = note['average'] if note else None
        point['rating_count'] = note['count'] if note else 0
    return points
