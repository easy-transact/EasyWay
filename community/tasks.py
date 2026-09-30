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
