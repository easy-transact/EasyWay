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
import statistics
from collections import defaultdict

from django.core.cache import cache
from django.utils import timezone

from community.models import Incident, SousTypeIncident, StatutIncident, TypeIncident

from . import client_locate, tuiles_trafic
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
DUREE_CACHE_ARETE_INCIDENT_S = 3600  # un incident ne bouge pas : un seul /locate par heure


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


def _arete_incident(incident):
    cle = f'trafic:incident:arete:{incident.id}'
    graph_id = cache.get(cle)
    if graph_id is None:
        arete = client_locate.localiser(incident.position.y, incident.position.x, incident.cap)
        graph_id = arete.get('graph_id') if arete else None
        if graph_id is None:
            return None
        cache.set(cle, graph_id, timeout=DUREE_CACHE_ARETE_INCIDENT_S)
    return graph_id


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
            graph_id = _arete_incident(incident)
        except (client_locate.ErreurLocate, DisjoncteurOuvert) as exc:
            logger.info('Aretes des embouteillages signales indisponibles: %s', exc)
            break
        if graph_id is None:
            continue
        vitesse = VITESSE_SIGNALEMENT_KMH.get(incident.sous_type, VITESSE_SIGNALEMENT_DEFAUT_KMH)
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
