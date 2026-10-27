"""GET /api/places/nearby/ : garages, hopitaux, stations-service et
pharmacies proches (cf. services/proximite.py)."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .services import proximite


class ProximiteParametresSerializer(serializers.Serializer):
    lat = serializers.FloatField(min_value=-90, max_value=90)
    lon = serializers.FloatField(min_value=-180, max_value=180)
    radius_m = serializers.IntegerField(
        min_value=100, max_value=proximite.RAYON_MAX_M, required=False, default=proximite.RAYON_DEFAUT_M,
    )
    types = serializers.CharField(required=False, default='')
    limit = serializers.IntegerField(
        min_value=1, max_value=proximite.LIMITE_MAX, required=False, default=proximite.LIMITE_DEFAUT,
    )
    open_now = serializers.BooleanField(required=False, default=False)

    def validate_types(self, brut):
        types = [t.strip().lower() for t in brut.split(',') if t.strip()] or list(proximite.TYPES)
        inconnus = sorted(set(types) - set(proximite.TYPES))
        if inconnus:
            raise serializers.ValidationError(f'Unknown types: {", ".join(inconnus)}. Allowed: {", ".join(proximite.TYPES)}.')
        return types


@extend_schema(
    tags=['Places'],
    summary='Garages, hopitaux, stations et pharmacies a proximite',
    description=(
        f'Du plus proche au plus loin, dans radius_m (defaut {proximite.RAYON_DEFAUT_M}, '
        f'100 a {proximite.RAYON_MAX_M}). types = garage,hopital,station,pharmacie (defaut : tous). '
        "Stations et pharmacies : opening_hours (null si inconnus) et is_open_now (null si inconnu) ; "
        'open_now=true ecarte celles fermees maintenant (horaires inconnus gardes). Garages : rating_average, '
        'rating_count, stars (etoiles ponderees par le nombre d\'avis). hopital couvre aussi '
        'cliniques et centres de sante.'
    ),
    parameters=[
        OpenApiParameter('lat', OpenApiTypes.FLOAT, required=True),
        OpenApiParameter('lon', OpenApiTypes.FLOAT, required=True),
        OpenApiParameter('radius_m', OpenApiTypes.INT),
        OpenApiParameter('types', OpenApiTypes.STR, description='Liste separee par des virgules.'),
        OpenApiParameter('limit', OpenApiTypes.INT),
        OpenApiParameter('open_now', OpenApiTypes.BOOL),
    ],
)
class ProximiteView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        serializer = ProximiteParametresSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        p = serializer.validated_data
        resultats = proximite.a_proximite(
            p['lat'], p['lon'], p['radius_m'], p['types'], p['limit'], proximite.maintenant_local(),
        )
        if p['open_now']:
            # Les types sans horaires (garages, hopitaux) restent : rien ne dit qu'ils sont fermes.
            resultats = [r for r in resultats if r.get('is_open_now') is not False]
        return Response({'radius_m': p['radius_m'], 'count': len(resultats), 'results': resultats})
