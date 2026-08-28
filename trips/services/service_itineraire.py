"""
ServiceItineraire : couche entre les vues et ClientRoutage. Traduit
Parametres/type_vehicule en options Valhalla, met en cache le resultat
(3 min -- le calcul d'itineraire est l'appel le plus couteux du systeme), et
normalise la reponse (Valhalla ou repli) en une forme stable pour le client.
"""

import hashlib
import json

from django.core.cache import cache

from accounts.models import TypeVehicule

from ..models import NiveauTrafic, RoadClass
from . import service_trafic
from .client_valhalla import ClientValhalla

DUREE_CACHE_S = 180

# Valhalla n'a pas de profil par type de vehicule du domaine -- rapprochement
# le plus proche disponible cote moteur de routage.
COSTING_PAR_VEHICULE = {
    TypeVehicule.VOITURE: 'auto',
    TypeVehicule.TAXI: 'auto',
    TypeVehicule.MOTO: 'motorcycle',
    TypeVehicule.CAMION: 'truck',
    TypeVehicule.UTILITAIRE: 'truck',
    TypeVehicule.BUS: 'bus',
}
COSTING_DEFAUT = 'auto'


class ServiceItineraire:
    def __init__(self, client=None):
        self.client = client or ClientValhalla()

    def calculer(self, depart, arrivee, utilisateur, eviter=None, cap_origine=None) -> list[dict]:
        """depart/arrivee : (lat, lon). eviter : liste de (lat, lon) a exclure
        du graphe de routage (ex. position d'un incident) -- transmis a
        Valhalla via exclude_locations, jamais un simple reclassement des
        candidats : Valhalla replanifie reellement autour du point.
        cap_origine (0-359, optionnel) : cap du vehicule au depart, transmis
        comme heading Valhalla sur la premiere location -- sans ca, un
        recalcul en cours de route peut choisir l'arete la plus proche dans
        le mauvais sens (voie a sens unique/chaussee separee) et demarrer par
        un demi-tour immediat. Retourne une liste d'itineraires candidats
        normalises (voir _normaliser_trip), le premier etant recommande."""
        options = self._options_depuis_parametres(utilisateur)
        if eviter:
            # cf. https://valhalla.github.io/valhalla/api/turn-by-turn/api-reference/#exclude-locations
            # -- cle top-level du payload /route, pas une costing_option.
            options['exclude_locations'] = [{'lat': lat, 'lon': lon} for lat, lon in eviter]
        cle = self._cle_cache(depart, arrivee, options, cap_origine)

        trips = cache.get(cle)
        if trips is None:
            trips = self.client.calculer_itineraires(depart, arrivee, options, cap_origine=cap_origine)
            cache.set(cle, trips, timeout=DUREE_CACHE_S)

        return [self._normaliser_trip(trip, index) for index, trip in enumerate(trips)]

    def _options_depuis_parametres(self, utilisateur) -> dict:
        costing = COSTING_PAR_VEHICULE.get(utilisateur.type_vehicule, COSTING_DEFAUT)
        parametres = utilisateur.parametres

        costing_options = {
            # 0.0 = evite completement, 1.0 = autorise sans penalite. use_tracks
            # est l'approximation la plus proche disponible pour "eviter les
            # routes non bitumees" -- Valhalla ne filtre pas sur le tag OSM
            # surface= directement via costing_options.
            'use_tolls': 0.0 if parametres.eviter_peages else 1.0,
            'use_tracks': 0.0 if parametres.eviter_non_bitumees else 1.0,
        }
        # eviter_intersections_difficiles : aucun levier Valhalla equivalent
        # (pas de notion de "complexite d'intersection" dans ses costing
        # options) -- delibrement non traduit plutot que faussement mappe.

        return {'costing': costing, 'costing_options': {costing: costing_options}}

    def _cle_cache(self, depart, arrivee, options, cap_origine) -> str:
        brut = json.dumps(
            {'depart': depart, 'arrivee': arrivee, 'options': options, 'cap_origine': cap_origine},
            sort_keys=True,
        )
        return 'itineraire:' + hashlib.sha256(brut.encode()).hexdigest()

    # Pattern de detection des routes nationales camerounaises dans les noms
    # de voies OSM -- couvre : N1, N3, RN4, N-1, Nationale 1, etc.
    # Intentionnellement large : un faux positif (NATIONALE sur une rue
    # ordinaire nommee par coincidence) est moins grave qu'un faux negatif
    # (URBAIN sur la N3 Douala-Yaounde). Repli toujours vers URBAIN.
    _NATIONALE_RE = __import__('re').compile(
        r'\b(R?N[-\s]?\d+|[Nn]ationale\s*\d*)\b'
    )

    def _traduire_road_class(self, manoeuvre: dict) -> str:
        """Deduit la classe de route a partir des champs disponibles dans
        les manoeuvres Valhalla. L'API /route ne renvoie pas de road_class
        direct par manoeuvre -- on s'appuie sur :
          - highway (bool) : True uniquement sur voies express / 2x2 voies
            tagguees highway=motorway|motorway_link dans OSM.
          - street_names   : les nationales camerounaises portent un ref OSM
            (N3, RN1…) systematiquement remonte dans ce champ par Valhalla.
        Repli conservateur vers URBAIN pour toute valeur non reconnue --
        le client ne doit jamais annoncer une limite superieure par defaut.
        """
        if manoeuvre.get('highway'):
            return RoadClass.AUTOROUTE
        for nom in manoeuvre.get('street_names', []):
            if self._NATIONALE_RE.search(nom):
                return RoadClass.NATIONALE
        return RoadClass.URBAIN

    def _normaliser_trip(self, trip: dict, index: int) -> dict:
        manoeuvres = [
            m for leg in trip['legs'] for m in leg['maneuvers']
        ]
        geometrie = ''.join(leg['shape'] for leg in trip['legs'])

        if trip.get('degrade'):
            # Ligne droite haversine (repli du disjoncteur Valhalla, cf.
            # client_valhalla.replier) : pas une geometrie routiere, evaluer
            # son trafic n'aurait aucun sens (et le disjoncteur, deja ouvert
            # puisqu'on est dans ce repli, ferait de toute facon echouer
            # evaluer_route immediatement).
            trafic = {'niveau_trafic': NiveauTrafic.NORMAL, 'duree_avec_trafic': None}
        else:
            trafic = service_trafic.evaluer_route(geometrie)

        return {
            'identifiant': hashlib.sha1(f"{geometrie}{index}".encode()).hexdigest()[:16],
            'libelle': f"Itineraire {index + 1}" if index else 'Itineraire recommande',
            'distance': round(trip['summary']['length'] * 1000),  # km -> m
            'duree': round(trip['summary']['time']),
            'duree_avec_trafic': trafic['duree_avec_trafic'],
            'niveau_trafic': trafic['niveau_trafic'],
            'geometrie': geometrie,
            'est_recommande': index == 0,
            'manoeuvres': [
                {
                    'type': str(m.get('type', '')),
                    'instruction': m.get('instruction', ''),
                    'instruction_vocale': m.get('verbal_post_transition_instruction') or m.get('instruction', ''),
                    'distance': round(m.get('length', 0) * 1000),
                    'duree': round(m.get('time', 0)),
                    'nom_voie': ', '.join(m.get('street_names', [])),
                    'road_class': self._traduire_road_class(m),
                }
                for m in manoeuvres
            ],
            'degrade': trip.get('degrade', False),
        }
