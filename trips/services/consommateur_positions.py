"""
ConsommateurPositions (Fig. 11 et 14) : XREADGROUP sur le flux Redis Streams
alimente par ProducteurEvenements, regroupe par trajet, fait matcher chaque
groupe par Meili (map-matching), accumule les vitesses observees par arete
dans Redis. Ne persiste rien en Postgres -- seul un flush periodique (a batir,
P5 partie 4) lira cet accumulateur pour ecrire EchantillonVitesse.

Trois points d'attention (cf. discussion) :

- XAUTOCLAIM recupere a la fois les messages orphelins (consommateur mort en
  cours de traitement) ET, en pratique, les messages laisses sciemment en
  pending par un echec Meili (cf. _traiter_trajet) : le meme mecanisme sert
  de nouvelle tentative avec recul naturel (SEUIL_INACTIVITE_MS), sans file
  de retry separee.
- L'acquittement (XACK) n'intervient qu'apres l'accumulation des vitesses,
  jamais a la lecture : un crash entre XREADGROUP et l'ecriture Redis laisse
  le message en pending, XAUTOCLAIM le represente au passage suivant.
- Une correspondance Meili vide (edges=[] ou matched_points tous non-apparies)
  est un resultat normal -- on acquitte quand meme, rejouer ne changera rien
  a une trace trop bruitee pour etre matchee.
"""

import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime

import redis

from etat_routes.services.reglages import reglages
from etat_routes.services.troncons import ATTRIBUTS_TRONCON, enregistrer_passages

from .client_meili import ErreurMeili, tracer
from .disjoncteur import DisjoncteurOuvert
from .geo import distance_haversine_m
from .producteur_evenements import FLUX_POSITIONS, connexion_redis_telemetrie

logger = logging.getLogger(__name__)

NOM_GROUPE = 'consommateurs-trafic'
TAILLE_LOT = 200
SEUIL_INACTIVITE_MS = 30_000  # au-dela, un message pending est repris (crash du consommateur ou echec Meili precedent)
DUREE_ACCUMULATEUR_S = 20 * 60  # marge large avant le flush periodique (5 min, P5 partie 4) -- purge seule si ce flush ne tourne pas encore
VITESSE_MAX_PLAUSIBLE_KMH = 180  # au-dela, artefact de saut GPS plutot qu'une vitesse reelle -- seuil de bon sens, a affiner avec des donnees reelles
# Spec TRAFIC_VALHALLA, section 4 : vitesse aberrante au-dela de 1,5 fois la
# vitesse de la classe de route (road_class Valhalla ; autres classes : 60).
VITESSE_CLASSE_KMH = {'motorway': 110, 'trunk': 90}
VITESSE_CLASSE_DEFAUT_KMH = 60
FACTEUR_VITESSE_ABERRANTE = 1.5
# Depart et stationnement : on y roule au pas quelle que soit la circulation.
MARGE_DEPART_ARRIVEE_M = 200
# Arret plus long que ca (sans avancer) : pause (livraison, panne), pas un
# bouchon -- l'intervalle est ignore plutot que de compter 0 km/h a tort.
DUREE_ARRET_MAX_S = 3 * 60
VEHICULES_MESURANT_LE_TRAFIC = {'VOITURE', 'TAXI'}  # cf. _infos_trajet
PREFIXE_ACCUMULATEUR = 'trafic:accumulateur'
ENSEMBLE_BUCKETS_ACTIFS = 'trafic:buckets:actifs'
TAILLE_BUCKET_S = 5 * 60
PREFIXE_CHAMP_TRAJET = 't:'
# Sens de parcours de l'arete sur la voie OSM : avec edge.way_id (deja dans
# ATTRIBUTS_TRONCON), cle stable d'une generation de tuiles a l'autre.
# Dans cet ordre : retires depuis la fin si Valhalla les refuse (cf. client_meili).
# Noeuds OSM de debut/fin d'arete : Valhalla >= 3.6, tuiles construites avec
# mjolnir.keep_osm_node_ids -- troncon entre deux carrefours, cle la plus fine.
ATTRIBUTS_CLE_STABLE = ['edge.forward', 'edge.begin_osm_node_id', 'edge.end_osm_node_id']


def noeuds_osm(arete):
    """(noeud OSM de debut, noeud OSM de fin) de l'arete, ou None (Valhalla
    < 3.6 ou tuiles sans keep_osm_node_ids). Format de trace_attributes :
    edge.node_id (debut) et edge.end_node.node_id (fin)."""
    debut = arete.get('node_id')
    fin = (arete.get('end_node') or {}).get('node_id')
    if debut is None or fin is None:
        return None
    return int(debut), int(fin)


def cle_stable(arete):
    """(way_id, sens_avant) de l'arete, ou None si Valhalla ne les fournit pas."""
    way_id, avant = arete.get('way_id'), arete.get('forward')
    if way_id is None or avant is None:
        return None
    return int(way_id), bool(avant)


def apprendre_correspondances(edges):
    """Memorise GraphId -> (voie, sens) pour les aretes de ce lot (cf.
    trips.models.CorrespondanceArete) : une seule requete, upsert."""
    from django.utils import timezone

    from ..models import CorrespondanceArete

    maintenant = timezone.now()
    lignes = {}
    for arete in edges:
        cle = cle_stable(arete)
        if cle is not None and arete.get('id') is not None:
            noeuds = noeuds_osm(arete) or (None, None)
            lignes[arete['id']] = CorrespondanceArete(
                graph_id=arete['id'], way_id=cle[0], sens_avant=cle[1],
                noeud_debut_osm=noeuds[0], noeud_fin_osm=noeuds[1], vu_le=maintenant,
            )
    if lignes:
        CorrespondanceArete.objects.bulk_create(
            lignes.values(), update_conflicts=True, unique_fields=['graph_id'],
            update_fields=['way_id', 'sens_avant', 'noeud_debut_osm', 'noeud_fin_osm', 'vu_le'],
        )


class ConsommateurPositions:
    def __init__(self, connexion=None, nom_consommateur='worker'):
        self.connexion = connexion or connexion_redis_telemetrie()
        self.nom_consommateur = nom_consommateur
        self._assurer_groupe()

    def _assurer_groupe(self):
        try:
            self.connexion.xgroup_create(FLUX_POSITIONS, NOM_GROUPE, id='0', mkstream=True)
        except redis.ResponseError as exc:
            if 'BUSYGROUP' not in str(exc):
                raise

    def consommer(self) -> int:
        """Une passe : reclame les messages pending abandonnes/a reessayer,
        lit un lot de nouveaux messages, traite le tout groupe par trajet.
        Retourne le nombre de messages lus (traites, pas necessairement tous
        acquittes -- cf. _traiter_trajet)."""
        entrees = self._reclamer_orphelins() + self._lire_nouveaux()
        if not entrees:
            return 0

        par_trajet = defaultdict(list)
        for id_message, champs in entrees:
            evenement = json.loads(champs['donnees'])
            par_trajet[evenement['trajet_id']].append((id_message, evenement))

        ids_a_acquitter = []
        for paires in par_trajet.values():
            paires.sort(key=lambda p: p[1]['horodatage'])
            ids_a_acquitter += self._traiter_trajet(paires)

        if ids_a_acquitter:
            self.connexion.xack(FLUX_POSITIONS, NOM_GROUPE, *ids_a_acquitter)
        return len(entrees)

    def _reclamer_orphelins(self):
        try:
            _curseur, entrees, _supprimees = self.connexion.xautoclaim(
                FLUX_POSITIONS, NOM_GROUPE, self.nom_consommateur,
                min_idle_time=SEUIL_INACTIVITE_MS, start_id='0', count=TAILLE_LOT,
            )
        except redis.ResponseError:
            return []
        return list(entrees)

    def _lire_nouveaux(self):
        resultat = self.connexion.xreadgroup(
            NOM_GROUPE, self.nom_consommateur, {FLUX_POSITIONS: '>'}, count=TAILLE_LOT,
        )
        if not resultat:
            return []
        _flux, entrees = resultat[0]
        return list(entrees)

    def _traiter_trajet(self, paires):
        """Retourne les ids de message a acquitter. Sur echec Meili
        (DisjoncteurOuvert/ErreurMeili), retourne [] deliberement : ces
        messages restent en pending et seront repris par XAUTOCLAIM une fois
        le seuil d'inactivite ecoule, jamais perdus."""
        ids = [id_message for id_message, _ in paires]
        if len(paires) < 2:
            return ids  # un seul point ne permet aucun calcul de vitesse -- rien a en tirer, on acquitte quand meme

        premiere_horodatage = self._parser_horodatage(paires[0][1]['horodatage'])
        points = [
            {
                'lat': evt['lat'],
                'lon': evt['lon'],
                'time': (self._parser_horodatage(evt['horodatage']) - premiere_horodatage).total_seconds(),
            }
            for _, evt in paires
        ]

        try:
            edges, matched_points = tracer(points, attributs=ATTRIBUTS_TRONCON, attributs_optionnels=ATTRIBUTS_CLE_STABLE)
        except (ErreurMeili, DisjoncteurOuvert) as exc:
            logger.info('Meili indisponible, %d positions laissees en pending: %s', len(ids), exc)
            return []

        extremites, exclu = self._infos_trajet(paires[0][1]['trajet_id'])
        # Moto, bus, camion, utilitaire : leur vitesse ne represente pas celle
        # d'une voiture (traffic.tar est unique, cf. MOTO_TRAFIC_EN_DIRECT).
        if not exclu:
            self._accumuler_vitesses(edges, matched_points, paires, extremites)
        try:
            apprendre_correspondances(edges)
        except Exception:
            logger.exception('Correspondance GraphId -> voie OSM impossible a enregistrer')
        # Validation passive des signalements (route barree franchie, bouchon
        # traverse au pas...) : jamais bloquante pour le trafic.
        try:
            from community.validation_passive import observer_passages

            observer_passages(paires[0][1]['trajet_id'], [evt for _id, evt in paires])
        except Exception:
            logger.exception('Validation passive des signalements impossible')
        # Etat des routes : passages par tranche de 100 m. Une erreur ici ne
        # doit jamais bloquer le trafic temps reel (messages acquittes quand meme).
        try:
            enregistrer_passages(paires[0][1]['trajet_id'], edges, matched_points, paires, reglages())
        except Exception:
            logger.exception('Enregistrement des passages (etat des routes) impossible')
        return ids

    @staticmethod
    def _infos_trajet(trajet_id):
        """([(lat, lon)] du depart et de l'arrivee, vehicule exclu de la mesure
        du trafic ?) -- ([], False) si le trajet est inconnu. Seuls VOITURE et
        TAXI (ou type non renseigne) mesurent le trafic : une moto remonte les
        files, un bus s'arrete souvent, un camion est lent en cote, un
        utilitaire de livraison stationne -- avec NB_MIN_CONDUCTEURS = 2, un
        bus et un camion suffiraient a publier un faux bouchon pour tous."""
        from ..models import Trajet

        try:
            trajet = Trajet.objects.filter(id=uuid.UUID(str(trajet_id))).values(
                'position_origine', 'position_destination', 'type_vehicule', 'utilisateur__type_vehicule',
            ).first()
        except ValueError:
            return [], False
        if trajet is None:
            return [], False
        extremites = [(p.y, p.x) for p in (trajet['position_origine'], trajet['position_destination']) if p is not None]
        vehicule = trajet['type_vehicule'] or trajet['utilisateur__type_vehicule']
        return extremites, bool(vehicule) and vehicule not in VEHICULES_MESURANT_LE_TRAFIC

    @staticmethod
    def _vitesse_max(arete):
        classe = arete.get('road_class')
        if classe is None:
            return VITESSE_MAX_PLAUSIBLE_KMH
        return VITESSE_CLASSE_KMH.get(classe, VITESSE_CLASSE_DEFAUT_KMH) * FACTEUR_VITESSE_ABERRANTE

    def _accumuler_vitesses(self, edges, matched_points, paires, extremites=()):
        for i in range(len(matched_points) - 1):
            actuel, suivant = matched_points[i], matched_points[i + 1]
            index_arete = actuel.get('edge_index')
            if index_arete is None or suivant.get('edge_index') != index_arete:
                continue  # points non matches ou sur des aretes differentes -- rien de mesurable entre les deux

            duree_s = (
                self._parser_horodatage(paires[i + 1][1]['horodatage'])
                - self._parser_horodatage(paires[i][1]['horodatage'])
            ).total_seconds()
            if duree_s <= 0:
                continue  # horodatages desordonnes/dupliques -- ignore plutot que diviser par zero ou negatif

            longueur_km = edges[index_arete]['length']
            distance_km = (suivant['distance_along_edge'] - actuel['distance_along_edge']) * longueur_km
            if distance_km < 0:
                continue  # recul (bruit GPS / demi-tour) -- pas un signal de vitesse exploitable
            # Vehicule a l'arret (distance ~0) : on ne jette PAS l'intervalle,
            # sa duree compte et tire la vitesse vers le bas (vrai bouchon). Mais
            # un arret long est une pause (livraison, panne), pas un bouchon.
            if distance_km == 0 and duree_s > DUREE_ARRET_MAX_S:
                continue
            if distance_km > 0 and distance_km / (duree_s / 3600) > self._vitesse_max(edges[index_arete]):
                continue  # saut GPS improbable -- ne pollue pas la mesure plutot que de le capper arbitrairement

            position = (paires[i][1]['lat'], paires[i][1]['lon'])
            if any(distance_haversine_m(position, e) < MARGE_DEPART_ARRIVEE_M for e in extremites):
                continue  # depart/stationnement : lent sans etre un bouchon

            identifiant_arete = edges[index_arete]['id']
            bucket = self._bucket_5min(self._parser_horodatage(paires[i][1]['horodatage']))
            self._accumuler(
                identifiant_arete, bucket, distance_km, duree_s, paires[i][1].get('trajet_id'),
                cle_stable=cle_stable(edges[index_arete]), noeuds=noeuds_osm(edges[index_arete]),
            )

    def _accumuler(self, identifiant_arete, bucket_epoch, distance_km, duree_s, trajet_id=None, cle_stable=None,
                   noeuds=None):
        """Accumule DISTANCE et DUREE (pas une moyenne de vitesses) : la vitesse
        d'une arete sur un bucket est Sigma distance / Sigma duree -- un vehicule
        qui roule 10 s vite puis 60 s au pas donne sa vraie vitesse, pas la
        moyenne des deux. Les intervalles a l'arret (distance 0) gardent leur
        duree. `nombre` sert de poids a l'agregation historique. Champs
        PREFIXE_CHAMP_TRAJET{trajet_id}:distance/:duree en plus (meme hash) :
        la publication temps reel en tire une vitesse par conducteur (mediane)."""
        cle = f'{PREFIXE_ACCUMULATEUR}:{identifiant_arete}:{bucket_epoch}'
        with self.connexion.pipeline() as pipe:
            pipe.hincrbyfloat(cle, 'distance', distance_km)
            pipe.hincrbyfloat(cle, 'duree', duree_s)
            pipe.hincrby(cle, 'nombre', 1)
            if cle_stable is not None:
                # Voie OSM + sens : recopies dans EchantillonVitesse au flush.
                pipe.hset(cle, mapping={'way': cle_stable[0], 'avant': int(cle_stable[1])})
            if noeuds is not None:
                pipe.hset(cle, mapping={'debut': noeuds[0], 'fin': noeuds[1]})
            if trajet_id is not None:
                pipe.hincrbyfloat(cle, f'{PREFIXE_CHAMP_TRAJET}{trajet_id}:distance', distance_km)
                pipe.hincrbyfloat(cle, f'{PREFIXE_CHAMP_TRAJET}{trajet_id}:duree', duree_s)
            pipe.expire(cle, DUREE_ACCUMULATEUR_S)
            pipe.sadd(ENSEMBLE_BUCKETS_ACTIFS, cle)
            pipe.execute()

    @staticmethod
    def _parser_horodatage(horodatage) -> datetime:
        return datetime.fromisoformat(horodatage)

    @staticmethod
    def _bucket_5min(horodatage: datetime) -> int:
        epoch = int(horodatage.timestamp())
        return epoch - (epoch % TAILLE_BUCKET_S)
