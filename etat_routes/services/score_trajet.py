"""
Etat de la route le long d'un itineraire (champ road_condition de
routes/calculate). Reutilise le recalage deja fait pour le trafic
(trips/services/service_trafic.py) : aucun appel Valhalla en plus.

unknown_m n'est jamais verse dans good_m : "65 % en bon etat, 19 % inconnu"
est honnete, "84 % en bon etat" ne l'est pas.
"""

from ..models import TAILLE_TRONCON_M, ClasseEtat, NoteTroncon
from .troncons import identifiant

ORDRE_GRAVITE = {ClasseEtat.MAUVAIS: 0, ClasseEtat.DEGRADE: 1}
NB_PIRES = 3


def tranches_parcourues(edges, matched_points):
    """[(id tranche, longueur_m, distance depuis le depart du trace)] dans
    l'ordre du trajet. Premiere/derniere arete : seulement la partie
    parcourue ; aretes intermediaires : entieres."""
    offsets = {}
    for point in matched_points:
        k = point.get('edge_index')
        if k is None or k >= len(edges):
            continue
        offset = (point.get('distance_along_edge') or 0) * (edges[k].get('length') or 0) * 1000
        bas, haut = offsets.get(k, (offset, offset))
        offsets[k] = (min(bas, offset), max(haut, offset))

    resultat, cumul, dernier = [], 0.0, len(edges) - 1
    for k, arete in enumerate(edges):
        longueur = (arete.get('length') or 0) * 1000
        if longueur <= 0:
            continue
        debut, fin = offsets.get(k, (0.0, longueur))
        debut = debut if k == 0 else 0.0
        fin = fin if k == dernier else longueur
        index = int(debut // TAILLE_TRONCON_M)
        while index * TAILLE_TRONCON_M < fin:
            a = max(debut, index * TAILLE_TRONCON_M)
            b = min(fin, (index + 1) * TAILLE_TRONCON_M)
            if b > a:
                resultat.append((identifiant(arete['id'], index), b - a, cumul))
                cumul += b - a
            index += 1
    return resultat


def etat_du_trace(edges, matched_points):
    """Longueurs par classe (sans unknown_m, complete par completer())."""
    tranches = tranches_parcourues(edges, matched_points)
    notes = {
        n['troncon_id']: n for n in NoteTroncon.objects.filter(troncon_id__in={t for t, _, _ in tranches}).values(
            'troncon_id', 'classe', 'causes', 'inondable', 'note',
        )
    }
    longueurs = {ClasseEtat.BON: 0.0, ClasseEtat.DEGRADE: 0.0, ClasseEtat.MAUVAIS: 0.0}
    inondable, pires = 0.0, []
    for tid, longueur, depuis in tranches:
        note = notes.get(tid)
        if note is None:
            continue
        if note['classe'] in longueurs:
            longueurs[note['classe']] += longueur
        if note['inondable']:
            inondable += longueur
        if note['classe'] in ORDRE_GRAVITE:
            pires.append((ORDRE_GRAVITE[note['classe']], note['note'] or 0, depuis, tid, note))
    pires = sorted(pires)[:NB_PIRES]
    return {
        'good_m': round(longueurs[ClasseEtat.BON]),
        'degraded_m': round(longueurs[ClasseEtat.DEGRADE]),
        'bad_m': round(longueurs[ClasseEtat.MAUVAIS]),
        'flood_prone_m': round(inondable),
        'worst': [
            {'segment_id': tid, 'class': note['classe'], 'causes': note['causes'], 'distance_from_start_m': round(depuis)}
            for _, _, depuis, tid, note in sorted(pires, key=lambda p: p[2])
        ],
    }


def completer(etat, distance_m):
    """Ajoute unknown_m pour que la somme vaille la distance de l'itineraire."""
    if etat is None:
        return None
    connu = etat['good_m'] + etat['degraded_m'] + etat['bad_m']
    return {**etat, 'unknown_m': max(distance_m - connu, 0)}
