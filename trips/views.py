from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import TransitionInvalide
from .models import StatutTrajet, Trajet
from .serializers import (
    CalculItineraireSerializer,
    ItineraireCandidatSerializer,
    NoterTrajetSerializer,
    TrajetCreationSerializer,
    TrajetMiseAJourSerializer,
    TrajetSerializer,
)
from .services.service_itineraire import ServiceItineraire

DUREE_PERIODE = {
    'semaine': timedelta(days=7),
    'mois': timedelta(days=30),
}


class CalculItineraireView(APIView):
    """POST /api/itineraires/calculer/ : view -> ServiceItineraire -> ClientValhalla.
    Ne persiste rien -- le client renvoie l'itineraire choisi a POST /api/trajets/."""

    def post(self, request):
        serializer = CalculItineraireSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data

        candidats = ServiceItineraire().calculer(
            depart=(donnees['origine_lat'], donnees['origine_lon']),
            arrivee=(donnees['destination_lat'], donnees['destination_lon']),
            utilisateur=request.user,
        )
        return Response(ItineraireCandidatSerializer(candidats, many=True).data)


class TrajetListeCreationView(APIView):
    """GET ?periode=semaine|mois|tout respecte la retention du plan (Droits.
    retention_historique_jours) -- tronque toujours 'tout', jamais l'inverse."""

    def get(self, request):
        periode = request.query_params.get('periode', 'tout')
        if periode not in (*DUREE_PERIODE, 'tout'):
            return Response({'detail': "periode doit etre 'semaine', 'mois' ou 'tout'."}, status=400)

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
            'resultats': TrajetSerializer(queryset, many=True).data,
            'tronque_le': tronque_le.isoformat() if tronque_le else None,
        })

    def post(self, request):
        serializer = TrajetCreationSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        trajet = serializer.save()
        return Response(TrajetSerializer(trajet).data, status=status.HTTP_201_CREATED)


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


class NoterTrajetView(APIView):
    def post(self, request, id):
        trajet = get_object_or_404(Trajet, id=id, utilisateur=request.user)
        if trajet.statut != StatutTrajet.TERMINE:
            return Response(
                {'detail': 'Seul un trajet termine peut etre note.'}, status=status.HTTP_400_BAD_REQUEST
            )
        serializer = NoterTrajetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        trajet.noter(**serializer.validated_data)
        return Response(TrajetSerializer(trajet).data)
