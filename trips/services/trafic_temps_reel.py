"""
Trafic temps reel dans le calcul d'itineraire : publie dans le traffic.tar
de Valhalla (cf. tuiles_trafic.py) les vitesses mesurees ces 5 dernieres
minutes, pour que Valhalla CHOISISSE la route selon le trafic -- pas
seulement en corriger la duree apres coup (cf. service_trafic.py).

Sources, par ordre de priorite sur une meme arete :
  1. positions GPS deja recalees par ConsommateurPositions (Meili) :
     mediane des vitesses par conducteur, a partir de NB_MIN_CONDUCTEURS ;
  2. signalements d'embouteillage actifs, la ou rien n'est mesure ;
  3. vitesse typique du creneau (EchantillonVitesse, meme jour et heure),
     si l'historique est suffisant.
Une arete non rafraichie depuis DUREE_VALIDITE_S est effacee : Valhalla
retombe alors sur la vitesse de la classe de route.
"""

import logging
import math
import statistics
from collections import defaultdict

from django.core.cache import cache
from django.utils import timezone

from community.models import Q_CONTESTE, Incident, SousTypeIncident, StatutIncident, TypeIncident

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
# Une mesure non rafraichie depuis ce delai est effacee (Valhalla retombe sur
# la vitesse de la classe) : un bouchon resorbe ne detourne plus les
# conducteurs pendant 15 min. Les aretes venues d'un SIGNALEMENT sont, elles,
# effacees des la publication suivante s'il ne les produit plus (retrait,
# expiration, contestation) -- cf. CLE_ARETES_SIGNALEMENTS.
DUREE_VALIDITE_S = 10 * 60
CLE_ARETES_PUBLIEES = 'trafic:publie'  # ZSET {graph_id: horodatage de derniere ecriture}
CLE_INCIDENTS_PUBLIES = 'trafic:incidents_publies'
CLE_ARETES_SIGNALEMENTS = 'trafic:aretes_signalements'  # cache : aretes ecrites depuis un signalement au tour precedent
CLE_DERNIERE_PUBLICATION = 'trafic:derniere_publication'  # cache : sante (cf. sante_trafic)
CLE_GENERATION_TUILES = 'trafic:generation'  # cache : identifiant du traffic.tar en place
# Un peu plus de 2 passes de la tache (90 s) : si la publication s'arrete, la
# cle expire et les embouteillages repassent en retard dans le calcul.
DUREE_INCIDENTS_PUBLIES_S = 3 * 60

# Vitesses imposees par un signalement d'embouteillage -- a calibrer sur les
# routes de Douala.
VITESSE_SIGNALEMENT_KMH = {
    SousTypeIncident.A_L_ARRET: 5,
    SousTypeIncident.IMPORTANT: 15,
}
VITESSE_SIGNALEMENT_DEFAUT_KMH = 25
# Vitesse "normale" de reference pour ponderer un embouteillage par sa
# confiance (pas de vitesse par arete disponible ici) : vitesse urbaine fluide.
VITESSE_NORMALE_REFERENCE_KMH = 40
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
    # Filtre sur le nom de cle d'abord, puis une seule lecture groupee
    # (pipeline) au lieu d'un aller-retour Redis par bucket.
    a_lire = []
    for cle in connexion.smembers(ENSEMBLE_BUCKETS_ACTIFS):
        try:
            _prefixe, arete_str, bucket_str = cle.rsplit(':', 2)
            graph_id, bucket_epoch = int(arete_str), int(bucket_str)
        except ValueError:
            continue
        if bucket_epoch + TAILLE_BUCKET_S <= maintenant - FENETRE_TEMPS_REEL_S:
            continue
        a_lire.append((cle, graph_id))
    with connexion.pipeline(transaction=False) as pipe:
        for cle, _graph_id in a_lire:
            pipe.hgetall(cle)
        contenus = pipe.execute()
    for (_cle, graph_id), contenu in zip(a_lire, contenus):
        for champ, valeur in (contenu or {}).items():
            if not champ.startswith(PREFIXE_CHAMP_TRAJET):
                continue
            trajet, _, mesure = champ[len(PREFIXE_CHAMP_TRAJET):].rpartition(':')
            cumul = par_arete[graph_id][trajet]
            if mesure == 'distance':
                cumul[0] += float(valeur)  # km
            elif mesure == 'duree':
                cumul[1] += float(valeur)  # s

    vitesses = {}
    for graph_id, par_trajet in par_arete.items():
        # Une vitesse par conducteur = sa distance totale / sa duree totale sur
        # l'arete (pas une moyenne de vitesses), puis mediane entre conducteurs.
        par_conducteur = [distance / (duree / 3600) for distance, duree in par_trajet.values() if duree > 0]
        if len(par_conducteur) >= NB_MIN_CONDUCTEURS:
            vitesses[graph_id] = statistics.median(par_conducteur)
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
    """Aretes ralenties par un embouteillage signale : son arete et celles des
    AMONT_SIGNALEMENT_M metres en amont. Sans cap, [] : sur une route a
    double sens, /locate sans heading peut rendre l'arete du sens OPPOSE --
    on ralentirait la mauvaise voie. L'embouteillage n'est alors pas dans
    traffic.tar, et retards_signalements le compte en retard a la place."""
    if incident.cap is None:
        return []
    cle = f'trafic:incident:aretes:{incident.id}'
    aretes = cache.get(cle)
    if aretes is None:
        aretes = _aretes_amont(incident)
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
    return _vitesses_et_incidents_signalements()[0]


def incidents_publies() -> set | None:
    """Ids (str) des embouteillages effectivement ecrits dans traffic.tar a la
    derniere publication, ou None si aucune publication recente (tache
    arretee, TRAFIC_TAR_PATH vide, fichier illisible) -- dans ce cas, aucun
    embouteillage n'est dans traffic.tar et retards_signalements doit les
    compter en retard (cf. REVUE_CONFIRMATION_SIGNALEMENTS, point 4)."""
    ids = cache.get(CLE_INCIDENTS_PUBLIES)
    return None if ids is None else set(ids)


def _vitesses_et_incidents_signalements():
    """({graph_id: km/h}, {ids des incidents dont au moins une arete est ecrite})."""
    vitesses, publies = {}, set()
    incidents = Incident.objects.filter(
        type=TypeIncident.EMBOUTEILLAGE, statut=StatutIncident.ACTIF, expire_le__gt=timezone.now(),
    ).exclude(Q_CONTESTE).select_related('auteur')  # conteste : sort de traffic.tar a la publication suivante
    for incident in incidents:
        try:
            aretes = _aretes_incident(incident)
        except DisjoncteurOuvert as exc:
            # Valhalla en panne : inutile d'essayer les suivants a ce tour.
            logger.info('Aretes des embouteillages signales indisponibles: %s', exc)
            break
        except client_locate.ErreurLocate as exc:
            # Erreur sur CET incident : on passe au suivant, sans priver les autres.
            logger.info('Aretes de l\'embouteillage %s indisponibles: %s', incident.id, exc)
            continue
        vitesse = _vitesse_ponderee(incident)
        if aretes:
            publies.add(str(incident.id))
        for graph_id in aretes:
            # Plusieurs signalements sur la meme arete : le plus severe l'emporte.
            vitesses[graph_id] = min(vitesse, vitesses.get(graph_id, vitesse))
    return vitesses, publies


def _fermetures_signalees():
    """({graph_ids fermes}, {ids des fermetures ecrites}) : routes barrees et
    inondations CONFIRMEES (meme regle que retards_signalements.fermeture_confirmee),
    marquees fermees dans traffic.tar sur l'arete exacte du signalement, dans
    son sens (heading). Sans cap, le sens serait devine : non publiee, elle
    reste exclue du calcul comme un point (exclude_locations)."""
    from .retards_signalements import FERMETURE, effet_de, fermeture_confirmee

    fermees, publiees = set(), set()
    incidents = Incident.objects.filter(
        statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE], expire_le__gt=timezone.now(),
        cap__isnull=False,
    ).exclude(Q_CONTESTE).select_related('auteur')
    for incident in incidents:
        effet = effet_de(incident)
        if effet is None or effet.mecanisme != FERMETURE or not fermeture_confirmee(incident, effet, 'auto'):
            continue
        cle = f'trafic:incident:fermeture:{incident.id}'
        graph_id = cache.get(cle)
        if graph_id is None:
            try:
                arete = client_locate.localiser(incident.position.y, incident.position.x, incident.cap)
            except DisjoncteurOuvert as exc:
                logger.info('Aretes des fermetures signalees indisponibles: %s', exc)
                break
            except client_locate.ErreurLocate as exc:
                logger.info('Arete de la fermeture %s indisponible: %s', incident.id, exc)
                continue
            graph_id = arete.get('graph_id') if arete else None
            if graph_id is None:
                continue
            cache.set(cle, graph_id, timeout=DUREE_CACHE_ARETE_INCIDENT_S)
        fermees.add(graph_id)
        publiees.add(str(incident.id))
    return fermees, publiees


def _vitesse_ponderee(incident) -> float:
    """Vitesse imposee, ramenee vers la vitesse normale selon la confiance
    (meme confiance que pour les retards : votes x usure dans le temps) : un
    embouteillage en fin de vie ou peu confirme ralentit moins qu'un
    embouteillage tout juste confirme par plusieurs conducteurs."""
    from .retards_signalements import confiance

    imposee = VITESSE_SIGNALEMENT_KMH.get(incident.sous_type, VITESSE_SIGNALEMENT_DEFAUT_KMH)
    normale = max(VITESSE_NORMALE_REFERENCE_KMH, imposee)
    return round(normale - (normale - imposee) * confiance(incident), 1)


def publier(chemin_trafic, connexion, maintenant: int) -> dict:
    """Ecrit les vitesses courantes et efface les perimees. Retourne des
    compteurs pour les logs de la tache. Leve TraficIndisponible si le
    fichier est absent/illisible -- a l'appelant de journaliser."""
    # Ordre de priorite sur une meme arete (le dernier update l'emporte) :
    #   4. rien : vitesse de la classe de route (Valhalla) ;
    #   3. vitesse typique du creneau, si l'historique est suffisant ;
    #   2. embouteillage signale ;
    #   1. mesure GPS en direct.
    # Sans 3, l'historique accumule dans EchantillonVitesse n'atteignait jamais
    # Valhalla : avec peu de conducteurs, presque tout le reseau etait route
    # en vitesse theorique (REVUE_ROUTAGE_ET_SERVICES, point 1.2).
    from .service_trafic import vitesses_typiques_creneau

    _suivre_generation(chemin_trafic, connexion)
    signalements, publies = _vitesses_et_incidents_signalements()
    fermees, fermetures_publiees = _fermetures_signalees()
    mesurees = vitesses_mesurees(connexion, maintenant)
    vitesses = vitesses_typiques_creneau()
    vitesses.update(signalements)
    # Fermeture confirmee : l'emporte sur l'historique et les embouteillages,
    # mais pas sur une mesure GPS (des voitures y roulent : elle n'est pas
    # fermee -- la validation passive l'infirmera).
    fermees -= set(mesurees)
    for graph_id in fermees:
        vitesses.pop(graph_id, None)
    vitesses.update(mesurees)  # une mesure reelle l'emporte
    aretes_signalements = (set(signalements) | fermees) - set(mesurees)
    publies |= fermetures_publiees

    ecrites = tuiles_trafic.ecrire_vitesses(chemin_trafic, vitesses, fermees)
    # Ecrit APRES l'ecriture reussie (ecrire_vitesses leve si le fichier est
    # absent/illisible) : retards_signalements ne saute que ces signalements-la.
    cache.set(CLE_INCIDENTS_PUBLIES, sorted(publies), timeout=DUREE_INCIDENTS_PUBLIES_S)
    if vitesses or fermees:
        connexion.zadd(CLE_ARETES_PUBLIEES, {str(graph_id): maintenant for graph_id in [*vitesses, *fermees]})

    # Aretes ecrites depuis un signalement au tour precedent et plus produites
    # maintenant (signalement retire, expire, conteste) : effacees tout de
    # suite plutot qu'apres DUREE_VALIDITE_S.
    precedentes = set(cache.get(CLE_ARETES_SIGNALEMENTS) or [])
    finies = precedentes - set(vitesses)
    perimees = {int(g) for g in connexion.zrangebyscore(CLE_ARETES_PUBLIEES, 0, maintenant - DUREE_VALIDITE_S)}
    a_effacer = sorted(perimees | finies)
    effacees = 0
    if a_effacer:
        effacees = tuiles_trafic.effacer(chemin_trafic, a_effacer)
        connexion.zrem(CLE_ARETES_PUBLIEES, *a_effacer)
    cache.set(CLE_ARETES_SIGNALEMENTS, sorted(aretes_signalements), timeout=DUREE_VALIDITE_S)

    compteurs = {'ecrites': ecrites, 'effacees': effacees}
    cache.set(CLE_DERNIERE_PUBLICATION, {'le': maintenant, **compteurs}, timeout=None)
    return compteurs


def _suivre_generation(chemin_trafic, connexion):
    """traffic.tar remplace (tuiles reconstruites) : les GraphId ont change.
    On oublie les correspondances GraphId -> voie (reapprises par le
    consommateur), les aretes publiees et les vitesses typiques en cache, et
    on note le debut de la nouvelle generation (cf. vitesses_typiques_creneau).
    L'historique par voie OSM (EchantillonVitesse.way_id) est conserve."""
    from ..models import CorrespondanceArete
    from .service_trafic import CLE_DEBUT_GENERATION_TUILES

    actuelle = tuiles_trafic.generation(chemin_trafic)
    precedente = cache.get(CLE_GENERATION_TUILES)
    if precedente == actuelle:
        return
    if precedente is not None:
        logger.warning('traffic.tar remplace (tuiles reconstruites) : correspondances d\'aretes reinitialisees')
        CorrespondanceArete.objects.all().delete()
        cache.delete_many([CLE_ARETES_SIGNALEMENTS, CLE_INCIDENTS_PUBLIES])
        if hasattr(cache, 'delete_pattern'):  # django-redis
            cache.delete_pattern('trafic:typiques:*')
        # Anciens GraphId : ne surtout pas les "effacer" dans le nouveau fichier,
        # ils y designent d'autres aretes.
        connexion.delete(CLE_ARETES_PUBLIEES)
        cache.set(CLE_DEBUT_GENERATION_TUILES, timezone.now(), timeout=None)
    cache.set(CLE_GENERATION_TUILES, actuelle, timeout=None)


def sante_trafic(connexion, maintenant: int) -> dict:
    """Etat du trafic temps reel pour le back-office : chaque etape se degrade
    en silence (par choix), d'ou ces compteurs pour voir qu'elle ne marche plus."""
    from .consommateur_positions import NOM_GROUPE
    from .producteur_evenements import FLUX_POSITIONS

    derniere = cache.get(CLE_DERNIERE_PUBLICATION)
    etat = {
        'last_publication_at': derniere and derniere['le'],
        'last_publication_age_s': derniere and maintenant - derniere['le'],
        'last_publication_written': derniere and derniere['ecrites'],
        'stream_length': None, 'pending_messages': None, 'oldest_pending_age_s': None,
    }
    try:
        etat['stream_length'] = connexion.xlen(FLUX_POSITIONS)
        resume = connexion.xpending(FLUX_POSITIONS, NOM_GROUPE)
        etat['pending_messages'] = resume.get('pending', 0)
        plus_ancien = resume.get('min')
        if plus_ancien:
            # Id Redis Streams "<ms>-<seq>" : l'horodatage d'arrivee du message.
            etat['oldest_pending_age_s'] = maintenant - int(str(plus_ancien).split('-')[0]) // 1000
    except Exception as exc:  # Redis-telemetrie injoignable : on le dit, sans planter la page
        logger.info('Sante du flux de telemetrie indisponible: %s', exc)
    return etat
