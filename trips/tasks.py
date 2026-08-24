import logging
import os
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from celery import shared_task

from .models import FUSEAU_TRAFIC, EchantillonVitesse
from .services.consommateur_positions import (
    ENSEMBLE_BUCKETS_ACTIFS,
    SEUIL_INACTIVITE_MS,
    TAILLE_BUCKET_S,
    ConsommateurPositions,
)
from .services.producteur_evenements import connexion_redis_telemetrie

logger = logging.getLogger(__name__)

# Marge au-dela de la fermeture exacte de la fenetre de 5 min : le consommateur
# peut laisser un message en pending jusqu'a SEUIL_INACTIVITE_MS avant qu'XAUTOCLAIM
# ne le reprenne (cf. consommateur_positions.py) -- flusher avant cette marge
# risquerait de persister une moyenne partielle puis de continuer a incrementer
# une cle deja ecrite en base, sans que rien ne revienne jamais la corriger.
MARGE_FLUSH_S = SEUIL_INACTIVITE_MS / 1000


@shared_task
def consommer_positions():
    """Celery Beat, cadence courte (cf. CELERY_BEAT_SCHEDULE) : une passe de
    ConsommateurPositions. Le nom de consommateur inclut le pid pour que
    plusieurs workers Celery sur le meme hote ne se disputent pas le meme nom
    dans le groupe Redis Streams. Un retard de consommation degrade
    uniquement la fraicheur du trafic -- jamais une erreur, POST
    /api/telemetry/positions/ retourne 202 quel que soit l'etat de cette tache."""
    consommateur = ConsommateurPositions(nom_consommateur=f'celery-{os.getpid()}')
    return consommateur.consommer()


@shared_task
def flusher_echantillons_vitesse():
    """Celery Beat, cadence alignee sur la taille du bucket (5 min, cf.
    CELERY_BEAT_SCHEDULE) : persiste dans EchantillonVitesse tout bucket dont
    la fenetre est definitivement fermee (cf. MARGE_FLUSH_S), puis nettoie
    l'accumulateur Redis correspondant -- jamais l'inverse, et jamais pour un
    bucket encore ouvert (il continuerait a etre incremente apres avoir ete lu).

    update_or_create sur (identifiant_arete, debut_intervalle) : si le
    nettoyage Redis echoue apres une ecriture reussie, le meme bucket sera
    recalcule et re-ecrit au passage suivant -- idempotent via la contrainte
    unique du modele, jamais une IntegrityError."""
    connexion = connexion_redis_telemetrie()
    maintenant = int(datetime.now(tz=dt_timezone.utc).timestamp())
    nb_flushes = 0

    for cle in connexion.smembers(ENSEMBLE_BUCKETS_ACTIFS):
        try:
            _prefixe, arete_str, bucket_str = cle.rsplit(':', 2)
            bucket_epoch = int(bucket_str)
        except ValueError:
            logger.warning('Cle accumulateur trafic malformee, ignoree: %s', cle)
            connexion.srem(ENSEMBLE_BUCKETS_ACTIFS, cle)
            continue

        if bucket_epoch + TAILLE_BUCKET_S + MARGE_FLUSH_S > maintenant:
            continue  # fenetre pas encore definitivement fermee -- laisse pour le passage suivant

        try:
            if _flusher_un_bucket(connexion, cle, int(arete_str), bucket_epoch):
                nb_flushes += 1
        except Exception:
            logger.exception('Echec du flush du bucket trafic %s, retente au passage suivant', cle)
            continue

    return nb_flushes


def _flusher_un_bucket(connexion, cle, identifiant_arete, bucket_epoch) -> bool:
    """Retourne True si un EchantillonVitesse a reellement ete ecrit (par
    opposition a une cle vide simplement nettoyee)."""
    champs = connexion.hgetall(cle)
    nombre = int(champs.get('nombre', 0))
    if nombre <= 0:
        connexion.delete(cle)
        connexion.srem(ENSEMBLE_BUCKETS_ACTIFS, cle)
        return False

    somme = float(champs['somme_vitesse'])
    debut_intervalle = datetime.fromtimestamp(bucket_epoch, tz=dt_timezone.utc)
    debut_local = debut_intervalle.astimezone(FUSEAU_TRAFIC)

    EchantillonVitesse.objects.update_or_create(
        identifiant_arete=identifiant_arete,
        debut_intervalle=debut_intervalle,
        defaults={
            # Decimal(str(...)) plutot que Decimal(somme / nombre) directement --
            # convertir un float en Decimal via le constructeur binaire reproduit
            # son imprecision (ex. Decimal(0.1) != Decimal('0.1')) ; le passage
            # par str l'evite. Cf. le bug deja rencontre sur ce meme genre de champ.
            'vitesse_moyenne': Decimal(str(round(somme / nombre, 2))),
            'nombre_echantillons': nombre,
            'jour_semaine': debut_local.weekday(),
            'heure_jour': debut_local.hour,
        },
    )
    # Nettoyage seulement apres succes de l'ecriture -- cf. docstring de la tache.
    connexion.delete(cle)
    connexion.srem(ENSEMBLE_BUCKETS_ACTIFS, cle)
    return True
