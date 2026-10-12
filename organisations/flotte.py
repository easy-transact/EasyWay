"""
Suivi de flotte (phase 3 de l'espace agences) : un chauffeur lie a une fiche
Chauffeur choisit, au depart d'un trajet dans l'application, le vehicule de
l'agence qu'il conduit. Ce trajet (et lui seul -- jamais les trajets
personnels) devient visible par l'organisation : position en direct, trace,
signalements faits pendant le trajet, exces de vitesse.
"""

from django.contrib.gis.geos import MultiPoint, Point
from django.contrib.gis.measure import D
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Chauffeur,
    ExcesVitesse,
    NotificationOrganisation,
    StatutFiche,
    StatutOrganisation,
    StatutVehicule,
    TypeNotificationOrganisation,
    Vehicule,
)
from .services import journaliser

# Deux positions en exces separees de plus de cet ecart = deux episodes.
ECART_MAX_EPISODE = timezone.timedelta(seconds=60)
# Couloir de recherche d'une zone de vitesse autour d'une position (meme
# valeur que /api/speed-limit/, cf. trips.views.BUFFER_M_ZONE_VITESSE).
BUFFER_ZONE_M = 30
# Une seule notification d'exces par trajet sur cette fenetre : un conducteur
# qui roule vite sur 20 km ne doit pas declencher 30 notifications.
FENETRE_NOTIFICATION_EXCES = timezone.timedelta(minutes=15)
# Au-dela, un vehicule n'est plus considere "en route" sur la carte en direct.
DELAI_POSITION_EN_DIRECT = timezone.timedelta(minutes=15)


class VehiculeRefuse(Exception):
    """Message destine au client (400)."""


# --- Lien compte EasyWay <-> fiche Chauffeur ----------------------------------


def lier_chauffeurs_au_compte(utilisateur):
    """Rattache au compte les fiches chauffeur saisies par les agences avec
    le meme numero (fiches encore sans compte). Appele a l'inscription et a la
    creation/modification d'une fiche."""
    if not utilisateur.telephone:
        return 0
    return Chauffeur.objects.filter(telephone=utilisateur.telephone, utilisateur__isnull=True).update(
        utilisateur=utilisateur
    )


def lier_compte_a_la_fiche(chauffeur):
    """Fiche creee ou modifiee : la relie au compte EasyWay de meme telephone
    s'il existe (ou la delie si le telephone a change)."""
    from accounts.models import Utilisateur

    compte = Utilisateur.objects.filter(telephone=chauffeur.telephone).first() if chauffeur.telephone else None
    if chauffeur.utilisateur_id != (compte.pk if compte else None):
        chauffeur.utilisateur = compte
        chauffeur.save(update_fields=['utilisateur', 'modifie_le'])


def fiches_chauffeur_actives(utilisateur):
    return Chauffeur.objects.filter(
        utilisateur=utilisateur,
        statut=StatutFiche.ACTIF,
        organisation__statut=StatutOrganisation.ACTIVE,
    ).select_related('organisation')


def vehicules_disponibles(utilisateur):
    """Vehicules que l'utilisateur peut declarer conduire : ceux en service
    des agences ou il a une fiche chauffeur active. Retourne des tuples
    (vehicule, fiche chauffeur, attribue_a_lui)."""
    fiches = {f.organisation_id: f for f in fiches_chauffeur_actives(utilisateur)}
    if not fiches:
        return []
    vehicules = (
        Vehicule.objects.filter(organisation_id__in=fiches.keys(), statut=StatutVehicule.ACTIF)
        .select_related('organisation', 'chauffeur')
        .order_by('immatriculation')
    )
    resultat = []
    for v in vehicules:
        fiche = fiches[v.organisation_id]
        resultat.append((v, fiche, v.chauffeur_id == fiche.id))
    # Ses vehicules attribues d'abord (preselection cote application).
    resultat.sort(key=lambda t: (not t[2], t[0].immatriculation))
    return resultat


def resoudre_vehicule(utilisateur, vehicule_id):
    """Verifie que l'utilisateur peut conduire ce vehicule ; retourne
    (vehicule, fiche chauffeur). Leve VehiculeRefuse sinon."""
    for vehicule, fiche, _attribue in vehicules_disponibles(utilisateur):
        if str(vehicule.id) == str(vehicule_id):
            return vehicule, fiche
    raise VehiculeRefuse("This vehicle is not available to you (not a driver of its organisation, or out of service).")


# --- Telemetrie : position en direct et exces de vitesse ----------------------


def _limite_pour(position, zones_candidates, limite_defaut):
    """(limite, zone) applicable a une position : zone active la plus
    restrictive a moins de BUFFER_ZONE_M, sinon la limite par defaut."""
    applicables = [z for z in zones_candidates if z.geometrie.distance(position) * 111_320 <= BUFFER_ZONE_M]
    if not applicables:
        return limite_defaut, None
    zone = min(applicables, key=lambda z: z.vitesse_max_kmh)
    return zone.vitesse_max_kmh, zone


def traiter_telemetrie(trajet, positions):
    """Appele pour chaque lot de positions d'un trajet d'agence (cf.
    trips.views.TelemetriePositionsView) : met a jour la derniere position
    et regroupe les depassements en episodes ExcesVitesse."""
    if not trajet.vehicule_id or not positions:
        return
    from trips.models import Trajet, ZoneVitesse
    from trips.views import LIMITE_VITESSE_DEFAUT_KMH

    positions = sorted(positions, key=lambda p: p['horodatage'])
    derniere = positions[-1]
    Trajet.objects.filter(pk=trajet.pk).filter(
        Q(derniere_position_le__isnull=True) | Q(derniere_position_le__lte=derniere['horodatage'])
    ).update(
        derniere_position=Point(derniere['lon'], derniere['lat'], srid=4326),
        derniere_vitesse_kmh=derniere.get('vitesse_kmh'),
        dernier_cap=derniere.get('cap'),
        derniere_position_le=derniere['horodatage'],
    )

    avec_vitesse = [p for p in positions if p.get('vitesse_kmh') is not None]
    if not avec_vitesse:
        return
    tolerance = trajet.organisation.tolerance_vitesse_kmh
    nuage = MultiPoint([Point(p['lon'], p['lat'], srid=4326) for p in avec_vitesse], srid=4326)
    # Zones proches du lot, chargees une fois (geometrie plane : distance en
    # degres convertie approximativement, suffisant a 30 m pres).
    zones = list(
        ZoneVitesse.objects.filter(
            actif=True, geometrie__isnull=False, geometrie__distance_lte=(nuage, D(m=BUFFER_ZONE_M * 2))
        )
    )

    for p in avec_vitesse:
        point = Point(p['lon'], p['lat'], srid=4326)
        limite, zone = _limite_pour(point, zones, LIMITE_VITESSE_DEFAUT_KMH)
        if p['vitesse_kmh'] > limite + tolerance:
            _enregistrer_exces(trajet, point, p, limite, zone)


def _enregistrer_exces(trajet, point, position, limite, zone):
    with transaction.atomic():
        episode = (
            ExcesVitesse.objects.select_for_update()
            .filter(trajet=trajet, fin_le__gte=position['horodatage'] - ECART_MAX_EPISODE)
            .order_by('-fin_le')
            .first()
        )
        if episode is not None:
            episode.fin_le = max(episode.fin_le, position['horodatage'])
            episode.nb_positions += 1
            episode.limite_kmh = min(episode.limite_kmh, limite)
            if position['vitesse_kmh'] > episode.vitesse_max_kmh:
                episode.vitesse_max_kmh = position['vitesse_kmh']
                episode.position = point
            episode.save()
            return
        episode = ExcesVitesse.objects.create(
            organisation_id=trajet.organisation_id, trajet=trajet, vehicule_id=trajet.vehicule_id,
            chauffeur_id=trajet.chauffeur_id, limite_kmh=limite, vitesse_max_kmh=position['vitesse_kmh'],
            zone=zone, nom_zone=zone.nom if zone else '', position=point,
            debut_le=position['horodatage'], fin_le=position['horodatage'],
        )
    _notifier_exces(trajet, episode)


def _notifier_exces(trajet, episode):
    deja = NotificationOrganisation.objects.filter(
        organisation_id=trajet.organisation_id,
        cle=f'exces-{trajet.id}',
        cree_le__gte=timezone.now() - FENETRE_NOTIFICATION_EXCES,
    ).exists()
    if deja:
        return
    vehicule = trajet.vehicule
    chauffeur = trajet.chauffeur
    lieu = f' · {episode.nom_zone}' if episode.nom_zone else ''
    NotificationOrganisation.objects.create(
        organisation_id=trajet.organisation_id,
        type=TypeNotificationOrganisation.EXCES_VITESSE,
        titre=f'Excès de vitesse · {vehicule.immatriculation if vehicule else "véhicule"}',
        texte=(
            f'{chauffeur.nom_complet if chauffeur else "Chauffeur"} : {episode.vitesse_max_kmh:.0f} km/h '
            f'pour {episode.limite_kmh} km/h{lieu}'
        ),
        lien=f'/agence/trajets?ouvrir={trajet.id}',
        cle=f'exces-{trajet.id}',
    )


# --- Signalements faits pendant un trajet d'agence ----------------------------


def rattacher_signalement(incident):
    """Incident cree par un chauffeur en plein trajet d'agence : rattache au
    trajet (donc visible par l'organisation) et notifie l'agence."""
    from trips.models import StatutTrajet, Trajet

    trajet = (
        Trajet.objects.filter(utilisateur_id=incident.auteur_id, statut=StatutTrajet.ACTIF, vehicule__isnull=False)
        .select_related('vehicule', 'chauffeur')
        .order_by('-demarre_le')
        .first()
    )
    if trajet is None:
        return None
    incident.trajet = trajet
    incident.save(update_fields=['trajet'])
    NotificationOrganisation.objects.create(
        organisation_id=trajet.organisation_id,
        type=TypeNotificationOrganisation.SIGNALEMENT,
        titre=f'Signalement · {incident.get_type_display()}',
        texte=(
            f'{trajet.chauffeur.nom_complet if trajet.chauffeur else "Chauffeur"} '
            f'({trajet.vehicule.immatriculation}) · {incident.nom_voie or incident.ville or "position inconnue"}'
        ),
        lien='/agence/signalements',
        cle=f'signalement-{incident.id}',
    )
    return trajet


def declarer_vehicule(trajet, vehicule, fiche, acteur):
    """Appele a la creation du trajet quand le chauffeur a choisi un vehicule."""
    trajet.organisation = vehicule.organisation
    trajet.vehicule = vehicule
    trajet.chauffeur = fiche
    trajet.save(update_fields=['organisation', 'vehicule', 'chauffeur'])
    journaliser(
        vehicule.organisation, acteur, 'trajet.demarre', trajet,
        vehicule=vehicule.immatriculation, chauffeur=fiche.nom_complet,
    )


