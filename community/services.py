"""
ServiceIncident : couche entre la vue et le modele pour POST /api/incidents/.
Ordre volontaire (cf. discussion) : quota d'abord (rejette vite, pas de
verrou pose pour rien), puis doublon + creation dans la meme transaction
verrouillee.
"""

import h3
from django.conf import settings
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import LineString, Point
from django.contrib.gis.measure import D
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from places.services.client_nominatim import ClientNominatim
from places.utils import normaliser
from trips.services import client_locate
from trips.services.client_trace_attributes import ErreurTraceAttributes, attributs_trace
from trips.services.disjoncteur import DisjoncteurOuvert

from . import temps_reel
from .cache_incidents import invalider_cache_cellule
from .models import (
    RESOLUTION_H3_FIN,
    Incident,
    SensVote,
    StatutIncident,
    Vote,
    duree_de_vie_base,
)


def incidents_actifs_par_topologie(points: list[tuple[float, float]]) -> list[Incident] | None:
    """None si Valhalla/trace_attributes est indisponible ou ne matche aucune
    arete sur cette geometrie -- l'appelant retombe alors sur un autre mode de
    recherche (cf. IncidentsSurTrajetView._incidents_par_couloir). Une liste
    (potentiellement vide) sinon : vide signifie "aucun incident sur ce
    trajet", pas un echec. Extrait de IncidentsSurTrajetView pour etre
    reutilisable (cf. trips.serializers.TrajetModerationSerializer, qui
    l'appelle sur la geometrie stockee d'un Trajet plutot que sur une
    geometrie fournie par le client).

    Trois cas, tous limites au COULOIR du trace (meme couloir que le calcul
    d'itineraire, retards_signalements.COULOIR_M) -- une voie OSM peut faire
    des kilometres, le trajet n'en parcourt souvent qu'une partie :
      - signalement oriente (cap) : meme voie ET meme sens que le trajet
        (une contre-allee parallele, autre voie, n'est jamais prise) ;
      - signalement sans cap : meme voie, dans les deux sens (son sens
        `forward_osm` a ete devine par Valhalla, il ne veut rien dire) ;
      - signalement pas encore cale sur une voie (Valhalla indisponible a sa
        creation, cf. tache recaler_signalements_sans_voie) : couloir + cap,
        comme le repli historique, plutot que de le perdre."""
    from trips.services.sur_trajet import COULOIR_M, Trace

    try:
        aretes = attributs_trace(points)
    except (ErreurTraceAttributes, DisjoncteurOuvert):
        return None
    if aretes is None:
        return None

    paires = {(arete['way_id'], arete['forward']) for arete in aretes}
    voies = {way_id for way_id, _forward in paires}
    troncons = {arete['noeuds'] for arete in aretes if arete.get('noeuds')}
    q_oriente = Q(pk__in=[])
    for way_id, forward in paires:
        q_oriente |= Q(way_id_osm=way_id, forward_osm=forward)
    q_troncon = Q(pk__in=[])
    for debut, fin in troncons:
        q_troncon |= Q(noeud_debut_osm=debut, noeud_fin_osm=fin)
    sans_troncon = Q(noeud_debut_osm__isnull=True)
    q = (
        # Troncon connu des deux cotes (Valhalla >= 3.6) : le plus precis --
        # meme troncon entre deux carrefours, dans le meme sens.
        (Q(noeud_debut_osm__isnull=False) & q_troncon)
        # Sinon, voie OSM + sens (signalement oriente), ou voie seule (sans cap).
        | (sans_troncon & Q(cap__isnull=False) & q_oriente)
        | (sans_troncon & Q(cap__isnull=True, way_id_osm__in=voies))
        | Q(way_id_osm__isnull=True)
    )
    if not troncons:
        # Trajet sans noeuds (Valhalla < 3.6) : un signalement qui en a se
        # rabat sur sa voie et son sens.
        q |= Q(noeud_debut_osm__isnull=False, cap__isnull=False) & q_oriente
    ligne = LineString(points, srid=4326)
    candidats = Incident.objects.select_related('auteur').filter(
        q, statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE], expire_le__gt=timezone.now(),
        position__distance_lte=(ligne, D(m=COULOIR_M)),
    )

    trace = Trace(points)
    # Sans voie : le sens se verifie sur le trace (la voie le donnait sinon).
    return [i for i in candidats if i.way_id_osm is not None or trace.dans_le_sens(i.cap, i.position)]


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
        # si la verification reussit -- sinon garde le point brut soumis, sans
        # way_id/forward (Valhalla indisponible ou hors-route non bloquant).
        resultat_routier = self._verifier_position_routiere(position, cap)
        if resultat_routier is not None:
            position, way_id_osm, forward_osm = resultat_routier
        else:
            way_id_osm, forward_osm = None, None
        noeuds = self._troncon_osm(position, cap) if way_id_osm is not None else None

        # Geocodage inverse (appel HTTP Nominatim) AVANT la transaction : sinon
        # le verrou select_for_update sur les signalements voisins est tenu
        # pendant tout l'appel reseau, et les votes/signalements de la meme zone
        # attendent. Parfois inutile (si doublon), mais jamais de verrou tenu.
        geocodage = self._geocoder_inverse(position)

        with transaction.atomic():
            doublon = self._chercher_doublon(type_incident, position, cap)
            if doublon is not None:
                self._corroborer(doublon, utilisateur)
                incident, est_doublon = doublon, True
            else:
                en_agglomeration = geocodage.get('urban') if geocodage else None
                incident = Incident.objects.create(
                    auteur=utilisateur,
                    type=type_incident,
                    sous_type=sous_type,
                    position=position,
                    cap=cap,
                    way_id_osm=way_id_osm,
                    forward_osm=forward_osm,
                    noeud_debut_osm=noeuds[0] if noeuds else None, noeud_fin_osm=noeuds[1] if noeuds else None,
                    nom_voie=geocodage['label'] if geocodage else '',
                    ville=geocodage['city'] if geocodage else '',
                    ville_normalisee=normaliser(geocodage['city']) if geocodage and geocodage['city'] else '',
                    en_agglomeration=en_agglomeration,
                    # Toujours EN_ATTENTE a la creation, quelle que soit la reputation
                    # de l'auteur : promu ACTIF par Incident.confirmer() une fois le
                    # score de confiance corrobore par d'autres utilisateurs
                    # (seuil reduit si l'auteur est deja repute, cf. seuil_validation()).
                    statut=StatutIncident.EN_ATTENTE,
                    expire_le=timezone.now() + timezone.timedelta(
                        minutes=duree_de_vie_base(type_incident, sous_type, en_agglomeration)
                    ),
                )
                est_doublon = False

        invalider_cache_cellule(incident.cellule_h3_res8)
        # Doublon fusionne : l'existant a change (confirmations, eventuellement
        # statut) -- l'application le met a jour, aucun nouveau signalement.
        temps_reel.publier_incident(incident, temps_reel.MODIFIE if est_doublon else temps_reel.CREE)
        return incident, est_doublon

    def signaler_officiel(self, membre, organisation, type_incident, position, debut_le, fin_le,
                          cap=None, sous_type='', note=''):
        """Signalement pose par une autorite (mairie, police) depuis le BO.
        Contrairement a signaler() : pas de quota ni de dedoublonnage (il fait
        autorite), marque valide_par_staff (traite comme confirme : une
        fermeture officielle est evitee tout de suite), et porte une periode
        debut_le -> fin_le. Programme si debut_le est dans le futur (hors des
        listes actives jusqu'a activer_programmes, community.tasks)."""
        resultat_routier = self._verifier_position_routiere(position, cap)
        if resultat_routier is not None:
            position, way_id_osm, forward_osm = resultat_routier
        else:
            way_id_osm, forward_osm = None, None
        noeuds = self._troncon_osm(position, cap) if way_id_osm is not None else None

        maintenant = timezone.now()
        with transaction.atomic():
            geocodage = self._geocoder_inverse(position)
            incident = Incident.objects.create(
                auteur=membre,
                organisation=organisation,
                type=type_incident,
                sous_type=sous_type,
                position=position,
                cap=cap,
                way_id_osm=way_id_osm,
                forward_osm=forward_osm,
                noeud_debut_osm=noeuds[0] if noeuds else None, noeud_fin_osm=noeuds[1] if noeuds else None,
                nom_voie=geocodage['label'] if geocodage else '',
                ville=geocodage['city'] if geocodage else '',
                ville_normalisee=normaliser(geocodage['city']) if geocodage and geocodage['city'] else '',
                en_agglomeration=geocodage.get('urban') if geocodage else None,
                valide_par_staff=True,
                valide_le=maintenant,
                debut_le=debut_le,
                note=note,
                statut=StatutIncident.ACTIF if debut_le <= maintenant else StatutIncident.PROGRAMME,
                expire_le=fin_le,
            )

        invalider_cache_cellule(incident.cellule_h3_res8)
        if incident.statut == StatutIncident.ACTIF:
            temps_reel.publier_incident(incident, temps_reel.CREE)
        return incident

    def _verifier_quota(self, utilisateur):
        # Desactivable via QUOTA_SIGNALEMENTS_ACTIF (settings.py) -- coupe
        # pour le moment pour ne pas bloquer les tests manuels repetes.
        if not settings.QUOTA_SIGNALEMENTS_ACTIF:
            return
        depuis = timezone.now() - timezone.timedelta(hours=1)
        recents = Incident.objects.filter(auteur=utilisateur, cree_le__gte=depuis).count()
        if recents >= QUOTA_SIGNALEMENTS_PAR_HEURE:
            raise QuotaDepasse()

    def _troncon_osm(self, position, cap):
        """Troncon entre deux carrefours (noeuds OSM de debut et de fin, dans
        le sens du signalement) : plus precis que la voie entiere pour savoir
        si un trajet passe dessus. Seulement avec un cap (sinon le sens, donc
        l'ordre des noeuds, serait devine) et Valhalla >= 3.6 avec
        keep_osm_node_ids ; None sinon -- la voie OSM sert alors."""
        if cap is None:
            return None
        from trips.services.client_trace_attributes import troncon_au_point

        return troncon_au_point(position.y, position.x, cap)

    def _verifier_position_routiere(self, position, cap=None):
        """None si la verification est ignoree (Valhalla indisponible, ou
        aucune route connue dans la zone) -- l'appelant garde alors le point
        brut, sans way_id/forward. Sinon (Point, way_id, forward) : la
        position calee sur l'arete trouvee (correlated_lat/lon -- meme un bon
        appariement peut avoir quelques metres d'ecart entre le point soumis
        et le centre reel de la route, precision GPS cote client ou choix de
        Nominatim en amont -- visible a l'affichage carte si on garde le
        point brut, cf. capture d'ecran frontend, un marqueur "a cote" de la
        route plutot que dessus), l'identifiant OSM de la voie et le sens de
        circulation dessus (cf. Incident.way_id_osm/forward_osm -- matching
        par topologie dans IncidentsSurTrajetView, pas par distance).

        `cap` transmis a client_locate.localiser() : sur une rue a double
        sens, les deux sens partagent le meme way_id -- sans heading pour
        les departager, l'arete renvoyee par Valhalla (et donc `forward`)
        n'est pas fiable, verifie en prod (cf. discussion : un signalement
        a heading=177 sur un trace a 357, donc oppose, s'etait vu attribuer
        forward=True au lieu de False)."""
        try:
            arete = client_locate.localiser(position.y, position.x, cap=cap)
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
        position_calee = Point(arete['lon'], arete['lat'], srid=4326)
        return position_calee, arete['way_id'], arete['forward']

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
        # L'auteur ne confirme pas son propre signalement : sinon un seul compte
        # qui re-signale au meme endroit fait passer confirmations a 1, ce qui
        # suffit a fermeture_confirmee (retards_signalements) -- route exclue
        # pour tous. Meme regle que la vue de vote (IncidentDetailView).
        if incident.auteur_id == utilisateur.id:
            return
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
