from celery import shared_task
from django.utils import timezone

from .cache_incidents import invalider_cache_cellule
from .models import Incident, StatutIncident


@shared_task
def expirer_incidents():
    """Celery Beat, cadence 60s (cf. CELERY_BEAT_SCHEDULE). Statut seul ne
    suffit pas a rendre un incident invisible de /proches/ -- celle-ci filtre
    deja sur expire_le, mais sans ce passage un incident EXPIRE resterait
    ACTIF en base indefiniment et le cache de sa cellule ne serait jamais
    invalide entre deux TTL naturels."""
    expires = Incident.objects.filter(
        statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE],
        expire_le__lte=timezone.now(),
    )
    cellules_a_invalider = set(expires.values_list('cellule_h3_res8', flat=True))
    nb = expires.update(statut=StatutIncident.EXPIRE)

    for cellule in cellules_a_invalider:
        invalider_cache_cellule(cellule)

    return nb


@shared_task
def activer_programmes():
    """Celery Beat, cadence 60s (meme que expirer_incidents). Passe ACTIF les
    signalements officiels PROGRAMME dont debut_le est atteint (travaux dont la
    periode commence), et les diffuse en temps reel."""
    from . import temps_reel

    a_activer = list(
        Incident.objects.filter(statut=StatutIncident.PROGRAMME, debut_le__lte=timezone.now())
        .filter(expire_le__gt=timezone.now())
    )
    for incident in a_activer:
        incident.statut = StatutIncident.ACTIF
        incident.save(update_fields=['statut'])
        invalider_cache_cellule(incident.cellule_h3_res8)
        temps_reel.publier_incident(incident, temps_reel.CREE)
    return len(a_activer)


MAX_RECALAGES_PAR_PASSE = 100


@shared_task
def recaler_signalements_sans_voie():
    """Celery Beat, toutes les 5 min. Un signalement cree pendant une panne
    de Valhalla n'a ni voie OSM ni sens (way_id_osm/forward_osm) : sans eux,
    il n'est retrouve sur un trajet que par le couloir de distance. Des que
    Valhalla repond, on complete voie et sens (meme appel /locate, avec le cap,
    que ServiceIncident._verifier_position_routiere). La position n'est pas
    deplacee : sa cellule H3 (cache, temps reel) est calculee a la creation.
    Un point decidement hors route (> 50 m, allee privee) reste sans voie."""
    from trips.services import client_locate
    from trips.services.disjoncteur import DisjoncteurOuvert

    from .services import RAYON_MAX_HORS_ROUTE_M

    a_recaler = (
        Incident.objects.filter(
            statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE, StatutIncident.PROGRAMME],
            expire_le__gt=timezone.now(), way_id_osm__isnull=True,
        ).order_by('-cree_le')[:MAX_RECALAGES_PAR_PASSE]
    )
    nb = 0
    for incident in a_recaler:
        try:
            arete = client_locate.localiser(incident.position.y, incident.position.x, cap=incident.cap)
        except DisjoncteurOuvert:
            break  # Valhalla toujours en panne : on reessaiera a la passe suivante
        except client_locate.ErreurLocate:
            continue
        if arete is None or arete['destination_only'] or arete['distance_m'] > RAYON_MAX_HORS_ROUTE_M:
            continue
        incident.way_id_osm, incident.forward_osm = arete['way_id'], arete['forward']
        champs = ['way_id_osm', 'forward_osm']
        if incident.cap is not None:
            from trips.services.client_trace_attributes import troncon_au_point

            noeuds = troncon_au_point(incident.position.y, incident.position.x, incident.cap)
            if noeuds:
                incident.noeud_debut_osm, incident.noeud_fin_osm = noeuds
                champs += ['noeud_debut_osm', 'noeud_fin_osm']
        incident.save(update_fields=champs)
        invalider_cache_cellule(incident.cellule_h3_res8)
        nb += 1
    return nb


# Les signalements termines restent en base (votes compris) pour l'etat des
# routes, qui les exploite sur cette fenetre -- au-dela, simple purge pour que
# la table ne grossisse pas indefiniment.
RETENTION_INCIDENTS_TERMINES_JOURS = 120


@shared_task
def purger_incidents_anciens():
    """Celery Beat, quotidien (cf. CELERY_BEAT_SCHEDULE). Supprime les
    incidents EXPIRE/RETIRE/FUSIONNE dont la fin remonte a plus de
    RETENTION_INCIDENTS_TERMINES_JOURS ; leurs votes partent avec eux
    (cascade). Deja invisibles de l'API, aucun cache a invalider."""
    limite = timezone.now() - timezone.timedelta(days=RETENTION_INCIDENTS_TERMINES_JOURS)
    nb, _detail = Incident.objects.filter(
        statut__in=[StatutIncident.EXPIRE, StatutIncident.RETIRE, StatutIncident.FUSIONNE],
        expire_le__lte=limite,
    ).delete()
    return nb
