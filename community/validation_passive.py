"""
Validation PASSIVE des signalements par la telemetrie : un conducteur qui
passe sur un signalement en dit plus, par sa vitesse, qu'un bouton qu'il
n'aura pas le temps de toucher en conduisant.

  - route barree / inondation (effet FERMETURE) : passer dessus a plus de
    VITESSE_ROUTE_OUVERTE_KMH prouve que la route est ouverte -> "Plus rien" ;
  - embouteillage : passer au pas (<= VITESSE_BOUCHON_MAX_KMH) le confirme,
    passer vite (>= VITESSE_FLUIDE_MIN_KMH) l'infirme ; entre les deux, rien.
  - autres types (accident, danger, police...) : la vitesse ne dit rien de
    leur presence -- aucun vote passif.

Un vote passif est un Vote ordinaire (passif=True) : meme unicite (un vote
par conducteur et par incident, actif ou passif), meme effet sur le score,
mais un poids reduit (FACTEUR_POIDS_PASSIF) et jamais de points de
recompense (cf. Incident.confirmer). Seuls VOITURE et TAXI votent : une moto
remonte les files (elle infirmerait un vrai bouchon), un bus ou un camion
roule au pas sans bouchon. Appele par trips.services.consommateur_positions
pour chaque lot de positions d'un trajet.
"""

import logging
import statistics
import uuid
from datetime import datetime
from decimal import Decimal

from django.contrib.gis.geos import Polygon
from django.db import transaction
from django.utils import timezone

from .cache_incidents import invalider_cache_cellule
from .models import Q_CONTESTE, Incident, SensVote, StatutIncident, TypeIncident, Vote

logger = logging.getLogger(__name__)

VITESSE_ROUTE_OUVERTE_KMH = 15
VITESSE_BOUCHON_MAX_KMH = 15
VITESSE_FLUIDE_MIN_KMH = 40
# Le vehicule doit etre passe a moins de COULOIR_M du point signale (sinon une
# rue parallele pourrait "infirmer" la route barree d'a cote) ; sa vitesse est
# mesuree sur les positions a moins de DISTANCE_MESURE_M.
COULOIR_M = 25
DISTANCE_MESURE_M = 60
ECART_CAP_MAX = 45
FACTEUR_POIDS_PASSIF = Decimal('0.5')
MARGE_RECHERCHE_DEG = 0.001  # ~110 m autour des positions du lot
VEHICULES_VOTANTS = {'VOITURE', 'TAXI', ''}
TYPES_OBSERVES = [TypeIncident.EMBOUTEILLAGE, TypeIncident.ROUTE_BARREE, TypeIncident.VOIE_BLOQUEE,
                  TypeIncident.MAUVAIS_TEMPS]


def observer_passages(trajet_id, evenements) -> int:
    """evenements : positions du lot (dicts lat, lon, vitesse_kmh, cap,
    horodatage ISO), dans l'ordre. Retourne le nombre de votes passifs crees."""
    from trips.models import StatutTrajet, Trajet

    if len(evenements) < 2:
        return 0
    try:
        trajet_id = uuid.UUID(str(trajet_id))
    except ValueError:
        return 0
    trajet = (
        Trajet.objects.filter(id=trajet_id, statut=StatutTrajet.ACTIF)
        .select_related('utilisateur').first()
    )
    if trajet is None:
        return 0
    utilisateur = trajet.utilisateur
    vehicule = trajet.type_vehicule or utilisateur.type_vehicule or ''
    if vehicule not in VEHICULES_VOTANTS or utilisateur.mode_invisible or not utilisateur.peut_signaler():
        return 0

    points = _points(evenements)
    if not points:
        return 0
    nb_votes = 0
    for incident in _incidents_proches(points, utilisateur):
        sens = _verdict(incident, points)
        if sens is not None and _voter(incident, utilisateur, sens):
            nb_votes += 1
    return nb_votes


def _points(evenements):
    """[(lat, lon, vitesse_kmh ou None, cap ou None, horodatage)] -- vitesse et
    cap completes depuis la position suivante quand le telephone ne les donne pas."""
    from trips.services.geo import distance_haversine_m
    from trips.services.retards_signalements import _cap

    points = []
    for i, evt in enumerate(evenements):
        try:
            horodatage = datetime.fromisoformat(evt['horodatage'])
        except (KeyError, TypeError, ValueError):
            continue
        vitesse, cap = evt.get('vitesse_kmh'), evt.get('cap')
        if i + 1 < len(evenements):
            suivant = evenements[i + 1]
            try:
                duree_s = (datetime.fromisoformat(suivant['horodatage']) - horodatage).total_seconds()
            except (KeyError, TypeError, ValueError):
                duree_s = 0
            distance_m = distance_haversine_m((evt['lat'], evt['lon']), (suivant['lat'], suivant['lon']))
            if vitesse is None and duree_s > 0:
                vitesse = distance_m / duree_s * 3.6
            if cap is None and distance_m >= 5:  # en dessous, le cap calcule n'est que du bruit GPS
                cap = _cap(evt['lon'], evt['lat'], suivant['lon'], suivant['lat'])
        points.append((evt['lat'], evt['lon'], vitesse, cap, horodatage))
    return points


def _incidents_proches(points, utilisateur):
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    rectangle = Polygon.from_bbox((
        min(lons) - MARGE_RECHERCHE_DEG, min(lats) - MARGE_RECHERCHE_DEG,
        max(lons) + MARGE_RECHERCHE_DEG, max(lats) + MARGE_RECHERCHE_DEG,
    ))
    rectangle.srid = 4326
    return (
        Incident.objects.filter(
            statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE],
            expire_le__gt=timezone.now(),
            position__intersects=rectangle,
            type__in=TYPES_OBSERVES,
            organisation__isnull=True,  # officiel : fait autorite, pas de vote
        )
        .exclude(Q_CONTESTE)
        .exclude(auteur=utilisateur)
        .exclude(votes__votant=utilisateur)
    )


def _verdict(incident, points):
    """SensVote ou None (rien a conclure de ce passage)."""
    from trips.services.geo import distance_haversine_m
    from trips.services.retards_signalements import FERMETURE, effet_de

    effet = effet_de(incident)
    if effet is None:
        return None
    position = (incident.position.y, incident.position.x)
    # Seulement les positions APRES le signalement : un lot envoye en retard
    # ne doit pas infirmer une fermeture survenue depuis.
    proches = [
        (p, distance_haversine_m((p[0], p[1]), position)) for p in points if p[4] >= incident.cree_le
    ]
    proches = [(p, d) for p, d in proches if d <= DISTANCE_MESURE_M]
    if not proches or min(d for _p, d in proches) > COULOIR_M:
        return None  # pas passe sur l'incident (rue parallele, ou trop loin)
    if incident.cap is not None:
        caps = [p[3] for p, _d in proches if p[3] is not None]
        if not caps:
            return None  # sens de circulation inconnu : prudence
        ecart = abs((incident.cap - caps[0] + 180) % 360 - 180)
        if ecart > ECART_CAP_MAX:
            return None  # autre sens de circulation
    vitesses = [p[2] for p, _d in proches if p[2] is not None]
    if not vitesses:
        return None
    vitesse = statistics.median(vitesses)

    if effet.mecanisme == FERMETURE:
        return SensVote.INFIRMATION if vitesse >= VITESSE_ROUTE_OUVERTE_KMH else None
    if incident.type == TypeIncident.EMBOUTEILLAGE:
        if vitesse <= VITESSE_BOUCHON_MAX_KMH:
            return SensVote.CONFIRMATION
        if vitesse >= VITESSE_FLUIDE_MIN_KMH:
            return SensVote.INFIRMATION
    return None


def _voter(incident, utilisateur, sens) -> bool:
    from . import temps_reel

    with transaction.atomic():
        incident = Incident.objects.select_for_update().filter(pk=incident.pk).first()
        if incident is None or Vote.objects.filter(incident=incident, votant=utilisateur).exists():
            return False
        vote = Vote.objects.create(
            incident=incident, votant=utilisateur, sens=sens, passif=True,
            poids=(utilisateur.poids_de_vote() * FACTEUR_POIDS_PASSIF).quantize(Decimal('0.01')),
        )
        if sens == SensVote.CONFIRMATION:
            incident.confirmer(vote)
        else:
            incident.infirmer(vote)

        invalider_cache_cellule(incident.cellule_h3_res8)
        # Meme publication que la vue de vote (VoterIncidentView).
        if incident.expire_le <= timezone.now():
            temps_reel.publier_retrait(incident.id, incident.cellule_h3_res8, temps_reel.RAISON_CONTESTE)
        else:
            temps_reel.publier_incident(incident, temps_reel.MODIFIE)
    logger.info('Vote passif %s sur %s', sens, incident.id)
    return True
