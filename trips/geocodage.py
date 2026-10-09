"""Remplace les libelles generiques envoyes par l'appli ("Votre position",
quand le depart est la position GPS du conducteur) par le nom reel du lieu,
via le geocodage inverse Nominatim. Lance en arriere-plan a la creation du
trajet (cf. TrajetListeCreationView.post) et rejouable sur l'existant par
`manage.py geocoder_trajets`."""

import logging

from places.services.client_nominatim import ClientNominatim

logger = logging.getLogger(__name__)

# Comparaison en minuscules, espaces retires. Valeurs envoyees par l'appli
# (fr/en) ; un libelle vide est aussi traite comme generique.
LIBELLES_GENERIQUES = {
    '', 'votre position', 'ma position', 'position actuelle', 'ma position actuelle',
    'your location', 'my location', 'current location',
}


def libelle_generique(libelle):
    return (libelle or '').strip().lower() in LIBELLES_GENERIQUES


def nom_du_lieu(lat, lon, client=None):
    """"Carrefour Ndokoti, Akwa, Douala" depuis une position, ou None si
    Nominatim est indisponible ou ne trouve rien."""
    resultat = (client or ClientNominatim()).inverser(lat, lon)
    if not resultat:
        return None
    morceaux = [resultat.get('label'), resultat.get('sublabel')]
    libelle = ', '.join(m for m in morceaux if m)
    return libelle[:500] or None


def geocoder_trajet(trajet, client=None):
    """Remplace les libelles generiques du trajet ; retourne les champs modifies."""
    client = client or ClientNominatim()
    champs = []
    for champ, position in (('libelle_origine', trajet.position_origine),
                            ('libelle_destination', trajet.position_destination)):
        if not libelle_generique(getattr(trajet, champ)):
            continue
        nom = nom_du_lieu(position.y, position.x, client)
        if nom:
            setattr(trajet, champ, nom)
            champs.append(champ)
    if champs:
        trajet.save(update_fields=champs)
    return champs


def lancer_geocodage_trajet(trajet_id):
    """Tache Celery si le broker repond ; sinon on journalise et on laisse
    `geocoder_trajets` rattraper plus tard (la creation du trajet ne doit pas
    echouer pour un libelle)."""
    from .tasks import geocoder_libelles_trajet

    try:
        geocoder_libelles_trajet.delay(str(trajet_id))
    except Exception:  # broker indisponible
        logger.warning('Geocodage du trajet %s non planifie (broker indisponible)', trajet_id, exc_info=True)
