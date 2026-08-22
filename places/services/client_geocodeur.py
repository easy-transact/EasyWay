from abc import ABC, abstractmethod


class ErreurGeocodage(Exception):
    """Le geocodeur externe (Nominatim, Photon, ou autre) est indisponible ou a echoue."""


class ClientRecherche(ABC):
    """Recherche par texte. Contrat separe de ClientInverse : Photon n'a pas
    d'endpoint inverse, un client qui ne fait QUE de la recherche ne doit pas
    etre force d'en simuler un (cf. ClientRoutage/ClientValhalla pour le
    principe general du repli abstrait)."""

    @abstractmethod
    def rechercher(self, q: str, autour: tuple[float, float] | None) -> list[dict]:
        """Retourne une liste normalisee : id, libelle, sous_libelle,
        categorie, lat, lon, distance_m, source."""

    @abstractmethod
    def replier_recherche(self, q: str) -> list[dict]:
        """Repli quand le geocodeur est indisponible. Liste vide est un choix
        legitime : l'appelant (RechercheView) a deja les resultats locaux
        PostGIS, la fusion se degrade juste sans l'apport externe."""


class ClientInverse(ABC):
    """Geocodage inverse (coordonnees -> lieu)."""

    @abstractmethod
    def inverser(self, lat: float, lon: float) -> dict | None:
        """Retourne un lieu normalise (meme forme que ClientRecherche) ou
        None si rien trouve."""

    @abstractmethod
    def replier_inverse(self, lat: float, lon: float) -> dict | None:
        """Repli quand le geocodeur est indisponible. None est un choix
        legitime : l'appelant (InverseView) retombe sur le lieu approuve le
        plus proche en local."""
