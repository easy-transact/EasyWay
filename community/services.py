"""
ServiceIncident : couche entre la vue et le modele pour POST /api/incidents/.
Ordre volontaire (cf. discussion) : quota d'abord (rejette vite, pas de
verrou pose pour rien), puis doublon + creation dans la meme transaction
verrouillee.
"""

import h3
from django.conf import settings
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.db import transaction
from django.utils import timezone

from places.services.client_nominatim import ClientNominatim
from places.utils import normaliser
from trips.services import client_locate
from trips.services.disjoncteur import DisjoncteurOuvert

from .cache_incidents import invalider_cache_cellule
from .models import (
    DUREE_VIE_BASE_PAR_TYPE,
    RESOLUTION_H3_FIN,
    Incident,
    SensVote,
    StatutIncident,
    Vote,
)

QUOTA_SIGNALEMENTS_PAR_HEURE = 10
RAYON_DOUBLON_M = 150
SECTEUR_DOUBLON_DEGRES = 45
# Au-dela, on considere que la position n'est sur/pres d'aucune route connue
# de Valhalla -- pas mesure sur donnees reelles (derive GPS, imprecision
# urbaine autour des grands carrefours), a ajuster avec de l'usage reel.
RAYON_MAX_HORS_ROUTE_M = 50


class QuotaDepasse(Exception):
    """Leve quand l'utilisateur a atteint QUOTA_SIGNALEMENTS_PAR_HEURE."""


class PositionHorsRoute(Exception):
    """Leve quand la position du signalement est trop loin de toute route
    connue de Valhalla (RAYON_MAX_HORS_ROUTE_M), ou quand l'arete la plus
    proche est une allee privee/un parking (destination_only) -- jamais une
    route publique, meme a distance nulle."""


class ServiceIncident:
    def signaler(self, utilisateur, type_incident, position, cap=None, sous_type=''):
        """Retourne (incident, est_doublon). est_doublon=True signifie qu'aucun
        nouvel Incident n'a ete cree -- le signalement a corrobore un existant."""
        self._verifier_quota(utilisateur)
        # Position calee sur l'arete routiere trouvee (cf. _verifier_position_routiere)
        # si la verification reussit -- sinon garde le point brut soumis.
        position = self._verifier_position_routiere(position) or position

        with transaction.atomic():
            doublon = self._chercher_doublon(type_incident, position, cap)
            if doublon is not None:
                self._corroborer(doublon, utilisateur)
                incident, est_doublon = doublon, True
            else:
                geocodage = self._geocoder_inverse(position)
                incident = Incident.objects.create(
                    auteur=utilisateur,
                    type=type_incident,
                    sous_type=sous_type,
                    position=position,
                    cap=cap,
                    nom_voie=geocodage['label'] if geocodage else '',
                    ville=geocodage['city'] if geocodage else '',
                    ville_normalisee=normaliser(geocodage['city']) if geocodage and geocodage['city'] else '',
                    # Toujours EN_ATTENTE a la creation, quelle que soit la reputation
                    # de l'auteur : promu ACTIF par Incident.confirmer() une fois le
                    # score de confiance corrobore par d'autres utilisateurs
                    # (seuil reduit si l'auteur est deja repute, cf. seuil_validation()).
                    statut=StatutIncident.EN_ATTENTE,
                    expire_le=timezone.now() + timezone.timedelta(
                        minutes=DUREE_VIE_BASE_PAR_TYPE.get(type_incident, 60)
                    ),
                )
                est_doublon = False

        invalider_cache_cellule(incident.cellule_h3_res8)
        return incident, est_doublon

    def _verifier_quota(self, utilisateur):
        # Desactivable via QUOTA_SIGNALEMENTS_ACTIF (settings.py) -- coupe
        # pour le moment pour ne pas bloquer les tests manuels repetes.
        if not settings.QUOTA_SIGNALEMENTS_ACTIF:
            return
        depuis = timezone.now() - timezone.timedelta(hours=1)
        recents = Incident.objects.filter(auteur=utilisateur, cree_le__gte=depuis).count()
        if recents >= QUOTA_SIGNALEMENTS_PAR_HEURE:
            raise QuotaDepasse()

    def _verifier_position_routiere(self, position):
        """None si la verification est ignoree (Valhalla indisponible, ou
        aucune route connue dans la zone) -- l'appelant garde alors le point
        brut. Sinon la position calee sur l'arete trouvee (correlated_lat/
        lon) : meme un bon appariement peut avoir quelques metres d'ecart
        entre le point soumis et le centre reel de la route (precision GPS
        cote client, ou choix de Nominatim en amont) -- visible a l'affichage
        carte si on garde le point brut (cf. capture d'ecran frontend, un
        marqueur "a cote" de la route plutot que dessus)."""
        try:
            arete = client_locate.localiser(position.y, position.x)
        except (client_locate.ErreurLocate, DisjoncteurOuvert):
            # Meme principe que _nom_voie() : un Valhalla en panne ne doit
            # jamais bloquer un signalement -- on renonce juste a la
            # verification pour cette fois.
            return None
        if arete is None:
            return None
        if arete['destination_only']:
            # Allee privee/parking (use=driveway typiquement) : Valhalla la
            # considere routable (on peut y conduire) mais ce n'est jamais
            # une route publique -- trouve en verification live, un
            # signalement "radar" s'etait cale sur une allee a 16m d'une
            # vraie route, jugee la plus proche par simple distance.
            raise PositionHorsRoute(
                'Reported position matches a private driveway/parking access, not a public road.'
            )
        if arete['distance_m'] > RAYON_MAX_HORS_ROUTE_M:
            raise PositionHorsRoute(
                f"Reported position is {round(arete['distance_m'])}m from the nearest known road."
            )
        return Point(arete['lon'], arete['lat'], srid=4326)

    def _chercher_doublon(self, type_incident, position, cap):
        # select_for_update() verrouille les candidats pour la duree de la
        # transaction : sans ca, deux signalements du meme evenement arrivant
        # dans la meme seconde peuvent chacun voir "aucun doublon" et creer
        # deux Incident distincts pour la meme chose.
        cellule = h3.latlng_to_cell(position.y, position.x, RESOLUTION_H3_FIN)
        cellules_voisines = [h3.str_to_int(c) for c in h3.grid_disk(cellule, 1)]

        # expire_le__gt en plus du statut : `statut` ne passe a EXPIRE que via
        # la tache periodique expirer_incidents (jusqu'a 60s de retard, ou
        # indefiniment si cette tache est en panne) -- sans ce filtre, un
        # signalement deja perime mais encore marque ACTIF en base peut etre
        # "corrobore" au lieu qu'un nouveau soit cree, et corroborer() ne fait
        # que prolonger son expire_le deja passe de 10 minutes plutot que de
        # le faire repartir de maintenant -- il reste invisible partout.
        candidats = Incident.objects.select_for_update().filter(
            type=type_incident,
            statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE],
            expire_le__gt=timezone.now(),
            cellule_h3_res8__in=cellules_voisines,
        ).annotate(distance=Distance('position', position)).filter(distance__lte=D(m=RAYON_DOUBLON_M))

        for candidat in candidats:
            if cap is not None and candidat.cap is not None:
                ecart = abs(cap - candidat.cap) % 360
                ecart = min(ecart, 360 - ecart)
                if ecart > SECTEUR_DOUBLON_DEGRES:
                    continue
            return candidat
        return None

    def _corroborer(self, incident, utilisateur):
        if Vote.objects.filter(incident=incident, votant=utilisateur).exists():
            return  # deja signale/vote sur cet incident -- ne compte pas deux fois
        vote = Vote.objects.create(
            incident=incident, votant=utilisateur,
            sens=SensVote.CONFIRMATION, poids=utilisateur.poids_de_vote(),
        )
        incident.confirmer(vote)

    def _geocoder_inverse(self, position):
        # Un seul appel Nominatim pour nom_voie et ville (pas deux) -- meme
        # reponse, pas de raison de doubler la charge sur un service externe
        # deja partage. Circuit breaker deja dans ClientNominatim (P2b) : un
        # Nominatim en panne renvoie None ici, jamais une exception -- la
        # creation de l'incident ne doit jamais bloquer sur des champs
        # secondaires.
        return ClientNominatim().inverser(position.y, position.x)
