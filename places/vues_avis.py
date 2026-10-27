"""
Avis sur les garages (AvisGarage) : note 1-5 + commentaire, un par
utilisateur et par garage, modifiable. Aucun point de recompense.

Appli :      GET/POST /api/garages/{id}/reviews/, DELETE /api/garages/{id}/reviews/mine/
Back-office : GET /api/staff/reviews/, POST /api/staff/reviews/{id}/remove/
"""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.pagination import StaffPagination
from ads_admin.services import journaliser

from .models import AvisGarage, Lieu, StatutAvis
from .services import garages


class AvisSerializer(serializers.ModelSerializer):
    rating = serializers.IntegerField(source='note')
    comment = serializers.CharField(source='commentaire')
    # Meme regle que les signalements : masque en mode invisible.
    author_name = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(source='cree_le')
    updated_at = serializers.DateTimeField(source='modifie_le')

    class Meta:
        model = AvisGarage
        fields = ['id', 'rating', 'comment', 'author_name', 'created_at', 'updated_at']

    def get_author_name(self, avis):
        return None if avis.utilisateur.mode_invisible else avis.utilisateur.nom_complet


class AvisEcritureSerializer(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')


class AvisModerationSerializer(AvisSerializer):
    status = serializers.CharField(source='statut')
    reason = serializers.CharField(source='motif_retrait')
    author_id = serializers.UUIDField(source='utilisateur_id')
    author_phone = serializers.CharField(source='utilisateur.telephone')
    author_name = serializers.CharField(source='utilisateur.nom_complet')
    garage = serializers.SerializerMethodField()

    class Meta(AvisSerializer.Meta):
        fields = AvisSerializer.Meta.fields + ['status', 'reason', 'author_id', 'author_phone', 'garage']

    def get_garage(self, avis):
        garage = avis.garage
        return {
            'id': str(garage.id), 'kind': 'place' if isinstance(garage, Lieu) else 'establishment',
            'name': garage.nom, 'city': garage.ville,
        }


class RetraitAvisSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


def _resume(garage, utilisateur):
    note = garages.note_de(garage)
    mien = None
    if utilisateur.is_authenticated:
        mien = AvisGarage.objects.filter(garages.filtre_cible(garage), utilisateur=utilisateur).first()
    return {
        'garage': {'id': str(garage.id), 'kind': 'place' if isinstance(garage, Lieu) else 'establishment', 'name': garage.nom},
        'average': note['average'], 'count': note['count'], 'stars': note['stars'],
        # Son propre avis, meme retire par la moderation (status indique).
        'my_review': {**AvisSerializer(mien).data, 'status': mien.statut} if mien else None,
    }


@extend_schema(
    tags=['Garages'],
    summary="Avis d'un garage (lecture) / noter un garage",
    description=(
        "GET (public) : note moyenne, nombre d'avis, mon avis (si connecte) et avis publies, "
        "les plus recents d'abord (page, page_size). POST (connecte) : cree ou remplace mon avis "
        "{rating 1-5, comment?} -> 201 a la creation, 200 a la modification. 404 si l'id n'est "
        "pas un garage (lieu de categorie garage/mecanique ou etablissement GARAGE)."
    ),
    parameters=[OpenApiParameter('page', OpenApiTypes.INT), OpenApiParameter('page_size', OpenApiTypes.INT)],
    request=AvisEcritureSerializer,
)
class AvisGarageView(APIView):
    throttle_scope = 'avis-garage'

    def get_permissions(self):
        return [AllowAny()] if self.request.method == 'GET' else [IsAuthenticated()]

    def get_throttles(self):
        return [ScopedRateThrottle()] if self.request.method == 'POST' else []

    def get(self, request, id):
        garage = garages.trouver_garage(id)
        avis = (
            AvisGarage.objects.filter(garages.filtre_cible(garage), statut=StatutAvis.PUBLIE)
            .select_related('utilisateur').order_by('-modifie_le')
        )
        paginateur = StaffPagination()
        page = paginateur.paginate_queryset(avis, request)
        reponse = paginateur.get_paginated_response(AvisSerializer(page, many=True).data)
        reponse.data.update(_resume(garage, request.user))
        return reponse

    def post(self, request, id):
        garage = garages.trouver_garage(id)
        serializer = AvisEcritureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data
        cible = {'lieu': garage} if isinstance(garage, Lieu) else {'etablissement': garage}
        avis, cree = AvisGarage.objects.update_or_create(
            utilisateur=request.user, **cible,
            # Modifier un avis retire le republie : le motif de retrait reste
            # dans le journal d'audit.
            defaults={
                'note': donnees['rating'], 'commentaire': donnees['comment'].strip(),
                'statut': StatutAvis.PUBLIE, 'motif_retrait': '',
            },
        )
        return Response(
            {**AvisSerializer(avis).data, **_resume(garage, request.user)},
            status=status.HTTP_201_CREATED if cree else status.HTTP_200_OK,
        )


@extend_schema(tags=['Garages'], summary='Supprimer mon avis sur un garage', responses={204: None})
class MonAvisGarageView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, id):
        garage = garages.trouver_garage(id)
        avis = get_object_or_404(AvisGarage, garages.filtre_cible(garage), utilisateur=request.user)
        avis.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    tags=['Staff Garages'],
    summary='Avis sur les garages (moderation)',
    description='Reserve au staff. Filtres : status (PUBLIE/RETIRE/ALL, defaut ALL), user, garage (id).',
    parameters=[
        OpenApiParameter('status', OpenApiTypes.STR), OpenApiParameter('user', OpenApiTypes.UUID),
        OpenApiParameter('garage', OpenApiTypes.UUID), OpenApiParameter('page', OpenApiTypes.INT),
    ],
    responses={200: AvisModerationSerializer(many=True)},
)
class AvisModerationListView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        avis = AvisGarage.objects.select_related('utilisateur', 'lieu', 'etablissement').order_by('-modifie_le')
        params = request.query_params
        if params.get('status') in (StatutAvis.PUBLIE, StatutAvis.RETIRE):
            avis = avis.filter(statut=params['status'])
        if params.get('user'):
            avis = avis.filter(utilisateur_id=params['user'])
        if params.get('garage'):
            avis = avis.filter(lieu_id=params['garage']) | avis.filter(etablissement_id=params['garage'])
        paginateur = StaffPagination()
        page = paginateur.paginate_queryset(avis, request)
        return paginateur.get_paginated_response(AvisModerationSerializer(page, many=True).data)


@extend_schema(
    tags=['Staff Garages'],
    summary='Retirer un avis',
    description="Reserve au staff. L'avis reste en base (statut RETIRE, motif) et sort de la moyenne.",
    request=RetraitAvisSerializer,
    responses={200: AvisModerationSerializer},
)
class AvisRetirerView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, id):
        serializer = RetraitAvisSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        avis = get_object_or_404(AvisGarage.objects.select_related('utilisateur', 'lieu', 'etablissement'), id=id)
        avis.statut, avis.motif_retrait = StatutAvis.RETIRE, serializer.validated_data['reason']
        avis.save(update_fields=['statut', 'motif_retrait', 'modifie_le'])
        journaliser(
            request.user, 'reviews.remove', avis, libelle=f'{avis.note}/5 sur {avis.garage.nom}',
            apres={'motif': avis.motif_retrait},
        )
        return Response(AvisModerationSerializer(avis).data)
