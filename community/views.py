import math
from collections import defaultdict

import h3
from django.contrib.gis.geos import LineString, Point
from django.contrib.gis.measure import D
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.pagination import StaffPagination
from accounts.serializers import MessageSerializer
from places.utils import normaliser
from trips.services.client_trace_attributes import ErreurTraceAttributes, attributs_trace
from trips.services.disjoncteur import DisjoncteurOuvert

from .cache_incidents import (
    DUREE_CACHE_CELLULE_S,
    cle_cache_cellule,
    ecriture_recente,
    invalider_cache_cellule,
)
from .models import Incident, SensVote, StatutIncident, Vote
from .serializers import (
    IncidentAvecDoublonSerializer,
    IncidentCreationSerializer,
    IncidentModerationSerializer,
    IncidentRetraitSerializer,
    IncidentsSurTrajetSerializer,
    IncidentSerializer,
    VoteIncidentSerializer,
)
from .services import PositionHorsRoute, QuotaDepasse, ServiceIncident

DUREE_IDEMPOTENCE_S = 24 * 3600

# Le decoupage H3 cote client n'a pas de limite fiable -- un viewport dezoome
# ou un bug de calcul peut envoyer des centaines de cellules. Le serveur est
# le seul a connaitre le volume reel derriere chaque cellule, donc c'est lui
# qui doit refuser une requete trop large plutot que de la servir en silence.
# Valeurs pas mesurees sur donnees reelles -- a ajuster avec de l'usage reel.
MAX_CELLULES = 50
MAX_RESULTATS = 50

# Mode rayon (lat/lon/radius_km, sans cells) : requete geographique directe,
# ne passe pas par le cache par cellule H3 -- un rayon de plusieurs km couvre
# trop de cellules pour que ce cache reste pertinent (cf. discussion, un
# simple passage par grid_disk() a ce rayon produirait des milliers de
# cellules). Valeurs pas mesurees sur donnees reelles.
RAYON_KM_DEFAUT = 10
RAYON_KM_MAX = 20


def _distance_m(lat1, lon1, lat2, lon2):
    rayon_terre_m = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = math.sin(delta_lat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(delta_lon / 2) ** 2
    return 2 * rayon_terre_m * math.asin(math.sqrt(a))


@extend_schema(
    tags=['Incidents'],
    summary='Lister les incidents actifs a proximite',
    description=(
        "Deux modes, au choix : `cells` (cellules H3 resolution 8, mode habituel du "
        "client en conduite -- cache par cellule, invalide a l'ecriture plutot que "
        "de compter sur le seul TTL, cf. cache_incidents.py) ou `lat`+`lon`+"
        f"`radius_km` (requete geographique directe dans un rayon, sans passer par "
        f"le cache par cellule -- pas adapte a un rayon de plusieurs km. Defaut "
        f"{RAYON_KM_DEFAUT}km, maximum {RAYON_KM_MAX}km). Exactement un des deux "
        "modes doit etre fourni."
    ),
    parameters=[
        OpenApiParameter(
            'cells', OpenApiTypes.STR, required=False,
            description=(
                f'Mode cellules H3 (resolution 8) en hexadecimal, separees par des virgules. '
                f'Maximum {MAX_CELLULES}. Incompatible avec radius_km.'
            ),
        ),
        OpenApiParameter(
            'radius_km', OpenApiTypes.FLOAT, required=False,
            description=(
                f"Mode rayon : distance en km autour de lat/lon (defaut {RAYON_KM_DEFAUT}, "
                f"maximum {RAYON_KM_MAX}). Ignore un `cells` fourni en meme temps."
            ),
        ),
        OpenApiParameter(
            'lat', OpenApiTypes.FLOAT, required=False,
            description=(
                "Mode cells : position de reference pour le tri par pertinence (optionnel). "
                "Mode rayon : centre de la recherche (obligatoire avec lon)."
            ),
        ),
        OpenApiParameter(
            'lon', OpenApiTypes.FLOAT, required=False,
            description="Meme role que lat selon le mode -- cf. description de lat.",
        ),
    ],
    responses={200: IncidentSerializer(many=True), 400: MessageSerializer},
)
class IncidentsProchesView(APIView):
    """GET /api/incidents/nearby/ : deux modes.
    ?cells=<hex,hex,...>&lat=&lon= -- endpoint le plus sollicite du module en
    conduite. Cache par cellule H3 res8 (~0.74km², cf. l'index
    cellule_h3_res8+statut du modele), invalide a l'ecriture (creation/vote/
    expiration/retrait) plutot que de compter sur le seul TTL -- cf.
    cache_incidents.py.

    ?lat=&lon=&radius_km= -- requete geographique directe (pas de cache par
    cellule : un rayon de plusieurs km couvrirait trop de cellules H3 pour
    que ce cache reste pertinent). Pense pour un "que se passe-t-il autour de
    moi" ponctuel, pas pour l'appel repete pendant la conduite (cells reste
    le mode a preferer dans ce cas, deja optimise pour ca).

    MAX_CELLULES/MAX_RESULTATS : le decoupage H3 cote client n'a pas de
    limite fiable (viewport dezoome, bug de calcul) -- c'est le serveur qui
    doit refuser une requete trop large et plafonner/trier la reponse,
    puisqu'il est seul a connaitre le volume reel derriere chaque cellule."""

    permission_classes = [AllowAny]

    def get(self, request):
        cellules_hex = [c for c in request.query_params.get('cells', '').split(',') if c]

        lat = request.query_params.get('lat')
        lon = request.query_params.get('lon')
        if (lat is None) != (lon is None):
            return Response({'detail': "'lat' and 'lon' must be provided together."}, status=400)
        if lat is not None:
            try:
                lat, lon = float(lat), float(lon)
            except ValueError:
                return Response({'detail': "'lat'/'lon' must be numbers."}, status=400)

        if not cellules_hex:
            if lat is None:
                return Response(
                    {'detail': "Provide either 'cells' or 'lat'+'lon' (optionally with 'radius_km')."},
                    status=400,
                )
            return self._recherche_par_rayon(request, lat, lon)

        if len(cellules_hex) > MAX_CELLULES:
            return Response(
                {
                    'detail': (
                        f"Too many cells requested ({len(cellules_hex)}), maximum {MAX_CELLULES}. "
                        "Narrow the request to the area actually visible to the user."
                    )
                },
                status=400,
            )

        resultats_par_cellule = {}
        manquantes = []
        for hex_cell in cellules_hex:
            valeur = cache.get(cle_cache_cellule(h3.str_to_int(hex_cell)))
            if valeur is None:
                manquantes.append(hex_cell)
            else:
                resultats_par_cellule[hex_cell] = valeur

        if manquantes:
            cellules_int = {c: h3.str_to_int(c) for c in manquantes}
            incidents = Incident.objects.filter(
                cellule_h3_res8__in=cellules_int.values(),
                statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE],
                expire_le__gt=timezone.now(),
            )
            par_cellule = defaultdict(list)
            for incident in incidents:
                par_cellule[h3.int_to_str(incident.cellule_h3_res8)].append(incident)

            for hex_cell in manquantes:
                donnees = IncidentSerializer(par_cellule.get(hex_cell, []), many=True).data
                # Ne pas cacher si la cellule vient d'etre invalidee (ecriture recente,
                # cf. cache_incidents.py) : cette lecture a pu demarrer sa requete DB
                # juste avant l'ecriture (snapshot perime) -- la cacher ici la ferait
                # servir jusqu'a son propre TTL au lieu du prochain miss legitime.
                if not ecriture_recente(cellules_int[hex_cell]):
                    cache.set(cle_cache_cellule(cellules_int[hex_cell]), donnees, timeout=DUREE_CACHE_CELLULE_S)
                resultats_par_cellule[hex_cell] = donnees

        fusion = [incident for hex_cell in cellules_hex for incident in resultats_par_cellule[hex_cell]]

        # Pertinence : proximite d'abord si on a une position de reference
        # (sinon impossible a evaluer -- rien ne dit que la cellule la plus
        # proche dans la liste du client l'est reellement), puis gravite et
        # confiance en depart-egalite. confidence_score serialise en str
        # (DecimalField) -- cast explicite pour un tri numerique.
        def cle_tri(incident):
            cle = (-incident['severity'], -float(incident['confidence_score']))
            if lat is not None:
                cle = (_distance_m(lat, lon, incident['lat'], incident['lon']),) + cle
            return cle

        fusion.sort(key=cle_tri)
        return Response(fusion[:MAX_RESULTATS])

    def _recherche_par_rayon(self, request, lat, lon):
        radius_km = request.query_params.get('radius_km', RAYON_KM_DEFAUT)
        try:
            radius_km = float(radius_km)
        except ValueError:
            return Response({'detail': "'radius_km' must be a number."}, status=400)
        if not (0 < radius_km <= RAYON_KM_MAX):
            return Response(
                {'detail': f"'radius_km' must be between 0 and {RAYON_KM_MAX}."}, status=400
            )

        centre = Point(lon, lat, srid=4326)
        incidents = Incident.objects.filter(
            statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE],
            expire_le__gt=timezone.now(),
            position__distance_lte=(centre, D(km=radius_km)),
        )

        donnees = IncidentSerializer(incidents, many=True).data
        donnees.sort(key=lambda i: _distance_m(lat, lon, i['lat'], i['lon']))
        return Response(donnees[:MAX_RESULTATS])


# Meme tolerance que SECTEUR_DOUBLON_DEGRES (community/services.py) : un
# signalement dont le cap s'ecarte de plus de 45 deg du cap local du trajet
# est considere comme une autre route (chaussee opposee, rue perpendiculaire)
# -- ne resout pas le cas d'une contre-allee parallele (meme cap que la
# route qu'elle longe), qui demande le matching par way_id ci-dessous.
ECART_CAP_MAX_DEGRES = 45


@extend_schema(
    tags=['Incidents'],
    summary="Lister les incidents le long d'un trajet complet",
    description=(
        "Alternative a /nearby/ pour 'tous les incidents sur mon trajet', pas "
        "seulement pres de ma position actuelle -- pense pour etre appele une fois au "
        "depart du trajet (geometrie = routes/calculate -> route.geometry, ou "
        "trips/{id} -> routes[0].geometry) et garde en cache local cote client comme "
        "filet hors-ligne ; /nearby/ reste la source vivante pendant la conduite. "
        "Geometrie en corps de requete (POST), jamais en parametres d'URL -- un trajet "
        "long (Douala-Yaounde, ~250km) demanderait plusieurs centaines de cellules H3 "
        "et une URL ingerable si on suivait le modele de /nearby/."
    ),
    request=IncidentsSurTrajetSerializer,
    responses={200: IncidentSerializer(many=True), 400: MessageSerializer},
)
class IncidentsSurTrajetView(APIView):
    """POST /api/incidents/along-route/ : matche par topologie (meme way_id
    OSM + meme sens que le trajet, cf. _incidents_par_topologie) plutot que
    par distance des que Valhalla/trace_attributes repondent -- un couloir de
    distance ne separe jamais une contre-allee parallele de la route qu'elle
    longe. Repli sur le couloir historique (+ cap, cf. _incidents_par_couloir)
    si Valhalla est indisponible."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = IncidentsSurTrajetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        points = serializer.validated_data['geometry']
        buffer_m = serializer.validated_data['buffer_m']

        incidents = self._incidents_par_topologie(points)
        if incidents is None:
            incidents = self._incidents_par_couloir(points, buffer_m)

        # Meme logique de pertinence que /nearby/ sans position de reference
        # (aucune ici -- le trajet entier est demande, pas un point) :
        # gravite puis confiance, et un plafond identique pour ne jamais
        # renvoyer un trajet tres long en entier sans limite.
        donnees = IncidentSerializer(incidents, many=True).data
        donnees.sort(key=lambda i: (-i['severity'], -float(i['confidence_score'])))
        return Response(donnees[:MAX_RESULTATS])

    def _incidents_par_topologie(self, points):
        """None si Valhalla est indisponible ou ne matche aucune arete sur
        cette geometrie -- l'appelant retombe alors sur _incidents_par_couloir().
        Une liste (potentiellement vide) sinon : vide signifie "aucun incident
        sur ce trajet", pas un echec, ne doit jamais declencher le repli. Les
        incidents sans way_id_osm (crees avant cette colonne, ou signales sans
        verification routiere disponible) n'apparaissent jamais ici par
        construction -- ils restent visibles via /nearby/ et /city/, juste
        pas via ce matching precis (transitoire, cf. Incident.way_id_osm)."""
        try:
            aretes = attributs_trace(points)
        except (ErreurTraceAttributes, DisjoncteurOuvert):
            return None
        if aretes is None:
            return None

        paires = {(arete['way_id'], arete['forward']) for arete in aretes}
        q = Q(pk__in=[])
        for way_id, forward in paires:
            q |= Q(way_id_osm=way_id, forward_osm=forward)

        return list(Incident.objects.filter(
            q, statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE], expire_le__gt=timezone.now(),
        ))

    def _incidents_par_couloir(self, points, buffer_m):
        """Repli historique (couloir de distance + cap, cf. discussion) --
        utilise seulement quand Valhalla/trace_attributes est indisponible.
        Ne separe pas une contre-allee parallele de la route qu'elle longe
        (meme cap) : limite structurelle de ce repli, pas un bug a corriger
        ici -- _incidents_par_topologie() est le vrai correctif."""
        ligne = LineString(points, srid=4326)
        incidents = Incident.objects.filter(
            statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE],
            expire_le__gt=timezone.now(),
            position__distance_lte=(ligne, D(m=buffer_m)),
        )

        resultats = []
        for incident in incidents:
            if incident.cap is not None:
                cap_trajet = _cap_trajet_le_plus_proche(points, incident.position)
                ecart = abs(incident.cap - cap_trajet) % 360
                ecart = min(ecart, 360 - ecart)
                if ecart > ECART_CAP_MAX_DEGRES:
                    continue
            resultats.append(incident)
        return resultats


def _cap_segment(lon1, lat1, lon2, lat2):
    """Cap (0-359, degres depuis le nord) du segment [1 -> 2] -- meme
    convention que Incident.cap (fourni par le client au signalement)."""
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    delta_lon = math.radians(lon2 - lon1)
    x = math.sin(delta_lon) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(delta_lon)
    return math.degrees(math.atan2(x, y)) % 360


def _cap_trajet_le_plus_proche(points, position):
    """Cap du segment de `points` (liste de (lon, lat) decodee du polyline6)
    le plus proche de `position` (geos Point) -- approxime par le sommet le
    plus proche plutot qu'une vraie projection sur la ligne : suffisant a
    l'echelle du couloir de recherche (quelques dizaines de metres, cf.
    BUFFER_M_DEFAUT), pas mesure au-dela de ca."""
    lon0, lat0 = position.x, position.y
    index_plus_proche, distance_min = 0, float('inf')
    for i, (lon, lat) in enumerate(points):
        distance = (lon - lon0) ** 2 + (lat - lat0) ** 2
        if distance < distance_min:
            distance_min, index_plus_proche = distance, i
    if index_plus_proche == len(points) - 1:
        index_plus_proche -= 1
    lon1, lat1 = points[index_plus_proche]
    lon2, lat2 = points[index_plus_proche + 1]
    return _cap_segment(lon1, lat1, lon2, lat2)


@extend_schema(
    tags=['Incidents'],
    summary="Lister les incidents actifs d'une ville",
    description=(
        "Filtre sur `city`, denormalise sur l'incident au moment du signalement "
        "(meme reverse-geocodage Nominatim que street_name, un seul appel pour les "
        "deux -- cf. ServiceIncident._geocoder_inverse). Comparaison insensible a "
        "la casse et aux accents, par sous-chaine plutot qu'exacte ('Yaounde' "
        "matche aussi bien 'Yaounde' que 'Yaounde I' ou une ville dont city "
        "n'a pas ete nettoyee de son granularite administrative -- cf. discussion "
        "sur l'incoherence 'Douala I' vs 'Communaute urbaine de Douala' dans les "
        "donnees OSM du Cameroun). Un signalement cree pendant une panne de "
        "Nominatim n'a pas de ville renseignee et n'apparait dans aucun resultat "
        "de cet endpoint."
    ),
    parameters=[
        OpenApiParameter(
            'name', OpenApiTypes.STR, required=True,
            description='Nom de la ville (ex. "Yaounde", "Douala").',
        ),
    ],
    responses={200: IncidentSerializer(many=True), 400: MessageSerializer},
)
class IncidentsParVilleView(APIView):
    """GET /api/incidents/city/?name=<ville> : incidents actifs/en attente
    dont la ville denormalisee correspond (insensible casse/accents)."""

    permission_classes = [AllowAny]

    def get(self, request):
        nom_ville = request.query_params.get('name', '').strip()
        if not nom_ville:
            return Response({'detail': "'name' is required."}, status=400)

        incidents = Incident.objects.filter(
            ville_normalisee__contains=normaliser(nom_ville),
            statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE],
            expire_le__gt=timezone.now(),
        )

        # Meme logique de pertinence que /nearby/ et /along-route/ sans
        # position de reference : gravite puis confiance, plafond identique.
        donnees = IncidentSerializer(incidents, many=True).data
        donnees.sort(key=lambda i: (-i['severity'], -float(i['confidence_score'])))
        return Response(donnees[:MAX_RESULTATS])


@extend_schema(
    tags=['Incidents'],
    summary='Signaler un incident',
    description=(
        "L'en-tete Idempotency-Key est obligatoire (rejeu reseau = meme reponse, "
        "jamais un deuxieme signalement, cle valable "
        f"{DUREE_IDEMPOTENCE_S // 3600}h). Si le signalement est fusionne avec un "
        "incident existant a proximite, 'duplicate_of_existing' vaut true et le "
        "statut HTTP est 200 au lieu de 201. La position est verifiee contre le "
        "graphe routier de Valhalla (/locate) : trop loin de toute route connue "
        "(> 50m) ou sur une allee privee/un parking (destination_only), le "
        "signalement est rejete en 400 -- verification desactivee sans bloquer "
        "le signalement si Valhalla est indisponible. La position enregistree "
        "(lat/lon de la reponse) est calee sur l'arete routiere trouvee, pas "
        "forcement identique au point soumis."
    ),
    parameters=[
        OpenApiParameter(
            'Idempotency-Key', OpenApiTypes.STR, OpenApiParameter.HEADER, required=True,
            description='Cle unique generee par le client pour ce signalement.',
        ),
    ],
    request=IncidentCreationSerializer,
    responses={
        200: IncidentAvecDoublonSerializer,
        201: IncidentAvecDoublonSerializer,
        400: MessageSerializer,
        429: OpenApiResponse(MessageSerializer, description='Hourly report quota reached.'),
    },
)
class IncidentCreationView(APIView):
    """POST /api/incidents/ : Idempotency-Key obligatoire (rejeu reseau =
    meme reponse, jamais un deuxieme signalement)."""

    def post(self, request):
        cle_idempotence = request.headers.get('Idempotency-Key')
        if not cle_idempotence:
            return Response({'detail': 'The Idempotency-Key header is required.'}, status=400)

        cle_cache = f'incident:idempotence:{request.user.id}:{cle_idempotence}'
        incident_id_rejoue = cache.get(cle_cache)
        if incident_id_rejoue:
            incident = get_object_or_404(Incident, id=incident_id_rejoue)
            return Response(IncidentSerializer(incident).data, status=status.HTTP_200_OK)

        serializer = IncidentCreationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data

        try:
            incident, est_doublon = ServiceIncident().signaler(
                utilisateur=request.user,
                type_incident=donnees['type'],
                position=Point(donnees['lon'], donnees['lat'], srid=4326),
                cap=donnees['cap'],
                sous_type=donnees['sous_type'],
            )
        except QuotaDepasse:
            return Response(
                {'detail': 'Hourly report quota reached.'}, status=status.HTTP_429_TOO_MANY_REQUESTS
            )
        except PositionHorsRoute as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        cache.set(cle_cache, str(incident.id), timeout=DUREE_IDEMPOTENCE_S)

        corps = IncidentSerializer(incident).data
        corps['duplicate_of_existing'] = est_doublon
        return Response(corps, status=status.HTTP_200_OK if est_doublon else status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        tags=['Incidents'],
        summary="Detail d'un incident",
        description=(
            "404 si l'incident n'est plus actif/en attente ou si sa periode de "
            "validite est expiree -- meme regle que /nearby/, /along-route/ et "
            "/city/, pour qu'aucun endpoint ne puisse faire reapparaitre un "
            "signalement que ces listes ont deja exclu."
        ),
        responses={200: IncidentSerializer},
    ),
    delete=extend_schema(
        tags=['Incidents'],
        summary="Retirer son propre signalement",
        description='Retrait logique (statut, motif) plutot que suppression -- reserve a l\'auteur.',
        responses={204: None},
    ),
)
class IncidentDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, id):
        incident = get_object_or_404(
            Incident,
            id=id,
            statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE],
            expire_le__gt=timezone.now(),
        )
        return Response(IncidentSerializer(incident).data)

    def delete(self, request, id):
        incident = get_object_or_404(Incident, id=id, auteur=request.user)
        incident.retirer(motif="Retire par l'auteur")
        invalider_cache_cellule(incident.cellule_h3_res8)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    tags=['Incidents'],
    summary='Voter (confirmer/infirmer) un incident',
    description=(
        "Rejette avec 400 le vote sur son propre signalement ou un second vote du "
        "meme utilisateur sur le meme incident (un seul Vote par (incident, votant))."
    ),
    request=VoteIncidentSerializer,
    responses={200: IncidentSerializer, 400: MessageSerializer},
)
class VoterIncidentView(APIView):
    def post(self, request, id):
        serializer = VoteIncidentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        sens = serializer.validated_data['sens']

        with transaction.atomic():
            incident = get_object_or_404(Incident.objects.select_for_update(), id=id)

            if incident.auteur_id == request.user.id:
                return Response(
                    {'detail': 'You cannot vote on your own report.'}, status=400
                )
            if Vote.objects.filter(incident=incident, votant=request.user).exists():
                return Response({'detail': 'You have already voted on this report.'}, status=400)

            vote = Vote.objects.create(
                incident=incident, votant=request.user,
                sens=SensVote.CONFIRMATION if sens == 'confirm' else SensVote.INFIRMATION,
                poids=request.user.poids_de_vote(),
            )
            if sens == 'confirm':
                incident.confirmer(vote)
            else:
                incident.infirmer(vote)

        invalider_cache_cellule(incident.cellule_h3_res8)
        return Response(IncidentSerializer(incident).data)


@extend_schema(
    tags=['Incidents'],
    summary="Lister les signalements de l'utilisateur connecte",
    responses={200: IncidentSerializer(many=True)},
)
class MesSignalementsView(APIView):
    def get(self, request):
        incidents = request.user.incidents_signales.order_by('-cree_le')
        return Response(IncidentSerializer(incidents, many=True).data)


@extend_schema(
    tags=['Staff Incidents'],
    summary='Lister les incidents (moderation)',
    description=(
        "Reserve au staff (is_staff). Filtre par defaut sur ACTIF+EN_ATTENTE -- "
        "passer `status=RETIRE`/`EXPIRE`/`FUSIONNE` pour voir les autres files."
    ),
    parameters=[
        OpenApiParameter('status', OpenApiTypes.STR, description='Defaut ACTIF+EN_ATTENTE.'),
        OpenApiParameter('page', OpenApiTypes.INT),
        OpenApiParameter('page_size', OpenApiTypes.INT),
    ],
    responses={200: IncidentModerationSerializer(many=True)},
)
class IncidentModerationListView(APIView):
    """GET /api/staff/incidents/?status=&page= : file de moderation des incidents."""

    permission_classes = [IsAdminUser]
    pagination_class = StaffPagination

    def get(self, request):
        statut = request.query_params.get('status')
        if statut:
            incidents = Incident.objects.filter(statut=statut)
        else:
            incidents = Incident.objects.filter(statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE])
        incidents = incidents.order_by('-cree_le')

        paginateur = self.pagination_class()
        page = paginateur.paginate_queryset(incidents, request)
        return paginateur.get_paginated_response(IncidentModerationSerializer(page, many=True).data)


@extend_schema(
    tags=['Staff Incidents'],
    summary='Retirer un signalement',
    description=(
        'Reserve au staff (is_staff). cf. Incident.retirer() -- meme invalidation '
        'du cache de cellule que le retrait par son propre auteur (IncidentDetailView.delete).'
    ),
    request=IncidentRetraitSerializer,
    responses={200: IncidentModerationSerializer},
)
class IncidentRetraitStaffView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, id):
        serializer = IncidentRetraitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        incident = get_object_or_404(Incident, id=id)
        incident.retirer(motif=serializer.validated_data['reason'])
        invalider_cache_cellule(incident.cellule_h3_res8)
        return Response(IncidentModerationSerializer(incident).data)
