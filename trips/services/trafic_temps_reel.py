"""
Trafic temps reel dans le calcul d'itineraire : publie dans le traffic.tar
de Valhalla (cf. tuiles_trafic.py) les vitesses mesurees ces 5 dernieres
minutes, pour que Valhalla CHOISISSE la route selon le trafic -- pas
seulement en corriger la duree apres coup (cf. service_trafic.py).

Sources, par ordre de priorite sur une meme arete :
  1. positions GPS deja recalees par ConsommateurPositions (Meili) :
     mediane des vitesses par conducteur, a partir de NB_MIN_CONDUCTEURS ;
  2. signalements d'embouteillage actifs, la ou rien n'est mesure.
Une arete non rafraichie depuis DUREE_VALIDITE_S est effacee : Valhalla
retombe alors sur l'historique puis la vitesse de la classe de route.
"""

import logging
import math
import statistics
from collections import defaultdict

from django.core.cache import cache
from django.utils import timezone

from community.models import Incident, SousTypeIncident, StatutIncident, TypeIncident

from . import client_locate, client_meili, tuiles_trafic
from .consommateur_positions import (
    ENSEMBLE_BUCKETS_ACTIFS,
    PREFIXE_CHAMP_TRAJET,
    TAILLE_BUCKET_S,
)
from .disjoncteur import DisjoncteurOuvert

logger = logging.getLogger(__name__)

FENETRE_TEMPS_REEL_S = 5 * 60
NB_MIN_CONDUCTEURS = 2  # un seul vehicule lent (livraison, panne) ne fait pas un bouchon
DUREE_VALIDITE_S = 15 * 60
CLE_ARETES_PUBLIEES = 'trafic:publie'  # ZSET {graph_id: horodatage de derniere ecriture}

# Vitesses imposees par un signalement d'embouteillage -- a calibrer sur les
# routes de Douala.
VITESSE_SIGNALEMENT_KMH = {
    SousTypeIncident.A_L_ARRET: 5,
    SousTypeIncident.IMPORTANT: 15,
}
VITESSE_SIGNALEMENT_DEFAUT_KMH = 25
DUREE_CACHE_ARETE_INCIDENT_S = 3600  # un incident ne bouge pas : un seul recalage par heure
# La file d'un bouchon s'etend en amont du point signale (spec TRAFIC_VALHALLA,
# section 3) : aretes ralenties sur cette distance, recalee tous les PAS_AMONT_M.
AMONT_SIGNALEMENT_M = 200
PAS_AMONT_M = 40
METRES_PAR_DEGRE = 111_320


def vitesses_mesurees(connexion, maintenant: int) -> dict:
    """{graph_id: vitesse mediane km/h} sur les buckets qui recouvrent les
    FENETRE_TEMPS_REEL_S dernieres secondes, y compris le bucket encore
    ouvert (contrairement au flush vers EchantillonVitesse)."""
    par_arete = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    for cle in connexion.smembers(ENSEMBLE_BUCKETS_ACTIFS):
        try:
            _prefixe, arete_str, bucket_str = cle.rsplit(':', 2)
            graph_id, bucket_epoch = int(arete_str), int(bucket_str)
        except ValueError:
            continue
        if bucket_epoch + TAILLE_BUCKET_S <= maintenant - FENETRE_TEMPS_REEL_S:
            continue
        for champ, valeur in connexion.hgetall(cle).items():
            if not champ.startswith(PREFIXE_CHAMP_TRAJET):
                continue
            trajet, _, mesure = champ[len(PREFIXE_CHAMP_TRAJET):].rpartition(':')
            cumul = par_arete[graph_id][trajet]
            if mesure == 'somme':
                cumul[0] += float(valeur)
            elif mesure == 'nombre':
                cumul[1] += int(valeur)

    vitesses = {}
    for graph_id, par_trajet in par_arete.items():
        moyennes = [somme / nombre for somme, nombre in par_trajet.values() if nombre > 0]
        if len(moyennes) >= NB_MIN_CONDUCTEURS:
            vitesses[graph_id] = statistics.median(moyennes)
    return vitesses


def _amont(lat, lon, cap, distance_m):
    """Point a distance_m EN AMONT (d'ou viennent les vehicules) selon cap."""
    angle = math.radians(cap)
    return (
        lat - distance_m * math.cos(angle) / METRES_PAR_DEGRE,
        lon - distance_m * math.sin(angle) / (METRES_PAR_DEGRE * math.cos(math.radians(lat))),
    )


def _aretes_amont(incident):
    """Aretes dirigees des AMONT_SIGNALEMENT_M metres qui menent au bouchon,
    par recalage d'une ligne amont -> signalement : la file d'attente est en
    amont du point signale, dans le sens du cap. [] si le recalage echoue."""
    lat, lon = incident.position.y, incident.position.x
    points = [
        {'lat': p[0], 'lon': p[1], 'time': i * 4}
        for i, p in enumerate(
            _amont(lat, lon, incident.cap, d) for d in range(AMONT_SIGNALEMENT_M, -1, -PAS_AMONT_M)
        )
    ]
    try:
        aretes, _points = client_meili.tracer(points)
    except (client_meili.ErreurMeili, DisjoncteurOuvert):
        return []
    return [a['id'] for a in aretes if a.get('id') is not None]


def _aretes_incident(incident):
    """Aretes ralenties par un embouteillage signale : son arete et, s'il a
    un cap, celles des AMONT_SIGNALEMENT_M metres en amont."""
    cle = f'trafic:incident:aretes:{incident.id}'
    aretes = cache.get(cle)
    if aretes is None:
        aretes = _aretes_amont(incident) if incident.cap is not None else []
        if not aretes:
            arete = client_locate.localiser(incident.position.y, incident.position.x, incident.cap)
            aretes = [arete['graph_id']] if arete and arete.get('graph_id') is not None else []
        if not aretes:
            return []
        cache.set(cle, aretes, timeout=DUREE_CACHE_ARETE_INCIDENT_S)
    return aretes


def vitesses_signalements() -> dict:
    """{graph_id: km/h} pour l'arete de chaque embouteillage actif. Valhalla
    indisponible : on s'arrete la, sans lever -- les mesures GPS restent
    publiees."""
    vitesses = {}
    incidents = Incident.objects.filter(
        type=TypeIncident.EMBOUTEILLAGE, statut=StatutIncident.ACTIF, expire_le__gt=timezone.now(),
    )
    for incident in incidents:
        try:
            aretes = _aretes_incident(incident)
        except (client_locate.ErreurLocate, DisjoncteurOuvert) as exc:
            logger.info('Aretes des embouteillages signales indisponibles: %s', exc)
            break
        vitesse = VITESSE_SIGNALEMENT_KMH.get(incident.sous_type, VITESSE_SIGNALEMENT_DEFAUT_KMH)
        for graph_id in aretes:
            # Plusieurs signalements sur la meme arete : le plus severe l'emporte.
            vitesses[graph_id] = min(vitesse, vitesses.get(graph_id, vitesse))
    return vitesses


def publier(chemin_trafic, connexion, maintenant: int) -> dict:
    """Ecrit les vitesses courantes et efface les perimees. Retourne des
    compteurs pour les logs de la tache. Leve TraficIndisponible si le
    fichier est absent/illisible -- a l'appelant de journaliser."""
    vitesses = vitesses_signalements()
    vitesses.update(vitesses_mesurees(connexion, maintenant))  # une mesure reelle l'emporte

    ecrites = tuiles_trafic.ecrire_vitesses(chemin_trafic, vitesses)
    if vitesses:
        connexion.zadd(CLE_ARETES_PUBLIEES, {str(graph_id): maintenant for graph_id in vitesses})

    perimees = [int(g) for g in connexion.zrangebyscore(CLE_ARETES_PUBLIEES, 0, maintenant - DUREE_VALIDITE_S)]
    effacees = 0
    if perimees:
        effacees = tuiles_trafic.effacer(chemin_trafic, perimees)
        connexion.zrem(CLE_ARETES_PUBLIEES, *perimees)
    return {'ecrites': ecrites, 'effacees': effacees}
