from django.db.models import Count, Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.pagination import StaffPagination
from accounts.serializers import MessageSerializer
from ads_admin.models import NiveauNotification, TypeNotification
from ads_admin.services import journaliser, notifier
from places.utils import normaliser

from .importation import FichierIllisible, importer, lire_fichier
from .models import Infraction
from .serializers import (
    CategorieInfractionSerializer,
    ImportInfractionsSerializer,
    InfractionSerializer,
    ResultatImportSerializer,
)


def _filtrer(infractions, params):
    """Filtres communs a la liste publique et a la liste staff."""
    recherche = params.get('search', '').strip()
    if recherche:
        infractions = infractions.filter(
            Q(libelle_normalise__contains=normaliser(recherche))
            | Q(code__icontains=recherche)
            | Q(reference_legale__icontains=recherche)
        )
    categorie = params.get('category', '').strip()
    if categorie:
        infractions = infractions.filter(categorie__iexact=categorie)
    for parametre, filtre in (('min_fine', 'amende_fcfa__gte'), ('max_fine', 'amende_fcfa__lte')):
        valeur = params.get(parametre)
        if valeur and valeur.isdigit():
            infractions = infractions.filter(**{filtre: int(valeur)})
    return infractions


PARAMETRES_FILTRE = [
    OpenApiParameter('search', OpenApiTypes.STR, description='Libelle (sans accents/casse), code ou reference.'),
    OpenApiParameter('category', OpenApiTypes.STR, description='Categorie exacte (insensible a la casse).'),
    OpenApiParameter('min_fine', OpenApiTypes.INT, description='Amende minimale (FCFA).'),
    OpenApiParameter('max_fine', OpenApiTypes.INT, description='Amende maximale (FCFA).'),
]


@extend_schema(
    tags=['Infractions'],
    summary='Lister les infractions routieres et leurs amendes',
    description=(
        "Public. Liste officielle (gendarmerie) des infractions actives, non paginee "
        "(une centaine d'entrees au plus). Triee par categorie puis libelle."
    ),
    parameters=PARAMETRES_FILTRE,
    responses={200: InfractionSerializer(many=True)},
)
class InfractionListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        infractions = _filtrer(Infraction.objects.filter(actif=True), request.query_params)
        return Response(InfractionSerializer(infractions, many=True).data)


@extend_schema(
    tags=['Infractions'],
    summary="Lister les categories d'infractions",
    description="Public. Categories des infractions actives, avec leur nombre -- pour le filtre de l'appli.",
    responses={200: CategorieInfractionSerializer(many=True)},
)
class CategorieInfractionListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        categories = (
            Infraction.objects.filter(actif=True).exclude(categorie='')
            .values('categorie').annotate(nombre=Count('id')).order_by('categorie')
        )
        donnees = [{'category': c['categorie'], 'count': c['nombre']} for c in categories]
        return Response(CategorieInfractionSerializer(donnees, many=True).data)


@extend_schema_view(
    get=extend_schema(
        tags=['Staff Infractions'],
        summary='Lister les infractions (back-office)',
        description='Reserve au staff. Inclut les infractions desactivees ; `active=true/false` filtre dessus.',
        parameters=PARAMETRES_FILTRE + [
            OpenApiParameter('active', OpenApiTypes.BOOL),
            OpenApiParameter('page', OpenApiTypes.INT),
            OpenApiParameter('page_size', OpenApiTypes.INT),
        ],
        responses={200: InfractionSerializer(many=True)},
    ),
    post=extend_schema(
        tags=['Staff Infractions'],
        summary='Creer une infraction',
        request=InfractionSerializer,
        responses={201: InfractionSerializer},
    ),
)
class InfractionModerationListView(APIView):
    permission_classes = [IsAdminUser]
    pagination_class = StaffPagination

    def get(self, request):
        infractions = _filtrer(Infraction.objects.all(), request.query_params)
        actif = request.query_params.get('active')
        if actif is not None:
            infractions = infractions.filter(actif=actif.lower() == 'true')

        paginateur = self.pagination_class()
        page = paginateur.paginate_queryset(infractions.order_by('categorie', 'libelle'), request)
        return paginateur.get_paginated_response(InfractionSerializer(page, many=True).data)

    def post(self, request):
        serializer = InfractionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        tags=['Staff Infractions'],
        summary='Modifier une infraction',
        request=InfractionSerializer,
        responses={200: InfractionSerializer},
    ),
    delete=extend_schema(
        tags=['Staff Infractions'],
        summary='Supprimer une infraction',
        description='Suppression definitive -- preferer is_active=false pour simplement la masquer.',
        responses={204: None},
    ),
)
class InfractionModerationDetailView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, id):
        infraction = get_object_or_404(Infraction, id=id)
        serializer = InfractionSerializer(infraction, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, id):
        get_object_or_404(Infraction, id=id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    tags=['Staff Infractions'],
    summary='Importer des infractions en masse (CSV/JSON)',
    description=(
        "Reserve au staff. Soit `infractions` (liste JSON), soit `file` (CSV ; ou , "
        "ou JSON, multipart). Colonnes reconnues en francais ou anglais : code/numero, "
        "libelle/label, categorie/category, amende/fine_amount (\"25 000 FCFA\" ou "
        "fourchette \"5 000 - 25 000\" acceptes), amende_max, reference, sanctions, actif. "
        "Upsert par code, sinon par libelle. Les lignes invalides sont listees dans "
        "`errors` sans bloquer les autres. `dry_run=true` valide sans rien enregistrer."
    ),
    request={
        'application/json': ImportInfractionsSerializer,
        'multipart/form-data': ImportInfractionsSerializer,
    },
    responses={200: ResultatImportSerializer, 400: MessageSerializer},
)
class ImportInfractionsView(APIView):
    permission_classes = [IsAdminUser]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def post(self, request):
        serializer = ImportInfractionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data

        lignes = donnees.get('infractions')
        if lignes is None:
            fichier = donnees['file']
            try:
                lignes = lire_fichier(fichier.read(), fichier.name)
            except FichierIllisible as exc:
                return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        resultat = importer(lignes, dry_run=donnees['dry_run'])
        if not donnees['dry_run']:
            _suivre_import(request.user, resultat, getattr(donnees.get('file'), 'name', None))
        return Response(resultat)


def _suivre_import(acteur, resultat, nom_fichier):
    """Journal d'audit + notification de fin d'import (pas pour un dry_run)."""
    importees = resultat.get('created', 0) + resultat.get('updated', 0)
    erreurs = len(resultat.get('errors', []))
    libelle = f"{nom_fichier or 'import JSON'} ({importees} lignes)"
    journaliser(acteur, 'infractions.import', type_cible='infraction', libelle=libelle, apres={
        'created': resultat.get('created', 0), 'updated': resultat.get('updated', 0), 'errors': erreurs,
    })
    texte = f'{importees} lignes importées'
    if erreurs:
        texte += f', {erreurs} ignorée{"s" if erreurs > 1 else ""}'
    notifier(
        TypeNotification.IMPORT,
        titre="Import d'infractions terminé",
        texte=texte,
        lien='/infractions',
        niveau=NiveauNotification.ATTENTION if erreurs else NiveauNotification.SUCCES,
    )
