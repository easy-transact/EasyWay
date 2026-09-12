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
        etapes: list[tuple[float, float]] | None = None,
    ) -> list[dict]:
        """depart/arrivee : (lat, lon). etapes (optionnel) : arrets
        intermediaires dans l'ordre de passage, entre depart et arrivee --
        chaque paire consecutive (depart, etape[0], ..., arrivee) devient une
        location Valhalla, donc un leg distinct dans la reponse. cap_origine
        (0-359, optionnel) : cap du vehicule au depart, a transmettre comme
        heading sur la premiere location si le moteur le supporte.
        alternatives=False : un seul trip (le recommande), sans les appels/
        couts supplementaires que la recherche de variantes implique --
        honore reellement, pas juste tronque apres coup. Retourne une liste
        de trips au format Valhalla (summary.length/time, legs[].shape,
        legs[].maneuvers) -- un trip a `len(etapes) + 1` legs si des etapes
        sont fournies."""

    @abstractmethod
    def replier(
        self, depart: tuple[float, float], arrivee: tuple[float, float],
        etapes: list[tuple[float, float]] | None = None,
    ) -> list[dict]:
        """Reponse degradee quand le moteur de routage est indisponible --
        doit rester au meme format que calculer_itineraires (avec 'degrade': True)
        pour que le code appelant n'ait pas a distinguer les deux cas. Avec
        des etapes, trace un segment droit par troncon (depart->etape[0],
        etape[0]->etape[1], ..., ->arrivee) plutot qu'une seule ligne directe
        qui les ignorerait."""
