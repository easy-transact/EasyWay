"""
ClientPhoton : recherche uniquement (Photon n'a pas de geocodage inverse).
Meme disjoncteur que ClientValhalla/ClientNominatim, cle Redis dediee.
"""

import time

import requests
from django.conf import settings
from django.core.cache import cache

from .client_geocodeur import ClientRecherche, ErreurGeocodage

CLE_ECHECS = 'photon:disjoncteur:echecs'
CLE_OUVERT_JUSQU_A = 'photon:disjoncteur:ouvert_jusqu_a'
SEUIL_ECHECS = 3
DUREE_OUVERTURE_S = 30
FENETRE_COMPTAGE_ECHECS_S = 60


class DisjoncteurOuvert(Exception):
    """Interne -- rechercher() bascule sur replier_recherche() des qu'elle
    l'attrape, l'appelant ne la voit jamais."""


def _normaliser(feature: dict) -> dict:
    props = feature.get('properties', {})
    lon, lat = feature['geometry']['coordinates']
    sous_libelle = ', '.join(filter(None, [
        props.get('district') or props.get('suburb'),
        props.get('city') or props.get('town'),
    ]))
    return {
        'id': f"photon:{props.get('osm_type', '')}{props.get('osm_id', '')}",
        'libelle': props.get('name') or props.get('street') or props.get('osm_value', ''),
        'sous_libelle': sous_libelle,
        'categorie': props.get('osm_value') or props.get('osm_key', ''),
        'lat': lat,
        'lon': lon,
        'distance_m': None,
        'source': 'photon',
    }


class ClientPhoton(ClientRecherche):
    TIMEOUT_S = 5
    TENTATIVES = 2

    def rechercher(self, q, autour=None):
        try:
            self._verifier_disjoncteur()
            params = {'q': q, 'limit': 10, 'lang': 'fr'}
            if autour:
                params['lat'], params['lon'] = autour
            resultat = self._appeler_avec_retry(params)
        except (DisjoncteurOuvert, ErreurGeocodage):
            return self.replier_recherche(q)
        self._reinitialiser_echecs()
        return [_normaliser(f) for f in resultat.get('features', [])]

    def replier_recherche(self, q):
        return []

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

    def _appeler_avec_retry(self, params):
        derniere_erreur = None
        for _ in range(self.TENTATIVES):
            try:
                reponse = requests.get(f'{settings.PHOTON_URL}/api', params=params, timeout=self.TIMEOUT_S)
                reponse.raise_for_status()
                return reponse.json()
            except requests.RequestException as exc:
                derniere_erreur = exc
        self._enregistrer_echec()
        raise ErreurGeocodage(str(derniere_erreur))
