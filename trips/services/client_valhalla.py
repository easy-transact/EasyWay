"""
ClientValhalla : view -> ServiceItineraire -> ClientValhalla (jamais d'appel
direct a Valhalla depuis une vue). Le disjoncteur vit dans le cache Django
(Redis) plutot qu'en memoire de process, pour que son etat soit partage entre
workers/process.
"""

import copy
import math
import time

import requests
from django.conf import settings
from django.core.cache import cache

from .client_routage import ClientRoutage, ErreurRoutage

CLE_ECHECS = 'valhalla:disjoncteur:echecs'
CLE_OUVERT_JUSQU_A = 'valhalla:disjoncteur:ouvert_jusqu_a'
SEUIL_ECHECS = 3
DUREE_OUVERTURE_S = 30
FENETRE_COMPTAGE_ECHECS_S = 60

VITESSE_REPLI_KMH = 25  # vitesse urbaine moyenne prudente, pour l'estimation degradee


def _distance_haversine_m(depart, arrivee):
    lat1, lon1 = map(math.radians, depart)
    lat2, lon2 = map(math.radians, arrivee)
    d_lat = lat2 - lat1
    d_lon = lon2 - lon1
    a = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


class DisjoncteurOuvert(Exception):
    """Leve en interne uniquement -- calculer_itineraires() bascule sur
    replier() des qu'elle attrape ceci, l'appelant ne la voit jamais."""


def _forme_complete(trip):
    return ''.join(leg['shape'] for leg in trip['legs'])


class ClientValhalla(ClientRoutage):
    TIMEOUT_S = 5
    TENTATIVES = 2

    def calculer_itineraires(self, depart, arrivee, options):
        try:
            self._verifier_disjoncteur()
            trips = self._collecter_variantes(depart, arrivee, options)
        except (DisjoncteurOuvert, ErreurRoutage):
            return self.replier(depart, arrivee)
        self._reinitialiser_echecs()
        return trips

    def _collecter_variantes(self, depart, arrivee, options):
        """Le propre algorithme d'alternates de Valhalla est conservateur :
        il ne propose une deuxieme route que si elle est nettement differente
        de la meilleure (verifie empiriquement -- beaucoup de trajets courts
        ou a corridor unique n'en ont simplement pas). Pour maximiser les
        chances d'obtenir jusqu'a 3 options reelles, on interroge aussi avec
        un objectif de distance (shortest) plutot que de temps -- un vrai
        changement de critere d'optimisation, pas une nouvelle tentative du
        meme algorithme. On deduplique sur la geometrie complete : si les
        deux appels convergent vers le meme trace, ce n'est pas une option
        distincte, pas la peine de faire semblant."""
        trips = self._appeler_avec_retry(depart, arrivee, options, alternates=2)

        if len(trips) < 3:
            options_distance = copy.deepcopy(options)
            costing = options_distance.get('costing', 'auto')
            options_distance.setdefault('costing_options', {}).setdefault(costing, {})['shortest'] = True
            try:
                variante = self._appeler_avec_retry(depart, arrivee, options_distance, alternates=0)
            except ErreurRoutage:
                variante = []
            formes_connues = {_forme_complete(t) for t in trips}
            trips += [v for v in variante if _forme_complete(v) not in formes_connues]

        return trips[:3]

    def replier(self, depart, arrivee):
        distance_m = _distance_haversine_m(depart, arrivee)
        duree_s = distance_m / (VITESSE_REPLI_KMH * 1000 / 3600)
        # Import local : evite un cycle (trips.polyline n'a pas besoin de
        # connaitre trips.services, seul ce module a besoin des deux).
        from trips.polyline import encoder_polyline6

        shape = encoder_polyline6([(depart[1], depart[0]), (arrivee[1], arrivee[0])])
        return [{
            'summary': {'length': round(distance_m / 1000, 2), 'time': round(duree_s)},
            'legs': [{'shape': shape, 'maneuvers': []}],
            'degrade': True,
        }]

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

    def _appeler_avec_retry(self, depart, arrivee, options, alternates):
        derniere_erreur = None
        for _ in range(self.TENTATIVES):
            try:
                return self._appeler(depart, arrivee, options, alternates)
            except requests.RequestException as exc:
                derniere_erreur = exc
        self._enregistrer_echec()
        raise ErreurRoutage(str(derniere_erreur))

    def _appeler(self, depart, arrivee, options, alternates):
        payload = {
            'locations': [
                {'lat': depart[0], 'lon': depart[1]},
                {'lat': arrivee[0], 'lon': arrivee[1]},
            ],
            'units': 'kilometers',
            'language': 'fr-FR',
            'alternates': alternates,
            **options,
        }
        reponse = requests.post(f'{settings.VALHALLA_URL}/route', json=payload, timeout=self.TIMEOUT_S)
        reponse.raise_for_status()
        data = reponse.json()
        return [data['trip']] + [alt['trip'] for alt in data.get('alternates', [])]
