"""
Trafic PREVU dans les tuiles Valhalla : profils de vitesse par semaine
(lundi 8 h bouche, dimanche fluide...) construits depuis l'historique des
conducteurs EasyWay (EchantillonVitesse), au format attendu par l'outil
`valhalla_add_predicted_traffic` de Valhalla. Valhalla s'en sert pour tout
calcul (speed_types contient 'predicted'), en particulier avec `date_time`
(depart differe, cf. ServiceItineraire.calculer).

Format (verifie dans le code de Valhalla 3.5.1 : mjolnir/
valhalla_add_predicted_traffic.cc, baldr/predictedspeeds.cc, baldr/datetime.cc) :

  - un fichier CSV par tuile, au meme chemin relatif que la tuile
    (ex. 1/034/029.csv pour 1/034/029.gph) ;
  - une ligne par arete : "niveau/tuile/index,vitesse_libre,vitesse_contrainte,profil"
    (vitesses en km/h entieres) ;
  - profil : 2016 creneaux de 5 min sur une semaine qui commence le DIMANCHE
    a 0 h (heure locale), compresses en 200 coefficients DCT-II (int16,
    normalisation sqrt(2/2016), premier coefficient x 1/sqrt(2)), ecrits en
    big-endian puis en base64.

L'historique est agrege par heure (EchantillonVitesse.heure_jour) : les 12
creneaux d'une heure recoivent la meme vitesse. Une heure sans donnees prend
la vitesse libre la nuit et la vitesse contrainte le jour.
"""

import base64
import math
import os
import struct
from collections import defaultdict

from django.db.models import F, Sum
from django.utils import timezone

NB_COEFFICIENTS = 200
NB_CRENEAUX = 2016  # 7 jours x 24 h x 12 creneaux de 5 min
CRENEAUX_PAR_HEURE = 12
NORMALISATION = math.sqrt(2.0 / NB_CRENEAUX)
NB_MIN_ECHANTILLONS_PROFIL = 50  # sous ce total, pas assez d'historique pour un profil
NB_MIN_HEURES_PROFIL = 24        # heures distinctes de la semaine couvertes
HEURES_NUIT = set(range(21, 24)) | set(range(0, 6))
HEURES_JOUR = set(range(7, 20))

_table_heures = None


def _table():
    """[heure de la semaine][coefficient] = somme des cos DCT-II sur ses 12
    creneaux : le profil est constant par heure, 168 x 200 produits au lieu
    de 2016 x 200 par arete."""
    global _table_heures
    if _table_heures is None:
        pas = math.pi / NB_CRENEAUX
        _table_heures = [
            [
                sum(math.cos(pas * (creneau + 0.5) * c)
                    for creneau in range(h * CRENEAUX_PAR_HEURE, (h + 1) * CRENEAUX_PAR_HEURE))
                for c in range(NB_COEFFICIENTS)
            ]
            for h in range(7 * 24)
        ]
    return _table_heures


def encoder_profil(vitesses_horaires) -> str:
    """vitesses_horaires : 168 vitesses km/h, heure 0 = dimanche 0 h (heure
    locale). Retourne la chaine base64 de valhalla_add_predicted_traffic."""
    if len(vitesses_horaires) != 7 * 24:
        raise ValueError('168 vitesses horaires attendues')
    table = _table()
    coefficients = [0.0] * NB_COEFFICIENTS
    for heure, vitesse in enumerate(vitesses_horaires):
        ligne = table[heure]
        for c in range(NB_COEFFICIENTS):
            coefficients[c] += ligne[c] * vitesse
    coefficients[0] *= 1 / math.sqrt(2)
    entiers = [max(-32768, min(32767, round(NORMALISATION * c))) for c in coefficients]
    return base64.b64encode(struct.pack(f'>{NB_COEFFICIENTS}h', *entiers)).decode('ascii')


def decoder_vitesse(profil: str, creneau: int) -> float:
    """Inverse (DCT-III), comme Valhalla : vitesse km/h du creneau de 5 min
    (0 = dimanche 0 h). Sert aux tests et aux verifications."""
    entiers = struct.unpack(f'>{NB_COEFFICIENTS}h', base64.b64decode(profil))
    pas = math.pi / NB_CRENEAUX
    vitesse = entiers[0] / math.sqrt(2)
    for c in range(1, NB_COEFFICIENTS):
        vitesse += entiers[c] * math.cos(pas * (creneau + 0.5) * c)
    return vitesse * NORMALISATION


def graph_id_texte(graph_id: int) -> str:
    """GraphId -> "niveau/tuile/index" (bits 0-2, 3-24, 25-45)."""
    return f'{graph_id & 0x7}/{(graph_id >> 3) & 0x3fffff}/{(graph_id >> 25) & 0x1fffff}'


def _heure_valhalla(jour_semaine: int, heure: int) -> int:
    """Python : lundi = 0 ; Valhalla : la semaine commence le dimanche."""
    return ((jour_semaine + 1) % 7) * 24 + heure


def _profil(par_heure):
    """par_heure : {heure Valhalla (0-167): (vitesse, poids)}. Retourne
    (vitesse_libre, vitesse_contrainte, 168 vitesses) ou None si trop peu
    d'historique."""
    if len(par_heure) < NB_MIN_HEURES_PROFIL or sum(p for _v, p in par_heure.values()) < NB_MIN_ECHANTILLONS_PROFIL:
        return None
    nuit = [v for h, (v, _p) in par_heure.items() if h % 24 in HEURES_NUIT]
    jour = [(v, p) for h, (v, p) in par_heure.items() if h % 24 in HEURES_JOUR]
    toutes = sorted(v for v, _p in par_heure.values())
    libre = max(nuit) if nuit else toutes[int(0.9 * (len(toutes) - 1))]
    contrainte = (sum(v * p for v, p in jour) / sum(p for _v, p in jour)) if jour else min(toutes)
    libre = max(libre, contrainte)
    vitesses = []
    for h in range(7 * 24):
        if h in par_heure:
            vitesses.append(par_heure[h][0])
        else:
            vitesses.append(libre if h % 24 in HEURES_NUIT else contrainte)
    return round(libre), round(contrainte), vitesses


def profils_par_graph_id(debut_generation=None):
    """{graph_id actuel: (vitesse_libre, vitesse_contrainte, 168 vitesses)} :
    historique par voie OSM + sens reporte sur les GraphId actuels
    (CorrespondanceArete), et historique par GraphId pour les lignes sans
    voie, depuis le debut de la generation de tuiles (cf. service_trafic)."""
    from ..models import CorrespondanceArete, EchantillonVitesse
    from .service_trafic import FENETRE_HISTORIQUE

    base = EchantillonVitesse.objects.filter(debut_intervalle__gte=timezone.now() - FENETRE_HISTORIQUE)
    moyenne = dict(somme=Sum(F('vitesse_moyenne') * F('nombre_echantillons')), poids=Sum('nombre_echantillons'))

    profils = {}
    # Troncon (noeuds OSM) d'abord, puis voie + sens, puis GraphId.
    par_troncon = defaultdict(dict)
    for a in (base.filter(noeud_debut_osm__isnull=False, noeud_fin_osm__isnull=False)
              .values('noeud_debut_osm', 'noeud_fin_osm', 'jour_semaine', 'heure_jour').annotate(**moyenne)):
        if a['poids']:
            par_troncon[(a['noeud_debut_osm'], a['noeud_fin_osm'])][
                _heure_valhalla(a['jour_semaine'], a['heure_jour'])] = (float(a['somme'] / a['poids']), a['poids'])
    profils_troncon = {cle: p for cle, par_heure in par_troncon.items() if (p := _profil(par_heure))}
    if profils_troncon:
        for graph_id, debut, fin in CorrespondanceArete.objects.filter(
            noeud_debut_osm__isnull=False,
        ).values_list('graph_id', 'noeud_debut_osm', 'noeud_fin_osm').iterator():
            profil = profils_troncon.get((debut, fin))
            if profil:
                profils[graph_id] = profil

    par_voie = defaultdict(dict)
    for a in (base.filter(way_id__isnull=False, sens_avant__isnull=False, noeud_debut_osm__isnull=True)
              .values('way_id', 'sens_avant', 'jour_semaine', 'heure_jour').annotate(**moyenne)):
        if a['poids']:
            par_voie[(a['way_id'], a['sens_avant'])][_heure_valhalla(a['jour_semaine'], a['heure_jour'])] = (
                float(a['somme'] / a['poids']), a['poids'])

    profils_voie = {cle: p for cle, par_heure in par_voie.items() if (p := _profil(par_heure))}
    if profils_voie:
        for graph_id, way_id, sens in CorrespondanceArete.objects.values_list(
            'graph_id', 'way_id', 'sens_avant',
        ).iterator():
            profil = profils_voie.get((way_id, sens))
            if profil and graph_id not in profils:
                profils[graph_id] = profil

    sans_voie = base.filter(way_id__isnull=True)
    if debut_generation is not None:
        sans_voie = sans_voie.filter(debut_intervalle__gte=debut_generation)
    par_arete = defaultdict(dict)
    for a in sans_voie.values('identifiant_arete', 'jour_semaine', 'heure_jour').annotate(**moyenne):
        if a['poids']:
            par_arete[a['identifiant_arete']][_heure_valhalla(a['jour_semaine'], a['heure_jour'])] = (
                float(a['somme'] / a['poids']), a['poids'])
    for graph_id, par_heure in par_arete.items():
        if graph_id not in profils and (profil := _profil(par_heure)):
            profils[graph_id] = profil
    return profils


def exporter(chemin_trafic_tar, dossier_sortie) -> dict:
    """Ecrit les CSV de valhalla_add_predicted_traffic dans dossier_sortie, un
    par tuile, aux chemins des tuiles de traffic.tar (meme arborescence que
    les tuiles routieres). Retourne {'aretes': n, 'tuiles': n}."""
    from django.core.cache import cache

    from .service_trafic import CLE_DEBUT_GENERATION_TUILES
    from .tuiles_trafic import MASQUE_TUILE, chemins_tuiles

    chemins = chemins_tuiles(chemin_trafic_tar)
    par_tuile = defaultdict(list)
    for graph_id, (libre, contrainte, vitesses) in profils_par_graph_id(cache.get(CLE_DEBUT_GENERATION_TUILES)).items():
        chemin = chemins.get(graph_id & MASQUE_TUILE)
        if chemin is None:
            continue  # GraphId inconnu des tuiles actuelles
        par_tuile[chemin].append(f'{graph_id_texte(graph_id)},{libre},{contrainte},{encoder_profil(vitesses)}')

    for chemin, lignes in par_tuile.items():
        fichier = os.path.join(dossier_sortie, chemin + '.csv')
        os.makedirs(os.path.dirname(fichier), exist_ok=True)
        with open(fichier, 'w') as sortie:
            sortie.write('\n'.join(lignes) + '\n')
    return {'aretes': sum(len(lignes) for lignes in par_tuile.values()), 'tuiles': len(par_tuile)}
