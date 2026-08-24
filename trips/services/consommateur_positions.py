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
from collections import defaultdict
from datetime import datetime

import redis

from .client_meili import ErreurMeili, tracer
from .disjoncteur import DisjoncteurOuvert
from .producteur_evenements import FLUX_POSITIONS, connexion_redis_telemetrie

logger = logging.getLogger(__name__)

NOM_GROUPE = 'consommateurs-trafic'
TAILLE_LOT = 200
SEUIL_INACTIVITE_MS = 30_000  # au-dela, un message pending est repris (crash du consommateur ou echec Meili precedent)
DUREE_ACCUMULATEUR_S = 20 * 60  # marge large avant le flush periodique (5 min, P5 partie 4) -- purge seule si ce flush ne tourne pas encore
VITESSE_MAX_PLAUSIBLE_KMH = 180  # au-dela, artefact de saut GPS plutot qu'une vitesse reelle -- seuil de bon sens, a affiner avec des donnees reelles
PREFIXE_ACCUMULATEUR = 'trafic:accumulateur'
ENSEMBLE_BUCKETS_ACTIFS = 'trafic:buckets:actifs'
TAILLE_BUCKET_S = 5 * 60


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
            edges, matched_points = tracer(points)
        except (ErreurMeili, DisjoncteurOuvert) as exc:
            logger.info('Meili indisponible, %d positions laissees en pending: %s', len(ids), exc)
            return []

        self._accumuler_vitesses(edges, matched_points, paires)
        return ids

    def _accumuler_vitesses(self, edges, matched_points, paires):
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
            if distance_km <= 0:
                continue  # vehicule a l'arret ou artefact de bruit GPS -- pas un signal de vitesse exploitable

            vitesse_kmh = distance_km / (duree_s / 3600)
            if vitesse_kmh > VITESSE_MAX_PLAUSIBLE_KMH:
                continue  # saut GPS improbable -- ne pollue pas la moyenne plutot que de le capper arbitrairement

            identifiant_arete = edges[index_arete]['id']
            bucket = self._bucket_5min(self._parser_horodatage(paires[i][1]['horodatage']))
            self._accumuler(identifiant_arete, bucket, vitesse_kmh)

    def _accumuler(self, identifiant_arete, bucket_epoch, vitesse_kmh):
        cle = f'{PREFIXE_ACCUMULATEUR}:{identifiant_arete}:{bucket_epoch}'
        with self.connexion.pipeline() as pipe:
            pipe.hincrbyfloat(cle, 'somme_vitesse', vitesse_kmh)
            pipe.hincrby(cle, 'nombre', 1)
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
