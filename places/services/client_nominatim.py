"""
ClientNominatim : recherche/inverse externes, avec le meme disjoncteur que
ClientValhalla (etat dans Redis, ouvre apres des echecs repetes, se
reinitialise sur succes) -- meme discipline, service different.
"""

import re
import time

import requests
from django.conf import settings
from django.core.cache import cache

from .client_geocodeur import ClientInverse, ClientRecherche, ErreurGeocodage

# Sur les donnees OSM du Cameroun, la granularite de `address.city` varie
# selon la ville : pour Yaounde, city="Communaute urbaine de Yaounde"
# directement ; pour Douala, city="Douala I" (arrondissement) et le nom
# usuel est dans `municipality`="Communaute urbaine de Douala". D'ou l'ordre
# de priorite ci-dessous et le retrait du prefixe administratif -- verifie
# en direct sur ces deux villes, pas garanti pour le reste du pays.
PREFIXE_COMMUNAUTE_URBAINE = re.compile(r'^communaut[ée] urbaine de\s+', re.IGNORECASE)

CLE_ECHECS = 'nominatim:disjoncteur:echecs'
CLE_OUVERT_JUSQU_A = 'nominatim:disjoncteur:ouvert_jusqu_a'
SEUIL_ECHECS = 3
DUREE_OUVERTURE_S = 30
FENETRE_COMPTAGE_ECHECS_S = 60


class DisjoncteurOuvert(Exception):
    """Interne -- rechercher()/inverser() basculent sur le repli des qu'elles
    l'attrapent, l'appelant ne la voit jamais."""


def _normaliser(objet: dict) -> dict:
    adresse = objet.get('address', {})
    ville = (
        adresse.get('municipality') or adresse.get('city')
        or adresse.get('town') or adresse.get('village') or ''
    )
    ville = PREFIXE_COMMUNAUTE_URBAINE.sub('', ville).strip()
    libelle = objet.get('name') or objet.get('display_name', '').split(',')[0]
    sous_libelle = ', '.join(filter(None, [
        adresse.get('suburb') or adresse.get('quarter'),
        ville,
    ]))
    return {
        'id': f"nominatim:{objet.get('place_id')}",
        'label': libelle,
        'sublabel': sous_libelle,
        'category': objet.get('type') or objet.get('class', ''),
        'lat': float(objet['lat']),
        'lon': float(objet['lon']),
        'distance_m': None,
        'source': 'nominatim',
        'city': ville,
    }


class ClientNominatim(ClientRecherche, ClientInverse):
    TIMEOUT_S = 5
    TENTATIVES = 2

    def rechercher(self, q, autour=None):
        try:
            self._verifier_disjoncteur()
            params = {'q': q, 'format': 'jsonv2', 'addressdetails': 1, 'limit': 10, 'countrycodes': 'cm'}
            if autour:
                lat, lon = autour
                # Boite large (~0.5 deg, ~55 km) en biais non-restrictif : privilegie
                # le voisinage sans exclure un resultat pertinent plus loin.
                params.update({
                    'viewbox': f'{lon - 0.5},{lat + 0.5},{lon + 0.5},{lat - 0.5}',
                    'bounded': 0,
                })
            resultats = self._appeler_avec_retry('/search', params)
        except (DisjoncteurOuvert, ErreurGeocodage):
            return self.replier_recherche(q)
        self._reinitialiser_echecs()
        return [_normaliser(r) for r in resultats]

    def inverser(self, lat, lon):
        try:
            self._verifier_disjoncteur()
            resultat = self._appeler_avec_retry(
                '/reverse', {'lat': lat, 'lon': lon, 'format': 'jsonv2', 'addressdetails': 1}
            )
        except (DisjoncteurOuvert, ErreurGeocodage):
            return self.replier_inverse(lat, lon)
        self._reinitialiser_echecs()
        if not resultat or 'error' in resultat:
            return None
        return _normaliser(resultat)

    def replier_recherche(self, q):
        return []

    def replier_inverse(self, lat, lon):
        return None

    def _verifier_disjoncteur(self):
        ouvert_jusqu_a = cache.get(CLE_OUVERT_JUSQU_A)
        if ouvert_jusqu_a is not None and time.time() < ouvert_jusqu_a:
            raise DisjoncteurOuvert()

    def _enregistrer_echec(self):
        echecs = (cache.get(CLE_ECHECS) or 0) + 1
        cache.set(CLE_ECHECS, echecs, timeout=FENETRE_COMPTAGE_ECHECS_S)
        if echecs >= SEUIL_ECHECS:
            cache.set(CLE_OUVERT_JUSQU_A, time.time() + DUREE_OUVERTURE_S, timeout=DUREE_OUVERTURE_S)

    def _reinitialiser_echecs(self):
        cache.delete(CLE_ECHECS)
        cache.delete(CLE_OUVERT_JUSQU_A)

    def _appeler_avec_retry(self, chemin, params):
        derniere_erreur = None
        for _ in range(self.TENTATIVES):
            try:
                reponse = requests.get(f'{settings.NOMINATIM_URL}{chemin}', params=params, timeout=self.TIMEOUT_S)
                reponse.raise_for_status()
                return reponse.json()
            except requests.RequestException as exc:
                derniere_erreur = exc
        self._enregistrer_echec()
        raise ErreurGeocodage(str(derniere_erreur))
