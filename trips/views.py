from datetime import timedelta

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import MessageSerializer

from .exceptions import TransitionInvalide
from .models import StatutTrajet, Trajet
from .serializers import (
    CalculItineraireSerializer,
    ItineraireCandidatSerializer,
    NoterTrajetSerializer,
    TelemetriePositionsSerializer,
    TrajetCreationSerializer,
    TrajetMiseAJourSerializer,
    TrajetSerializer,
)
from .services.producteur_evenements import FLUX_POSITIONS, ProducteurRedisStreams
from .services.service_itineraire import ServiceItineraire

DUREE_PERIODE = {
    'week': timedelta(days=7),
    'month': timedelta(days=30),
}


@extend_schema(
    tags=['Routes'],
    summary="Calculer des candidats d'itineraire",
    description=(
        'view -> ServiceItineraire -> ClientValhalla. Ne persiste rien -- le client '
        "renvoie l'itineraire choisi tel quel a POST /api/trips/ pour le faire persister. "
        "'avoid' (optionnel) exclut reellement les points donnes du graphe de routage "
        "(ex. position d'un incident) -- Valhalla replanifie autour, ce n'est pas un "
        "simple reclassement des candidats existants. 'origin_heading' (optionnel) evite "
        "qu'un recalcul en cours de route demarre par un demi-tour immediat sur une voie "
        'a sens unique ou une chaussee separee.'
    ),
    request=CalculItineraireSerializer,
    responses={200: ItineraireCandidatSerializer(many=True)},
)
class CalculItineraireView(APIView):
    """POST /api/routes/calculate/ : view -> ServiceItineraire -> ClientValhalla.
    Ne persiste rien -- le client renvoie l'itineraire choisi a POST /api/trips/."""

    def post(self, request):
        serializer = CalculItineraireSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data

        candidats = ServiceItineraire().calculer(
            depart=(donnees['origine_lat'], donnees['origine_lon']),
            arrivee=(donnees['destination_lat'], donnees['destination_lon']),
            utilisateur=request.user,
            eviter=[(p['lat'], p['lon']) for p in donnees['eviter']],
            cap_origine=donnees['cap_origine'],
        )
        return Response(ItineraireCandidatSerializer(candidats, many=True).data)


@extend_schema_view(
    get=extend_schema(
        operation_id='trajets_lister',
        tags=['Trips'],
        summary="Lister les trajets de l'utilisateur connecte",
        description=(
            "period=week|month|all respecte la retention du plan (Droits."
            "retention_historique_jours) -- tronque toujours 'all', jamais l'inverse. "
            "'truncated_at' est non-null quand la retention du plan a effectivement exclu des trajets."
        ),
        parameters=[
            OpenApiParameter(
                'period', OpenApiTypes.STR, enum=['week', 'month', 'all'], default='all',
            ),
        ],
        responses={
            200: inline_serializer(
                name='TrajetListeReponse',
                fields={
                    'results': TrajetSerializer(many=True),
                    'truncated_at': drf_serializers.DateTimeField(allow_null=True),
                },
            ),
            400: MessageSerializer,
        },
    ),
    post=extend_schema(
        tags=['Trips'],
        summary='Creer un trajet a partir d\'un itineraire choisi',
        description="Demarre immediatement le trajet (PLANIFIE -> ACTIF via la machine a etats).",
        request=TrajetCreationSerializer,
        responses={201: TrajetSerializer},
    ),
)
class TrajetListeCreationView(APIView):
    """GET ?period=week|month|all respecte la retention du plan (Droits.
    retention_historique_jours) -- tronque toujours 'all', jamais l'inverse."""

    def get(self, request):
        periode = request.query_params.get('period', 'all')
        if periode not in (*DUREE_PERIODE, 'all'):
            return Response({'detail': "period must be 'week', 'month' or 'all'."}, status=400)

        queryset = request.user.trajets.order_by('-demarre_le')

        retention_jours = request.user.droits.retention_historique_jours
        limite_retention = timezone.now() - timedelta(days=retention_jours) if retention_jours else None

        depuis = timezone.now() - DUREE_PERIODE[periode] if periode in DUREE_PERIODE else None
        tronque_le = None
        if limite_retention is not None and (depuis is None or limite_retention > depuis):
            # La retention du plan est la contrainte la plus stricte -- mais ne
            # vaut la peine d'etre signalee que si elle exclut reellement des
            # trajets, pas juste parce qu'un plafond existe en theorie.
            if queryset.filter(demarre_le__lt=limite_retention).exists():
                depuis = limite_retention
                tronque_le = limite_retention

        if depuis is not None:
            queryset = queryset.filter(demarre_le__gte=depuis)

        return Response({
            'results': TrajetSerializer(queryset, many=True).data,
            'truncated_at': tronque_le.isoformat() if tronque_le else None,
        })

    def post(self, request):
        serializer = TrajetCreationSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        trajet = serializer.save()
        return Response(TrajetSerializer(trajet).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        operation_id='trajets_detail', tags=['Trips'], summary="Detail d'un trajet",
        responses={200: TrajetSerializer},
    ),
    patch=extend_schema(
        tags=['Trips'],
        summary="Mettre a jour un trajet (statut, mesures reelles)",
        description='changer_statut() applique la machine a etats du trajet ; une transition illegale renvoie 400.',
        request=TrajetMiseAJourSerializer,
        responses={200: TrajetSerializer, 400: MessageSerializer},
    ),
    delete=extend_schema(tags=['Trips'], summary='Supprimer un trajet', responses={204: None}),
)
class TrajetDetailView(APIView):
    def _objet(self, request, id):
        return get_object_or_404(Trajet, id=id, utilisateur=request.user)

    def get(self, request, id):
        return Response(TrajetSerializer(self._objet(request, id)).data)

    def patch(self, request, id):
        trajet = self._objet(request, id)
        serializer = TrajetMiseAJourSerializer(trajet, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            serializer.save()
        except TransitionInvalide as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TrajetSerializer(trajet).data)

    def delete(self, request, id):
        self._objet(request, id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    tags=['Trips'],
    summary='Noter un trajet termine',
    description='Refuse avec 400 si le trajet n\'est pas au statut TERMINE.',
    request=NoterTrajetSerializer,
    responses={200: TrajetSerializer, 400: MessageSerializer},
)
class NoterTrajetView(APIView):
    def post(self, request, id):
        trajet = get_object_or_404(Trajet, id=id, utilisateur=request.user)
        if trajet.statut != StatutTrajet.TERMINE:
            return Response(
                {'detail': 'Only a completed trip can be rated.'}, status=status.HTTP_400_BAD_REQUEST
            )
        serializer = NoterTrajetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        trajet.noter(**serializer.validated_data)
        return Response(TrajetSerializer(trajet).data)


@extend_schema(
    tags=['Telemetry'],
    summary='Ingerer un lot de positions GPS',
    description=(
        'Valide et publie sur le flux Redis Streams (ProducteurEvenements), '
        "retourne 202 immediatement -- aucune ecriture en base sur ce chemin de "
        "requete : les positions brutes ne sont jamais persistees, seul leur "
        "agregat 5 minutes (EchantillonVitesse) l'est plus tard, cote consommateur. "
        "Suppression silencieuse (202, rien publie) si l'utilisateur a active "
        'mode_invisible. Le trajet doit appartenir a l\'appelant et etre ACTIF.'
    ),
    request=TelemetriePositionsSerializer,
    responses={202: None, 400: MessageSerializer},
)
class TelemetriePositionsView(APIView):
    def post(self, request):
        if request.user.mode_invisible:
            return Response(status=status.HTTP_202_ACCEPTED)

        serializer = TelemetriePositionsSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        trajet = serializer.validated_data['trajet']

        producteur = ProducteurRedisStreams()
        for position in serializer.validated_data['positions']:
            producteur.publier(FLUX_POSITIONS, {
                'trajet_id': str(trajet.id),
                'lat': position['lat'],
                'lon': position['lon'],
                'vitesse_kmh': position.get('vitesse_kmh'),
                'cap': position.get('cap'),
                'horodatage': position['horodatage'].isoformat(),
            })  # jamais d'identifiant utilisateur publie -- trajet_id suffit au
            # regroupement cote consommateur (cf. cahier des charges, confidentialite)

        return Response(status=status.HTTP_202_ACCEPTED)
