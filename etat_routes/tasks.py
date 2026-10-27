import logging

from celery import shared_task

from .services.calcul import calculer_notes
from .services.troncons import purger_passages

logger = logging.getLogger(__name__)

# Observations brutes gardees 90 jours (spec section 8) : de quoi recalculer
# apres une regeneration du graphe Valhalla ou un changement de seuils.
RETENTION_PASSAGES_JOURS = 90


@shared_task
def calculer_etat_routes():
    """Celery Beat, chaque nuit (cf. CELERY_BEAT_SCHEDULE)."""
    bilan = calculer_notes()
    logger.info('Etat des routes : %s', bilan)
    return bilan


@shared_task
def purger_passages_anciens():
    return purger_passages(RETENTION_PASSAGES_JOURS)
