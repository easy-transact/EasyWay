from abc import ABC, abstractmethod


class ErreurRoutage(Exception):
    """Le moteur de routage (Valhalla ou autre) est indisponible ou a echoue."""


class ClientRoutage(ABC):
    """Contrat que tout moteur de routage doit respecter. `replier` est
    abstraite -- volontairement -- pour qu'un futur client (ex. un second
    fournisseur en cas de bascule) ne puisse pas oublier de definir une
    reponse degradee et laisser ServiceItineraire planter en silence."""

    @abstractmethod
    def calculer_itineraires(
        self, depart: tuple[float, float], arrivee: tuple[float, float], options: dict,
        cap_origine: int | None = None, alternatives: bool = True,
    ) -> list[dict]:
        """depart/arrivee : (lat, lon). cap_origine (0-359, optionnel) : cap
        du vehicule au depart, a transmettre comme heading sur la premiere
        location si le moteur le supporte. alternatives=False : un seul trip
        (le recommande), sans les appels/couts supplementaires que la
        recherche de variantes implique -- honore reellement, pas juste
        tronque apres coup. Retourne une liste de trips au format Valhalla
        (summary.length/time, legs[].shape, legs[].maneuvers)."""

    @abstractmethod
    def replier(self, depart: tuple[float, float], arrivee: tuple[float, float]) -> list[dict]:
        """Reponse degradee quand le moteur de routage est indisponible --
        doit rester au meme format que calculer_itineraires (avec 'degrade': True)
        pour que le code appelant n'ait pas a distinguer les deux cas."""
