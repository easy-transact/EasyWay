"""
ProducteurEvenements : interface de publication sur un flux d'evenements.
Redis Streams est la seule implementation aujourd'hui (P5), mais un futur
passage a Kafka (charge plus lourde, retention plus longue) ne doit changer
qu'une config/un import -- jamais les appelants (POST /api/telemetrie/positions/,
et tout futur producteur similaire).
"""

import json
from abc import ABC, abstractmethod

import redis
from django.conf import settings

LONGUEUR_MAX_DEFAUT = 200_000  # borne la memoire du flux si le consommateur tombe en panne ou prend du retard
FLUX_POSITIONS = 'telemetrie:positions'


def connexion_redis_telemetrie():
    """Connexion partagee producteur/consommateur -- decode_responses=True pour
    manipuler des str plutot que des bytes de part et d'autre du flux."""
    return redis.from_url(settings.TELEMETRIE_REDIS_URL, decode_responses=True)


class ProducteurEvenements(ABC):
    @abstractmethod
    def publier(self, flux: str, evenement: dict) -> None:
        """Publie `evenement` (dict JSON-serialisable) sur le flux nomme `flux`."""


class ProducteurRedisStreams(ProducteurEvenements):
    """XADD avec MAXLEN approximatif : borne la croissance du flux si le
    consommateur (P5 partie 3, pas encore construit) tombe en panne -- jamais
    de flux illimite en memoire Redis. `evenement` est encode en un seul champ
    JSON plutot qu'aplati sur plusieurs champs XADD : garde le producteur
    agnostique de la forme du payload (pas de contrainte "toutes les valeurs
    sont des chaines" de XADD a gerer ici), le consommateur json.loads()."""

    def __init__(self, connexion=None, longueur_max=LONGUEUR_MAX_DEFAUT):
        self.connexion = connexion or connexion_redis_telemetrie()
        self.longueur_max = longueur_max

    def publier(self, flux: str, evenement: dict) -> None:
        self.connexion.xadd(
            flux,
            {'donnees': json.dumps(evenement, default=str)},
            maxlen=self.longueur_max,
            approximate=True,
        )
