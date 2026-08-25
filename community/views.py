import math
from collections import defaultdict

import h3
from django.contrib.gis.geos import LineString, Point
from django.contrib.gis.measure import D
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import MessageSerializer

from .cache_incidents import DUREE_CACHE_CELLULE_S, cle_cache_cellule, invalider_cache_cellule
from .models import Incident, SensVote, StatutIncident, Vote
from .serializers import (
    IncidentAvecDoublonSerializer,
    IncidentCreationSerializer,
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


def _distance_m(lat1, lon1, lat2, lon2):
    rayon_terre_m = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)
    a = math.sin(delta_lat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(delta_lon / 2) ** 2
    return 2 * rayon_terre_m * math.asin(math.sqrt(a))


@extend_schema(
    tags=['Incidents'],
    summary='Lister les incidents actifs sur des cellules H3',
    description=(
        'Endpoint le plus sollicite du module. Cache par cellule H3 resolution 8 '
        "(~0.74km²), invalide a l'ecriture (creation/vote/expiration/retrait) plutot "
        'que de compter sur le seul TTL -- cf. cache_incidents.py.'
    ),
    parameters=[
        OpenApiParameter(
            'cells', OpenApiTypes.STR, required=True,
            description=f'Cellules H3 (resolution 8) en hexadecimal, separees par des virgules. Maximum {MAX_CELLULES}.',
        ),
        OpenApiParameter(
            'lat', OpenApiTypes.FLOAT, required=False,
            description="Position de reference pour le tri par pertinence (optionnel). Sans elle, tri par gravite/confiance seules.",
        ),
        OpenApiParameter(
            'lon', OpenApiTypes.FLOAT, required=False,
            description="Position de reference pour le tri par pertinence (optionnel).",
        ),
    ],
    responses={200: IncidentSerializer(many=True), 400: MessageSerializer},
)
class IncidentsProchesView(APIView):
    """GET /api/incidents/nearby/?cells=<hex,hex,...>&lat=&lon= : endpoint le
    plus sollicite du module. Cache par cellule H3 res8 (~0.74km², cf. l'index
    cellule_h3_res8+statut du modele), invalide a l'ecriture (creation/vote/
    expiration/retrait) plutot que de compter sur le seul TTL -- cf.
    cache_incidents.py.

    MAX_CELLULES/MAX_RESULTATS : le decoupage H3 cote client n'a pas de
    limite fiable (viewport dezoome, bug de calcul) -- c'est le serveur qui
    doit refuser une requete trop large et plafonner/trier la reponse,
    puisqu'il est seul a connaitre le volume reel derriere chaque cellule."""

    permission_classes = [AllowAny]

    def get(self, request):
        cellules_hex = [c for c in request.query_params.get('cells', '').split(',') if c]
        if not cellules_hex:
            return Response(
                {'detail': "'cells' is required (comma-separated H3 hex cells)."}, status=400
            )
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

        lat = request.query_params.get('lat')
        lon = request.query_params.get('lon')
        if (lat is None) != (lon is None):
            return Response({'detail': "'lat' and 'lon' must be provided together."}, status=400)
        if lat is not None:
            try:
                lat, lon = float(lat), float(lon)
            except ValueError:
                return Response({'detail': "'lat'/'lon' must be numbers."}, status=400)

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
    """POST /api/incidents/along-route/ : couloir de `buffer_m` metres autour
    d'une geometrie de route complete -- cf. IncidentsSurTrajetSerializer."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = IncidentsSurTrajetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        points = serializer.validated_data['geometry']
        buffer_m = serializer.validated_data['buffer_m']

        ligne = LineString(points, srid=4326)
        incidents = Incident.objects.filter(
            statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE],
            expire_le__gt=timezone.now(),
            position__distance_lte=(ligne, D(m=buffer_m)),
        )

        # Meme logique de pertinence que /nearby/ sans position de reference
        # (aucune ici -- le trajet entier est demande, pas un point) :
        # gravite puis confiance, et un plafond identique pour ne jamais
        # renvoyer un trajet tres long en entier sans limite.
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
        "(> 50m), le signalement est rejete en 400 -- verification desactivee "
        "sans bloquer le signalement si Valhalla est indisponible."
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
    get=extend_schema(tags=['Incidents'], summary="Detail d'un incident", responses={200: IncidentSerializer}),
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
        incident = get_object_or_404(Incident, id=id)
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
