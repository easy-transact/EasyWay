"""
ServiceTrafic : niveau_trafic et duree_avec_trafic pour un itineraire calcule,
a partir de EchantillonVitesse.

Pas de seuil absolu sur la vitesse (25 km/h est rapide en centre-ville, lent
sur une penetrante) : le signal est la vitesse RECENTE d'une arete comparee a
sa vitesse TYPIQUE au meme jour de semaine et a la meme heure -- c'est
precisement pourquoi EchantillonVitesse porte jour_semaine/heure_jour.

Sans historique -- EchantillonVitesse vide au demarrage, cf. P5 -- il n'existe
pas de "typique" : niveau_relatif() replie alors sur NORMAL et
evaluer_route() sur duree_avec_trafic=None. C'est le comportement HONNETE
attendu tant qu'aucun trajet reel n'a alimente le pipeline pendant des
semaines, pas un bug a corriger en trafiquant des seuils sur un jeu de
donnees vide.
"""

import hashlib
import logging

from django.core.cache import cache
from django.db.models import F, Sum
from django.utils import timezone

from ..models import FUSEAU_TRAFIC, EchantillonVitesse, NiveauTrafic
from ..polyline import decoder_polyline6
from .client_meili import ErreurMeili, tracer
from .disjoncteur import DisjoncteurOuvert

logger = logging.getLogger(__name__)

FENETRE_HISTORIQUE = timezone.timedelta(weeks=8)  # capture un rythme hebdo recent sans trainer une donnee perimee -- pas mesure sur donnees reelles, a ajuster
RATIO_MODERE = 0.7  # vitesse recente < 70% de la typique -> MODERE -- pas mesure, a ajuster une fois l'historique disponible
RATIO_DENSE = 0.4  # < 40% de la typique -> DENSE -- idem
DUREE_CACHE_TRAFIC_S = 60  # le trafic bouge plus vite que le calcul d'itineraire (DUREE_CACHE_S=180, service_itineraire.py)
_SEVERITE = {NiveauTrafic.NORMAL: 0, NiveauTrafic.MODERE: 1, NiveauTrafic.DENSE: 2}


def vitesse_typique(identifiant_arete, jour_semaine, heure_jour):
    """Moyenne ponderee par nombre_echantillons des observations passees pour
    cette arete a ce jour de semaine et cette heure, sur la fenetre
    d'historique -- None si aucune donnee (cas attendu au demarrage)."""
    agregat = EchantillonVitesse.objects.filter(
        identifiant_arete=identifiant_arete, jour_semaine=jour_semaine, heure_jour=heure_jour,
        debut_intervalle__gte=timezone.now() - FENETRE_HISTORIQUE,
    ).aggregate(
        somme_ponderee=Sum(F('vitesse_moyenne') * F('nombre_echantillons')),
        poids=Sum('nombre_echantillons'),
    )
    if not agregat['poids']:
        return None
    return agregat['somme_ponderee'] / agregat['poids']


def vitesse_recente(identifiant_arete):
    """Derniere observation connue pour cette arete, tous jours/heures
    confondus -- proxy de "ce qui s'y passe la, maintenant". None si l'arete
    n'a jamais ete observee."""
    plus_recent = (
        EchantillonVitesse.objects.filter(identifiant_arete=identifiant_arete)
        .order_by('-debut_intervalle')
        .first()
    )
    return plus_recent.vitesse_moyenne if plus_recent else None


def niveau_relatif(vitesse_recente_kmh, vitesse_typique_kmh) -> str:
    if not vitesse_recente_kmh or not vitesse_typique_kmh:
        return NiveauTrafic.NORMAL  # pas assez d'historique pour cette arete -- honnete plutot que de deviner
    ratio = float(vitesse_recente_kmh) / float(vitesse_typique_kmh)
    if ratio < RATIO_DENSE:
        return NiveauTrafic.DENSE
    if ratio < RATIO_MODERE:
        return NiveauTrafic.MODERE
    return NiveauTrafic.NORMAL


def evaluer_route(geometrie_encodee: str) -> dict:
    """Retourne {'niveau_trafic':, 'duree_avec_trafic':}. Ne leve jamais --
    une panne Valhalla/Meili ou une trace trop courte degrade juste la
    fraicheur du trafic affiche (repli NORMAL/None), ne doit jamais faire
    echouer le calcul d'itineraire qui l'appelle (cf. ServiceItineraire)."""
    cle_cache = 'trafic:route:' + hashlib.sha256(geometrie_encodee.encode()).hexdigest()
    resultat = cache.get(cle_cache)
    if resultat is not None:
        return resultat

    resultat = _evaluer_sans_cache(geometrie_encodee)
    cache.set(cle_cache, resultat, timeout=DUREE_CACHE_TRAFIC_S)
    return resultat


def _evaluer_sans_cache(geometrie_encodee: str) -> dict:
    repli = {'niveau_trafic': NiveauTrafic.NORMAL, 'duree_avec_trafic': None}

    points_route = decoder_polyline6(geometrie_encodee)
    if len(points_route) < 2:
        return repli

    try:
        # walk_or_snap, pas map_snap : cette geometrie vient deja de Valhalla
        # /route (cf. client_meili.tracer), pas d'une trace GPS bruitee --
        # tente edge_walk (rapide, precis) et ne bascule sur map_snap que si
        # necessaire plutot que d'echouer sur la moindre micro-discontinuite.
        edges, _matched_points = tracer(
            [{'lat': lat, 'lon': lon} for lon, lat in points_route],
            shape_match='walk_or_snap',
        )
    except (ErreurMeili, DisjoncteurOuvert) as exc:
        logger.info('Evaluation trafic indisponible (Valhalla/Meili): %s', exc)
        return repli

    if not edges:
        return repli

    maintenant_local = timezone.now().astimezone(FUSEAU_TRAFIC)
    jour_semaine, heure_jour = maintenant_local.weekday(), maintenant_local.hour

    niveau_pire = NiveauTrafic.NORMAL
    duree_estimee_s = 0.0
    a_mesure_une_arete = False

    for edge in edges:
        longueur_km = edge.get('length') or 0
        if longueur_km <= 0:
            continue
        identifiant_arete = edge['id']

        recente = vitesse_recente(identifiant_arete)
        typique = vitesse_typique(identifiant_arete, jour_semaine, heure_jour)
        niveau_arete = niveau_relatif(recente, typique)
        if _SEVERITE[niveau_arete] > _SEVERITE[niveau_pire]:
            niveau_pire = niveau_arete

        # A defaut d'observation recente, repli sur la vitesse assumee par
        # Valhalla pour cette arete -- rapproche alors duree_avec_trafic de
        # duree_prevue plutot que d'inventer un chiffre, cf. module docstring.
        vitesse_a_utiliser = float(recente) if recente else edge.get('speed')
        if vitesse_a_utiliser:
            a_mesure_une_arete = True
            duree_estimee_s += longueur_km / vitesse_a_utiliser * 3600

    if not a_mesure_une_arete:
        return repli

    return {'niveau_trafic': niveau_pire, 'duree_avec_trafic': round(duree_estimee_s)}
