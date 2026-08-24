from collections import defaultdict

import h3
from django.contrib.gis.geos import Point
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
    IncidentSerializer,
    VoteIncidentSerializer,
)
from .services import QuotaDepasse, ServiceIncident

DUREE_IDEMPOTENCE_S = 24 * 3600


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
            'cellules', OpenApiTypes.STR, required=True,
            description='Cellules H3 (resolution 8) en hexadecimal, separees par des virgules.',
        ),
    ],
    responses={200: IncidentSerializer(many=True), 400: MessageSerializer},
)
class IncidentsProchesView(APIView):
    """GET /api/incidents/proches/?cellules=<hex,hex,...> : endpoint le plus
    sollicite du module. Cache par cellule H3 res8 (~0.74km², cf. l'index
    cellule_h3_res8+statut du modele), invalide a l'ecriture (creation/vote/
    expiration/retrait) plutot que de compter sur le seul TTL -- cf.
    cache_incidents.py."""

    permission_classes = [AllowAny]

    def get(self, request):
        cellules_hex = [c for c in request.query_params.get('cellules', '').split(',') if c]
        if not cellules_hex:
            return Response(
                {'detail': "'cellules' est requis (cellules H3 hex separees par des virgules)."}, status=400
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
                cache.set(cle_cache_cellule(cellules_int[hex_cell]), donnees, timeout=DUREE_CACHE_CELLULE_S)
                resultats_par_cellule[hex_cell] = donnees

        fusion = [incident for hex_cell in cellules_hex for incident in resultats_par_cellule[hex_cell]]
        return Response(fusion)


@extend_schema(
    tags=['Incidents'],
    summary='Signaler un incident',
    description=(
        "L'en-tete Idempotency-Key est obligatoire (rejeu reseau = meme reponse, "
        "jamais un deuxieme signalement, cle valable "
        f"{DUREE_IDEMPOTENCE_S // 3600}h). Si le signalement est fusionne avec un "
        "incident existant a proximite, 'doublon_de_existant' vaut true et le "
        "statut HTTP est 200 au lieu de 201."
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
        429: OpenApiResponse(MessageSerializer, description='Quota horaire de signalements atteint.'),
    },
)
class IncidentCreationView(APIView):
    """POST /api/incidents/ : Idempotency-Key obligatoire (rejeu reseau =
    meme reponse, jamais un deuxieme signalement)."""

    def post(self, request):
        cle_idempotence = request.headers.get('Idempotency-Key')
        if not cle_idempotence:
            return Response({'detail': "L'en-tete Idempotency-Key est requis."}, status=400)

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
                {'detail': 'Quota horaire de signalements atteint.'}, status=status.HTTP_429_TOO_MANY_REQUESTS
            )

        cache.set(cle_cache, str(incident.id), timeout=DUREE_IDEMPOTENCE_S)

        corps = IncidentSerializer(incident).data
        corps['doublon_de_existant'] = est_doublon
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
                    {'detail': 'Vous ne pouvez pas voter sur votre propre signalement.'}, status=400
                )
            if Vote.objects.filter(incident=incident, votant=request.user).exists():
                return Response({'detail': 'Vous avez deja vote sur ce signalement.'}, status=400)

            vote = Vote.objects.create(
                incident=incident, votant=request.user,
                sens=SensVote.CONFIRMATION if sens == 'confirmer' else SensVote.INFIRMATION,
                poids=request.user.poids_de_vote(),
            )
            if sens == 'confirmer':
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
