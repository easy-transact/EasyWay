from difflib import SequenceMatcher

from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.contrib.postgres.search import TrigramSimilarity
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import MessageSerializer

from .models import AdresseEnregistree, Lieu, RechercheRecente, StatutLieu
from .serializers import (
    AdresseEnregistreeSerializer,
    LieuDetailSerializer,
    LieuPropositionSerializer,
    LieuRechercheSerializer,
    RechercheRecenteSerializer,
)
from .services.client_nominatim import ClientNominatim
from .services.client_photon import ClientPhoton
from .utils import normaliser

# 0.15 laissait passer trop de bruit (ex. "Avenue Kennedy" faisait remonter
# "Kenya Airways" a 0.16) : verifie empiriquement sur des requetes reelles que
# 0.25 coupe le bruit sans perdre les correspondances partielles legitimes.
SEUIL_SIMILARITE = 0.25
LIMITE_LOCALE = 12  # reserve toujours au moins 8 places a Nominatim sur les 20
LIMITE_TOTALE = 20
RAYON_INVERSE_M = 50
NB_RECHERCHES_RECENTES_MAX = 10


@extend_schema(
    tags=['Lieux'],
    summary='Rechercher un lieu (autocompletion)',
    description=(
        "Trigram local (nom_normalise) fusionne avec Photon (P2b) -- source='local'|'photon' "
        'par resultat. Photon, pas Nominatim, pour la recherche : autocompletion rapide ; '
        "Nominatim reste dedie au geocodage inverse (voir /lieux/inverse/). "
        'lat/lon, si fournis, influencent uniquement le classement -- jamais un filtre '
        "qui exclurait un resultat pertinent situe loin de l'utilisateur."
    ),
    parameters=[
        OpenApiParameter('q', OpenApiTypes.STR, required=True, description='Texte tape (min. 2 caracteres).'),
        OpenApiParameter('lat', OpenApiTypes.FLOAT, description='Latitude de reference pour le classement.'),
        OpenApiParameter('lon', OpenApiTypes.FLOAT, description='Longitude de reference pour le classement.'),
    ],
    responses={200: LieuRechercheSerializer(many=True), 400: MessageSerializer},
)
class RechercheView(APIView):
    """GET /api/lieux/recherche/?q=&lat=&lon= : trigram local (nom_normalise)
    fusionne avec Photon (P2b) -- source='local'|'photon' par resultat.
    Photon, pas Nominatim, pour la recherche : c'est son role (autocompletion
    rapide) ; Nominatim reste dedie a l'inverse (InverseView), le seul des
    deux a exposer un endpoint de geocodage inverse.

    La position ne filtre jamais les resultats locaux -- seulement leur
    classement. Un lieu peut correspondre au texte tape sans etre pres de
    l'utilisateur (ex. il tape le nom exact d'un lieu dans une autre ville) ;
    l'exclure sur la distance ferait disparaitre un resultat pertinent
    plutot que de simplement le classer plus bas."""

    permission_classes = [AllowAny]

    def get(self, request):
        q = request.query_params.get('q', '').strip()
        if len(q) < 2:
            return Response({'detail': "Le parametre 'q' doit contenir au moins 2 caracteres."}, status=400)

        lat = request.query_params.get('lat')
        lon = request.query_params.get('lon')
        position = (float(lat), float(lon)) if lat is not None and lon is not None else None

        requete = Lieu.objects.filter(statut=StatutLieu.APPROUVE).annotate(
            similarite=TrigramSimilarity('nom_normalise', normaliser(q))
        ).filter(similarite__gt=SEUIL_SIMILARITE)

        if position is not None:
            point = Point(position[1], position[0], srid=4326)
            requete = requete.annotate(distance=Distance('position', point)).order_by('-similarite', 'distance')
        else:
            requete = requete.order_by('-similarite', '-score_popularite')

        # Plafond local volontairement < LIMITE_TOTALE : sans ca, une requete
        # avec beaucoup de correspondances locales faibles (mais > SEUIL_SIMILARITE)
        # remplit toute la reponse et Photon n'apparait jamais, meme quand il a
        # la correspondance la plus pertinente (ex. un lieu mappe en way/relation
        # OSM, que seed_places n'importe pas encore -- seuls les nodes le sont).
        resultats_locaux = LieuRechercheSerializer(requete[:LIMITE_LOCALE], many=True).data
        noms_vus = {normaliser(r['libelle']) for r in resultats_locaux}

        # Deduplique aussi entre resultats externes : une rue mappee en
        # plusieurs troncons OSM (donc plusieurs osm_id) revient sinon comme
        # autant de doublons portant exactement le meme nom affiche.
        resultats_externes = []
        for r in ClientPhoton().rechercher(q, autour=position):
            nom = normaliser(r['libelle'])
            if nom in noms_vus:
                continue
            noms_vus.add(nom)
            resultats_externes.append(r)

        # Classement homogene sur les deux sources : sans ca, "local d'abord,
        # externe ensuite" fait passer une correspondance locale faible et
        # lointaine (ex. a 700 km) avant la meilleure correspondance Photon,
        # meme quasi-exacte -- exactement le bug observe sur "Hopital Laquintinie".
        # SequenceMatcher (stdlib) n'est pas le trigram Postgres utilise pour la
        # selection des candidats locaux ci-dessus, mais donne une mesure
        # comparable entre les deux sources pour ce seul classement final.
        q_normalise = normaliser(q)

        def cle_tri(resultat):
            pertinence = SequenceMatcher(None, q_normalise, normaliser(resultat['libelle'])).ratio()
            distance = resultat.get('distance_m')
            return (-pertinence, distance if distance is not None else float('inf'))

        fusion = sorted(resultats_locaux + resultats_externes, key=cle_tri)
        return Response(fusion[:LIMITE_TOTALE])


@extend_schema(
    tags=['Lieux'],
    summary='Geocodage inverse (position -> libelle)',
    description=(
        'Nominatim en priorite (P2b), repli sur le lieu approuve le plus proche '
        f'en local (rayon {RAYON_INVERSE_M} m) si indisponible/sans resultat.'
    ),
    parameters=[
        OpenApiParameter('lat', OpenApiTypes.FLOAT, required=True, description='Latitude.'),
        OpenApiParameter('lon', OpenApiTypes.FLOAT, required=True, description='Longitude.'),
    ],
    responses={
        200: inline_serializer(
            name='InverseReponse',
            fields={
                'libelle': drf_serializers.CharField(),
                'lieu': LieuRechercheSerializer(allow_null=True),
            },
        ),
        400: MessageSerializer,
    },
)
class InverseView(APIView):
    """GET /api/lieux/inverse/?lat=&lon= : Nominatim en priorite (P2b), repli
    sur le lieu approuve le plus proche en local si indisponible/sans resultat."""

    permission_classes = [AllowAny]

    def get(self, request):
        lat = request.query_params.get('lat')
        lon = request.query_params.get('lon')
        if lat is None or lon is None:
            return Response({'detail': "'lat' et 'lon' sont requis."}, status=400)
        lat, lon = float(lat), float(lon)

        resultat_externe = ClientNominatim().inverser(lat, lon)
        if resultat_externe is not None:
            return Response({'libelle': resultat_externe['libelle'], 'lieu': resultat_externe})

        point = Point(lon, lat, srid=4326)
        lieu = Lieu.objects.filter(statut=StatutLieu.APPROUVE).annotate(
            distance=Distance('position', point)
        ).filter(distance__lte=D(m=RAYON_INVERSE_M)).order_by('distance').first()

        if lieu is None:
            return Response({'libelle': 'Position actuelle', 'lieu': None})
        return Response({'libelle': lieu.nom, 'lieu': LieuRechercheSerializer(lieu).data})


@extend_schema(
    tags=['Lieux'],
    summary="Detail d'un lieu approuve",
    responses={200: LieuDetailSerializer, 404: OpenApiResponse(description='Lieu introuvable ou non approuve.')},
)
class LieuDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, id):
        lieu = get_object_or_404(Lieu, id=id, statut=StatutLieu.APPROUVE)
        return Response(LieuDetailSerializer(lieu).data)


@extend_schema(
    tags=['Lieux'],
    summary='Proposer un nouveau lieu',
    description='Soumission utilisateur, mise en file de moderation (statut EN_ATTENTE).',
    request=LieuPropositionSerializer,
    responses={201: LieuDetailSerializer},
)
class ProposerLieuView(APIView):
    def post(self, request):
        serializer = LieuPropositionSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        lieu = serializer.save()
        return Response(LieuDetailSerializer(lieu).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        tags=['Adresses enregistrees'],
        summary='Lister les adresses enregistrees du compte connecte',
        responses={200: AdresseEnregistreeSerializer(many=True)},
    ),
    post=extend_schema(
        tags=['Adresses enregistrees'],
        summary='Enregistrer une nouvelle adresse',
        description="Rejette avec 403 au-dela de max_adresses_enregistrees (limite de la formule, cf. Droits).",
        request=AdresseEnregistreeSerializer,
        responses={201: AdresseEnregistreeSerializer, 403: MessageSerializer},
    ),
)
class AdresseEnregistreeListCreateView(APIView):
    def get(self, request):
        adresses = request.user.adresses_enregistrees.all()
        return Response(AdresseEnregistreeSerializer(adresses, many=True).data)

    def post(self, request):
        limite = request.user.droits.max_adresses_enregistrees
        if limite is not None and request.user.adresses_enregistrees.count() >= limite:
            return Response(
                {'detail': f"Limite de {limite} adresses enregistrees atteinte pour votre formule."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = AdresseEnregistreeSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        adresse = serializer.save()
        return Response(AdresseEnregistreeSerializer(adresse).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(
        tags=['Adresses enregistrees'],
        summary='Mettre a jour partiellement une adresse enregistree',
        request=AdresseEnregistreeSerializer,
        responses={200: AdresseEnregistreeSerializer},
    ),
    delete=extend_schema(
        tags=['Adresses enregistrees'],
        summary='Supprimer une adresse enregistree',
        responses={204: None},
    ),
)
class AdresseEnregistreeDetailView(APIView):
    def _objet(self, request, id):
        return get_object_or_404(AdresseEnregistree, id=id, utilisateur=request.user)

    def patch(self, request, id):
        adresse = self._objet(request, id)
        serializer = AdresseEnregistreeSerializer(
            adresse, data=request.data, partial=True, context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, id):
        self._objet(request, id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(
        tags=['Recherches recentes'],
        summary='Lister les recherches recentes',
        responses={200: RechercheRecenteSerializer(many=True)},
    ),
    post=extend_schema(
        tags=['Recherches recentes'],
        summary='Ajouter une recherche recente',
        description=f'Purge automatiquement au-dela des {NB_RECHERCHES_RECENTES_MAX} entrees les plus recentes.',
        request=RechercheRecenteSerializer,
        responses={201: RechercheRecenteSerializer},
    ),
    delete=extend_schema(
        tags=['Recherches recentes'],
        summary="Vider l'historique de recherches",
        responses={204: None},
    ),
)
class RechercheRecenteView(APIView):
    """GET liste / POST ajoute (purge au-dela de NB_RECHERCHES_RECENTES_MAX) /
    DELETE vide l'historique de l'utilisateur connecte."""

    def get(self, request):
        recherches = request.user.recherches_recentes.all()[:NB_RECHERCHES_RECENTES_MAX]
        return Response(RechercheRecenteSerializer(recherches, many=True).data)

    def post(self, request):
        serializer = RechercheRecenteSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()

        anciennes = request.user.recherches_recentes.order_by('-recherche_le').values_list(
            'id', flat=True
        )[NB_RECHERCHES_RECENTES_MAX:]
        if anciennes:
            RechercheRecente.objects.filter(id__in=list(anciennes)).delete()

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def delete(self, request):
        request.user.recherches_recentes.all().delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
