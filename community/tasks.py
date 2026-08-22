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
    cellules_a_invalider = set(expires.values_list('cellule_h3_res7', flat=True))
    nb = expires.update(statut=StatutIncident.EXPIRE)

    for cellule in cellules_a_invalider:
        invalider_cache_cellule(cellule)

    return nb
