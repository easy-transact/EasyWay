"""
ClientValhalla : view -> ServiceItineraire -> ClientValhalla (jamais d'appel
direct a Valhalla depuis une vue). Le disjoncteur (partage avec client_meili.py,
cf. disjoncteur.py) vit dans le cache Django (Redis) plutot qu'en memoire de
process, pour que son etat soit partage entre workers/process.
"""

import copy

import requests
from django.conf import settings

from . import disjoncteur
from .client_routage import ClientRoutage, ErreurRoutage
from .disjoncteur import DisjoncteurOuvert
from .geo import distance_haversine_m

VITESSE_REPLI_KMH = 25  # vitesse urbaine moyenne prudente, pour l'estimation degradee


def _forme_complete(trip):
    return ''.join(leg['shape'] for leg in trip['legs'])


class ClientValhalla(ClientRoutage):
    TIMEOUT_S = 5
    TENTATIVES = 2

    def calculer_itineraires(self, depart, arrivee, options, cap_origine=None, alternatives=True, etapes=None):
        try:
            disjoncteur.verifier()
            if alternatives:
                trips = self._collecter_variantes(depart, arrivee, options, cap_origine, etapes)
            else:
                # alternatives=False reellement honore : un seul appel Valhalla
                # (alternates=0), jamais le deuxieme appel "shortest" de
                # _collecter_variantes -- pas seulement trips[:1] apres coup,
                # qui aurait quand meme paye le cout des variantes.
                trips = self._appeler_avec_retry(
                    depart, arrivee, options, alternates=0, cap_origine=cap_origine, etapes=etapes
                )
        except (DisjoncteurOuvert, ErreurRoutage):
            return self.replier(depart, arrivee, etapes)
        disjoncteur.reinitialiser_echecs()
        return trips

    def _collecter_variantes(self, depart, arrivee, options, cap_origine=None, etapes=None):
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
        trips = self._appeler_avec_retry(
            depart, arrivee, options, alternates=2, cap_origine=cap_origine, etapes=etapes
        )

        if len(trips) < 3:
            options_distance = copy.deepcopy(options)
            costing = options_distance.get('costing', 'auto')
            options_distance.setdefault('costing_options', {}).setdefault(costing, {})['shortest'] = True
            try:
                variante = self._appeler_avec_retry(
                    depart, arrivee, options_distance, alternates=0, cap_origine=cap_origine, etapes=etapes
                )
            except ErreurRoutage:
                variante = []
            formes_connues = {_forme_complete(t) for t in trips}
            trips += [v for v in variante if _forme_complete(v) not in formes_connues]

        return trips[:3]

    def replier(self, depart, arrivee, etapes=None):
        # Import local : evite un cycle (trips.polyline n'a pas besoin de
        # connaitre trips.services, seul ce module a besoin des deux).
        from trips.polyline import encoder_polyline6

        points = [depart, *(etapes or []), arrivee]
        legs = []
        distance_totale_m = 0
        for point_a, point_b in zip(points, points[1:]):
            distance_m = distance_haversine_m(point_a, point_b)
            distance_totale_m += distance_m
            shape = encoder_polyline6([(point_a[1], point_a[0]), (point_b[1], point_b[0])])
            legs.append({'shape': shape, 'maneuvers': []})

        duree_s = distance_totale_m / (VITESSE_REPLI_KMH * 1000 / 3600)
        return [{
            'summary': {'length': round(distance_totale_m / 1000, 2), 'time': round(duree_s)},
            'legs': legs,
            'degrade': True,
        }]

    def _appeler_avec_retry(self, depart, arrivee, options, alternates, cap_origine=None, etapes=None):
        derniere_erreur = None
        for _ in range(self.TENTATIVES):
            try:
                return self._appeler(depart, arrivee, options, alternates, cap_origine, etapes)
            except requests.RequestException as exc:
                derniere_erreur = exc
        disjoncteur.enregistrer_echec()
        raise ErreurRoutage(str(derniere_erreur))

    def _appeler(self, depart, arrivee, options, alternates, cap_origine=None, etapes=None):
        origine = {'lat': depart[0], 'lon': depart[1]}
        if cap_origine is not None:
            # cf. https://valhalla.github.io/valhalla/api/turn-by-turn/api-reference/#locations
            # -- heading_tolerance laisse au defaut Valhalla (60), pas mesure sur donnees reelles.
            origine['heading'] = cap_origine

        locations = [origine]
        locations += [{'lat': lat, 'lon': lon} for lat, lon in (etapes or [])]
        locations.append({'lat': arrivee[0], 'lon': arrivee[1]})

        payload = {
            'locations': locations,
            'units': 'kilometers',
            'language': 'fr-FR',
            'alternates': alternates,
            **options,
        }
        reponse = requests.post(f'{settings.VALHALLA_URL}/route', json=payload, timeout=self.TIMEOUT_S)
        reponse.raise_for_status()
        data = reponse.json()
        return [data['trip']] + [alt['trip'] for alt in data.get('alternates', [])]
