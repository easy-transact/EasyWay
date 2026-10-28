"""
ServiceItineraire : couche entre les vues et ClientRoutage. Traduit
Parametres/type_vehicule en options Valhalla, met en cache le resultat
(3 min -- le calcul d'itineraire est l'appel le plus couteux du systeme), et
normalise la reponse (Valhalla ou repli) en une forme stable pour le client.
"""

import hashlib
import json
import logging

from django.conf import settings
from django.core.cache import cache

from accounts.models import Parametres, TypeVehicule
from etat_routes.services import evitement
from etat_routes.services.score_trajet import completer as completer_etat_route

from ..models import NiveauTrafic, RoadClass
from . import duree_ville, retards_signalements, risque_itineraire, service_trafic
from .client_corridor_reference import ClientCorridorReference
from .client_valhalla import NOMBRE_MAX_ITINERAIRES, ClientValhalla
from .corridors import trouver_corridor

logger = logging.getLogger(__name__)

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
# Premiere version (spec ITINERAIRES_VOITURE_MOTO, section 5) : la moto ignore
# le trafic en direct, mesure surtout sur des voitures. A repasser a True avec
# une instance Valhalla moto dediee.
MOTO_TRAFIC_EN_DIRECT = False

# Cap d'origine : a l'arret ou au pas, le cap GPS du telephone n'a pas de
# sens. Transmis tel quel, il oblige Valhalla a partir dans une direction
# arbitraire, d'ou une boucle/un demi-tour des le depart (vu sur Douala ->
# Bafoussam). En dessous de cette vitesse, on l'ignore.
VITESSE_MIN_CAP_KMH = 10
# Vitesse inconnue (verifier_cap) : si l'itineraire sans cap est plus rapide
# d'au moins ce delai, le cap a force un detour -- on le laisse tomber.
GAIN_MIN_SANS_CAP_S = 120


def parametres_de(utilisateur):
    """Parametres du compte, crees au besoin : un compte cree par
    createsuperuser (staff) n'en a pas -- seule l'inscription normale les
    cree -- et le calcul d'itineraire repondait alors 500 (carte de test du
    back-office, 28/10)."""
    try:
        return utilisateur.parametres
    except Parametres.DoesNotExist:
        return Parametres.objects.get_or_create(utilisateur=utilisateur)[0]


def ajouter_reperes_sans_risque(candidat):
    """Points de repere des manoeuvres (places/services/reperes.py) : un
    echec ne doit jamais empecher de calculer l'itineraire."""
    from places.services.reperes import ajouter_reperes

    try:
        ajouter_reperes(candidat)
    except Exception:
        logger.exception('Points de repere indisponibles')
        for manoeuvre in candidat.get('manoeuvres') or []:
            manoeuvre['repere'] = None


class ServiceItineraire:
    def __init__(self, client=None):
        self.client = client or ClientValhalla()

    def calculer(
        self, depart, arrivee, utilisateur, eviter=None, cap_origine=None, alternatives=True, etapes=None,
        type_vehicule=None, vitesse_origine_kmh=None, verifier_cap=False,
    ) -> list[dict]:
        """depart/arrivee : (lat, lon). etapes (optionnel) : arrets
        intermediaires dans l'ordre de passage -- chaque paire consecutive
        devient un leg Valhalla distinct (cf. ClientValhalla._appeler), et
        chaque manoeuvre de fin de troncon intermediaire porte
        arrivee_etape_index dans le resultat normalise (voir _normaliser_trip).
        eviter : liste de (lat, lon) a exclure du graphe de routage (ex.
        position d'un incident) -- transmis a Valhalla via exclude_locations,
        jamais un simple reclassement des candidats : Valhalla replanifie
        reellement autour du point. cap_origine (0-359, optionnel) : cap du
        vehicule au depart, transmis comme heading Valhalla sur la premiere
        location -- sans ca, un recalcul en cours de route peut choisir
        l'arete la plus proche dans le mauvais sens (voie a sens unique/
        chaussee separee) et demarrer par un demi-tour immediat.
        alternatives=False : un seul itineraire (le recommande), reellement
        propage jusqu'a ClientValhalla -- pas juste tronque apres coup, pour
        eviter le cout des appels de variantes quand l'appelant n'en a pas
        besoin. Retourne une liste d'itineraires candidats normalises (voir
        _normaliser_trip), le premier etant recommande.
        vitesse_origine_kmh : sous VITESSE_MIN_CAP_KMH, cap_origine est ignore.
        verifier_cap (vitesse inconnue) : compare a un calcul sans cap et
        garde celui-ci s'il evite un detour (cf. GAIN_MIN_SANS_CAP_S)."""
        controle = settings.CONTROLE_CAP_ORIGINE  # cf. settings : False = cap transmis tel quel
        if controle and vitesse_origine_kmh is not None and vitesse_origine_kmh < VITESSE_MIN_CAP_KMH:
            cap_origine = None
        type_vehicule = type_vehicule or utilisateur.type_vehicule
        options = self._options_depuis_parametres(utilisateur, type_vehicule)
        costing = options['costing']
        # Fermetures confirmees (route barree, inondation) : exclues du calcul,
        # comme un point `avoid` (cf. retards_signalements.fermetures_a_eviter).
        eviter = list(eviter or []) + retards_signalements.fermetures_a_eviter(depart, arrivee, etapes, costing)
        if eviter:
            # cf. https://valhalla.github.io/valhalla/api/turn-by-turn/api-reference/#exclude-locations
            # -- cle top-level du payload /route, pas une costing_option.
            options['exclude_locations'] = [{'lat': lat, 'lon': lon} for lat, lon in eviter]

        trips = self._trips(depart, arrivee, options, cap_origine, alternatives, etapes, eviter)
        if controle and verifier_cap and cap_origine is not None and trips and not trips[0].get('degrade'):
            direct = self._trips(depart, arrivee, options, None, False, etapes, eviter)
            if direct and direct[0]['summary']['time'] + GAIN_MIN_SANS_CAP_S < trips[0]['summary']['time']:
                trips = self._trips(depart, arrivee, options, None, alternatives, etapes, eviter) if alternatives else direct

        trafic_en_direct = 'current' in options['costing_options'][costing]['speed_types']
        penalite = evitement.penalite_appliquee if parametres_de(utilisateur).eviter_routes_degradees else None
        candidats = self._evaluer(trips, 0, type_vehicule, costing, trafic_en_direct)
        candidats = risque_itineraire.classer(candidats, penalite)

        # La route choisie traverse un danger serieux et confirme (accident...) :
        # si aucune variante ne l'evite, on en demande une a Valhalla en
        # excluant ce point, puis on compare (un appel de plus, seulement dans ce cas).
        if candidats and not candidats[0].get('degrade'):
            dangers = risque_itineraire.a_contourner(candidats[0], [depart, arrivee, *(etapes or [])])
            if dangers:
                options_contournement = {
                    **options, 'exclude_locations': [{'lat': lat, 'lon': lon} for lat, lon in eviter + dangers],
                }
                contournement = self._trips(
                    depart, arrivee, options_contournement, cap_origine, False, etapes, eviter + dangers,
                )
                connues = {c['geometrie'] for c in candidats}
                nouveaux = [
                    c for c in self._evaluer(contournement, len(candidats), type_vehicule, costing, trafic_en_direct)
                    if c['geometrie'] not in connues and not c.get('degrade')
                ]
                if nouveaux:
                    candidats = risque_itineraire.classer(candidats + nouveaux, penalite)
        return candidats[:NOMBRE_MAX_ITINERAIRES] if alternatives else candidats[:1]

    def _evaluer(self, trips, premier_index, type_vehicule, costing, trafic_en_direct):
        """Trips Valhalla -> candidats normalises, avec reperes, retards des
        signalements et risque. Retards et risque recalcules a chaque appel (pas
        mis en cache : un signalement peut apparaitre ou expirer entre deux calculs)."""
        candidats = [self._normaliser_trip(trip, premier_index + index) for index, trip in enumerate(trips)]
        for candidat in candidats:
            candidat['type_vehicule'] = type_vehicule or TypeVehicule.VOITURE
            ajouter_reperes_sans_risque(candidat)
        candidats = retards_signalements.appliquer(candidats, costing, trafic_en_direct)
        return risque_itineraire.evaluer(candidats, costing)

    def _trips(self, depart, arrivee, options, cap_origine, alternatives, etapes, eviter):
        """Trips Valhalla bruts, en cache DUREE_CACHE_S."""
        cle = self._cle_cache(depart, arrivee, options, cap_origine, alternatives, etapes)
        trips = cache.get(cle)
        if trips is None:
            client = self.client
            # Corridor de reference (cf. trips/models.py:CorridorReference) :
            # geometrie/duree Valhalla verifiees proches pour ce genre de
            # route en distance mais tres optimistes en duree -- utilise a la
            # place du client normal quand depart/arrivee correspondent a un
            # corridor connu. eviter/etapes non supportes avec un corridor en
            # v1 (complexite de decoupage supplementaire, hors perimetre) --
            # on retombe alors sur le client normal.
            if not eviter and not etapes:
                corridor = trouver_corridor(depart, arrivee)
                if corridor is not None:
                    client = ClientCorridorReference(corridor, client_valhalla=self.client)
            trips = client.calculer_itineraires(
                depart, arrivee, options, cap_origine=cap_origine, alternatives=alternatives, etapes=etapes
            )
            cache.set(cle, trips, timeout=DUREE_CACHE_S)
        return trips

    def _options_depuis_parametres(self, utilisateur, type_vehicule=None) -> dict:
        costing = COSTING_PAR_VEHICULE.get(type_vehicule or utilisateur.type_vehicule, COSTING_DEFAUT)
        parametres = parametres_de(utilisateur)

        costing_options = {
            # 0.0 = evite completement, 1.0 = autorise sans penalite. use_tracks
            # est l'approximation la plus proche disponible pour "eviter les
            # routes non bitumees" -- Valhalla ne filtre pas sur le tag OSM
            # surface= directement via costing_options.
            'use_tolls': 0.0 if parametres.eviter_peages else 1.0,
            'use_tracks': 0.0 if parametres.eviter_non_bitumees else 1.0,
            # Vitesses par ordre de priorite (Valhalla 3.5.1, GraphTile::GetSpeed) :
            # trafic temps reel publie dans traffic.tar (cf. trafic_temps_reel.py),
            # puis profils historiques, puis vitesse de la classe de route.
            # Pas de date_time dans la requete : le temps reel s'applique sans
            # (seconds_from_now=0), alors qu'un date_time bascule Valhalla sur
            # un A* dependant du temps qui ne produit plus AUCUNE alternate
            # (verifie en local). Contrepartie : l'historique ('predicted') ne
            # sert qu'aux departs differes, qui passeraient un date_time.
            'speed_types': ['current', 'predicted', 'constrained', 'freeflow'],
        }
        if costing == 'motorcycle':
            # Moto (Valhalla "motorcycle", en essai) : rues etroites et pistes
            # acceptees, grands axes un peu moins. Reglages de depart a caler.
            costing_options.update({
                'use_highways': 0.5, 'use_trails': 0.3, 'use_living_streets': 0.5,
                'use_tracks': 0.0 if parametres.eviter_non_bitumees else 0.5,
            })
            if not MOTO_TRAFIC_EN_DIRECT:
                # traffic.tar est unique et mesure surtout des voitures : une moto
                # remonte les files, elle ne subit pas un bouchon comme une voiture.
                # Ses signalements passent par les retards (part moto reduite).
                costing_options['speed_types'] = ['predicted', 'constrained', 'freeflow']
        # eviter_intersections_difficiles : aucun levier Valhalla equivalent
        # (pas de notion de "complexite d'intersection" dans ses costing
        # options) -- delibrement non traduit plutot que faussement mappe.

        return {'costing': costing, 'costing_options': {costing: costing_options}}

    def _cle_cache(self, depart, arrivee, options, cap_origine, alternatives, etapes=None) -> str:
        brut = json.dumps(
            {
                'depart': depart, 'arrivee': arrivee, 'options': options,
                'cap_origine': cap_origine, 'alternatives': alternatives,
                'etapes': etapes or [],
            },
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
        legs = trip['legs']
        manoeuvres = []
        for indice_leg, leg in enumerate(legs):
            leg_manoeuvres = leg['maneuvers']
            for indice_m, m in enumerate(leg_manoeuvres):
                # dernier maneuver d'un troncon non final == arrivee a une etape
                # intermediaire -- marqueur demande pour "arrivee a l'arret N".
                arrivee_etape = (
                    indice_leg if indice_leg < len(legs) - 1 and indice_m == len(leg_manoeuvres) - 1 else None
                )
                manoeuvres.append((m, arrivee_etape))
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

        candidat = {
            'identifiant': hashlib.sha1(f"{geometrie}{index}".encode()).hexdigest()[:16],
            'libelle': f"Itineraire {index + 1}" if index else 'Itineraire recommande',
            'distance': round(trip['summary']['length'] * 1000),  # km -> m
            'duree': round(trip['summary']['time']),
            'duree_avec_trafic': trafic['duree_avec_trafic'],
            'niveau_trafic': trafic['niveau_trafic'],
            # Etat des routes (bon/degrade/mauvais/inconnu en metres), null si indisponible.
            'etat_route': completer_etat_route(trafic.get('etat_route'), round(trip['summary']['length'] * 1000)),
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
                    'arrivee_etape_index': arrivee_etape,
                }
                for m, arrivee_etape in manoeuvres
            ],
            'degrade': trip.get('degrade', False),
        }
        # Durees Valhalla trop optimistes en ville : correction selon l'heure,
        # sauf pour un corridor de reference (duree deja de confiance).
        if not trip.get('corridor'):
            duree_ville.corriger(candidat, trafic)
        return candidat
