from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, F, Max, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
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
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from ads_admin.models import EntreeAudit
from ads_admin.services import FENETRE_ABUS, SEUIL_RETRAITS_ABUS, journaliser
from community.models import Incident, StatutIncident, TypeIncident, Vote
from places.models import Lieu, StatutLieu
from trips.models import StatutTrajet, Trajet

from .config_data import VERSION_MINIMALE_APP, VILLES_DISPONIBLES
from .emails import envoyer_email_reinitialisation, envoyer_email_verification
from .models import Appareil, InscriptionListeAttente, Parametres, ProfilListeAttente, TypeVehicule, Utilisateur
from .pagination import StaffPagination
from .serializers import (
    AppareilSerializer,
    AvatarSerializer,
    BanUtilisateurSerializer,
    ConfirmationReinitialisationSerializer,
    ConnexionGoogleSerializer,
    ConnexionSerializer,
    DemandeReinitialisationSerializer,
    ExisteSerializer,
    FormuleUtilisateurSerializer,
    InscriptionSerializer,
    JetonsSerializer,
    ListeAttenteModerationSerializer,
    ListeAttenteSerializer,
    ListeAttenteSuiviSerializer,
    MessageSerializer,
    ParametresSerializer,
    RemiseAZeroPointsSerializer,
    UtilisateurMiseAJourSerializer,
    UtilisateurModerationSerializer,
    UtilisateurSerializer,
    VerifierExistenceSerializer,
)
from .tokens import email_verification_token


def _reponse_authentification(nom):
    """Schema de reponse {user, tokens} partage par inscription/connexion."""
    return inline_serializer(
        name=nom,
        fields={'user': UtilisateurSerializer(), 'tokens': JetonsSerializer()},
    )


def _jetons_pour(utilisateur):
    rafraichissement = RefreshToken.for_user(utilisateur)
    return {'access': str(rafraichissement.access_token), 'refresh': str(rafraichissement)}


def _decoder_uid(uidb64):
    """Retourne l'utilisateur cible d'un lien email, ou None si le lien est mal forme."""
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        return Utilisateur.objects.get(pk=uid)
    except (Utilisateur.DoesNotExist, ValueError, TypeError, DjangoValidationError):
        return None


@extend_schema(
    tags=['Authentication'],
    summary="Verifier si un email est deja associe a un compte",
    description=(
        "Premier temps de la connexion en deux temps (section 4.1) : le client "
        "appelle cet endpoint avant d'afficher le formulaire mot de passe, pour "
        "savoir s'il doit proposer une connexion ou une inscription."
    ),
    request=VerifierExistenceSerializer,
    responses={200: ExisteSerializer},
)
class VerifierExistenceView(APIView):
    """Premier temps de la connexion en deux temps (section 4.1). Repond par
    nature "ce compte existe ou non" : limite de debit pour empecher de tester
    des numeros en masse (audit securite du 07/10)."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'verification-existence'

    def post(self, request):
        serializer = VerifierExistenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        existe = Utilisateur.objects.filter(telephone=serializer.validated_data['telephone']).exists()
        return Response({'exists': existe})


@extend_schema(
    tags=['Authentication'],
    summary='Creer un compte (UC-01)',
    description=(
        'Cree le compte et ses Parametres par defaut (Droits est resolu '
        "dynamiquement depuis la formule, cf. Utilisateur.droits), envoie le lien "
        'de verification par email et retourne directement les jetons JWT (acces '
        'complet des la creation, cf. postconditions de UC-01).'
    ),
    request=InscriptionSerializer,
    responses={201: _reponse_authentification('InscriptionReponse')},
)
class InscriptionView(APIView):
    """UC-01 : cree le compte et ses Parametres par defaut (Droits est resolu
    dynamiquement depuis la formule, cf. Utilisateur.droits), envoie le lien
    de verification et retourne directement les jetons (acces complet des la
    creation, cf. postconditions de UC-01)."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'inscription'

    def post(self, request):
        serializer = InscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        utilisateur = serializer.save()
        if utilisateur.email:
            envoyer_email_verification(utilisateur)
        return Response(
            {
                'user': UtilisateurSerializer(utilisateur, context={'request': request}).data,
                'tokens': _jetons_pour(utilisateur),
            },
            status=status.HTTP_201_CREATED,
        )


@extend_schema(
    tags=['Authentication'],
    summary='Connexion par email/mot de passe',
    description='Second temps de la connexion en deux temps, apres VerifierExistenceView.',
    request=ConnexionSerializer,
    responses={200: _reponse_authentification('ConnexionReponse')},
)
class ConnexionView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'connexion'

    def post(self, request):
        serializer = ConnexionSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        utilisateur = serializer.validated_data['utilisateur']
        return Response(
            {
                'user': UtilisateurSerializer(utilisateur, context={'request': request}).data,
                'tokens': _jetons_pour(utilisateur),
            }
        )


@extend_schema(
    tags=['Authentication'],
    summary='Connexion / inscription via Google',
    description=(
        "Section 4.1 'Connexion avec Google' : verifie le jeton d'identite Google "
        'cote serveur, lie un compte existant si l\'adresse email verifiee est deja '
        'connue, sinon en cree un nouveau. Rejette avec 403 si le compte est banni.'
    ),
    request=ConnexionGoogleSerializer,
    responses={
        200: _reponse_authentification('ConnexionGoogleReponse'),
        403: OpenApiResponse(MessageSerializer, description='Banned account.'),
    },
)
class ConnexionGoogleView(APIView):
    """Section 4.1 'Connexion avec Google' : lie un compte existant si l'adresse
    email verifiee est deja connue, sinon en cree un nouveau."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'connexion'

    def post(self, request):
        serializer = ConnexionGoogleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        identite = serializer.validated_data['identite']

        utilisateur = Utilisateur.objects.filter(identifiant_google=identite.identifiant).first()

        if utilisateur is None:
            utilisateur = Utilisateur.objects.filter(email__iexact=identite.email).first()
            if utilisateur is not None:
                utilisateur.identifiant_google = identite.identifiant
                if identite.email_verifie:
                    utilisateur.email_verifie = True
                utilisateur.save(update_fields=['identifiant_google', 'email_verifie'])

        if utilisateur is None:
            utilisateur = Utilisateur.objects.create_user(
                email=identite.email,
                password=None,
                nom_complet=identite.nom_complet or identite.email,
                url_avatar=identite.url_avatar,
                identifiant_google=identite.identifiant,
                email_verifie=identite.email_verifie,
                cgu_acceptee_le=timezone.now(),
            )
            Parametres.objects.create(utilisateur=utilisateur)

        if utilisateur.est_banni:
            return Response({'detail': 'This account is banned.'}, status=status.HTTP_403_FORBIDDEN)

        return Response(
            {
                'user': UtilisateurSerializer(utilisateur, context={'request': request}).data,
                'tokens': _jetons_pour(utilisateur),
            }
        )


@extend_schema(
    tags=['Authentication'],
    summary='Deconnexion (revocation du refresh token)',
    description=(
        'ServiceAuthentification.revoquerFamille(jeton) : place le refresh token '
        'sur liste noire pour empecher toute nouvelle rotation.'
    ),
    request=inline_serializer(
        name='DeconnexionRequete',
        fields={'refresh': drf_serializers.CharField()},
    ),
    responses={204: None, 400: MessageSerializer},
)
class DeconnexionView(APIView):
    """ServiceAuthentification.revoquerFamille(jeton) : place le refresh token
    sur liste noire pour empecher toute nouvelle rotation."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        rafraichissement = request.data.get('refresh')
        if not rafraichissement:
            return Response(
                {'detail': 'Refresh token is required.'}, status=status.HTTP_400_BAD_REQUEST
            )
        try:
            RefreshToken(rafraichissement).blacklist()
        except TokenError:
            return Response({'detail': 'Invalid token.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    tags=['Authentication'],
    summary="Verifier l'adresse email via le lien recu",
    description='uidb64 et jeton sont extraits du lien envoye par email a l\'inscription.',
    responses={200: MessageSerializer, 400: MessageSerializer},
)
class VerifierEmailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, uidb64, token):
        utilisateur = _decoder_uid(uidb64)
        if utilisateur is None or not email_verification_token.check_token(utilisateur, token):
            return Response({'detail': 'Invalid or expired link.'}, status=status.HTTP_400_BAD_REQUEST)
        utilisateur.email_verifie = True
        utilisateur.save(update_fields=['email_verifie'])
        return Response({'detail': 'Email address verified.'})


@extend_schema(
    tags=['Authentication'],
    summary='Demander un email de reinitialisation de mot de passe',
    description=(
        "Reponse identique que le compte existe ou non (200 dans les deux cas) : "
        "evite de reveler l'existence d'une adresse, meme principe que la "
        "connexion en deux temps."
    ),
    request=DemandeReinitialisationSerializer,
    responses={200: MessageSerializer},
)
class DemandeReinitialisationView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'mot-de-passe'

    def post(self, request):
        serializer = DemandeReinitialisationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        utilisateur = Utilisateur.objects.filter(telephone=serializer.validated_data['telephone']).first()
        if utilisateur is not None and utilisateur.email:
            envoyer_email_reinitialisation(utilisateur, default_token_generator)
        # Reponse identique que le compte existe ou non : evite de reveler
        # l'existence d'une adresse (meme principe que la connexion en deux temps).
        return Response({'detail': 'If this account exists, an email has been sent.'})


@extend_schema(
    tags=['Authentication'],
    summary='Confirmer la reinitialisation avec le lien recu',
    description='uid et jeton proviennent du lien envoye par DemandeReinitialisationView.',
    request=ConfirmationReinitialisationSerializer,
    responses={200: MessageSerializer, 400: MessageSerializer},
)
class ConfirmationReinitialisationView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ConfirmationReinitialisationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data

        utilisateur = _decoder_uid(donnees['uid'])
        if utilisateur is None or not default_token_generator.check_token(utilisateur, donnees['token']):
            return Response({'detail': 'Invalid or expired link.'}, status=status.HTTP_400_BAD_REQUEST)

        utilisateur.set_password(donnees['new_password'])
        utilisateur.save(update_fields=['password'])
        return Response({'detail': 'Password reset.'})


@extend_schema_view(
    get=extend_schema(
        tags=['Account'],
        summary='Lire le profil du compte connecte',
        responses={200: UtilisateurSerializer},
    ),
    patch=extend_schema(
        tags=['Account'],
        summary='Mettre a jour partiellement le profil',
        request=UtilisateurMiseAJourSerializer,
        responses={200: UtilisateurSerializer},
    ),
    delete=extend_schema(
        tags=['Account'],
        summary='Demander la suppression du compte',
        description='Suppression logique : grace de 30 jours avant purge par une tache planifiee future.',
        responses={204: None},
    ),
)
class MoiView(APIView):
    """Profil du compte connecte : lecture, mise a jour partielle, suppression
    logique (grace de 30 jours avant purge par une tache planifiee future)."""

    def get(self, request):
        return Response(UtilisateurSerializer(request.user, context={'request': request}).data)

    def patch(self, request):
        serializer = UtilisateurMiseAJourSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UtilisateurSerializer(request.user, context={'request': request}).data)

    def delete(self, request):
        request.user.suppression_demandee_le = timezone.now()
        request.user.save(update_fields=['suppression_demandee_le'])
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    tags=['Account'],
    summary="Televerser l'avatar du compte connecte",
    description='Image jusqu\'a 5 Mo. Remplace tout avatar existant.',
    request=AvatarSerializer,
    responses={200: UtilisateurSerializer},
)
class AvatarView(APIView):
    def post(self, request):
        serializer = AvatarSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.avatar = serializer.validated_data['avatar']
        request.user.save(update_fields=['avatar'])
        return Response(UtilisateurSerializer(request.user, context={'request': request}).data)


@extend_schema_view(
    get=extend_schema(
        tags=['Account'],
        summary='Lire les parametres du compte connecte',
        responses={200: ParametresSerializer},
    ),
    patch=extend_schema(
        tags=['Account'],
        summary='Mettre a jour partiellement les parametres',
        request=ParametresSerializer,
        responses={200: ParametresSerializer},
    ),
)
class ParametresView(APIView):
    def get(self, request):
        return Response(ParametresSerializer(request.user.parametres).data)

    def patch(self, request):
        serializer = ParametresSerializer(request.user.parametres, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


@extend_schema(
    tags=['Account'],
    summary='Statistiques du compte connecte',
    description=(
        "completed_trips/total_distance_km/reported_incidents sont calcules sur les "
        "donnees reelles de l'utilisateur. time_saved_minutes reste a 0 -- aucune notion "
        "de 'temps gagne grace a l'appli' (vs. un trajet non guide) n'est encore definie "
        "nulle part dans le systeme ; mieux vaut un zero honnete qu'un chiffre invente."
    ),
    responses={
        200: inline_serializer(
            name='StatistiquesReponse',
            fields={
                'completed_trips': drf_serializers.IntegerField(),
                'total_distance_km': drf_serializers.FloatField(),
                'reported_incidents': drf_serializers.IntegerField(),
                'time_saved_minutes': drf_serializers.IntegerField(),
            },
        )
    },
)
class StatistiquesView(APIView):
    def get(self, request):
        trajets_termines = Trajet.objects.filter(utilisateur=request.user, statut=StatutTrajet.TERMINE)
        distance_totale_m = trajets_termines.aggregate(
            total=Sum(Coalesce('distance_reelle', 'distance_prevue'))
        )['total'] or 0

        return Response({
            'completed_trips': trajets_termines.count(),
            'total_distance_km': round(distance_totale_m / 1000, 1),
            'reported_incidents': Incident.objects.filter(auteur=request.user).count(),
            # cf. description ci-dessus : pas encore de definition de "temps gagne".
            'time_saved_minutes': 0,
        })


@extend_schema(
    tags=['Account'],
    summary='Enregistrer ou mettre a jour un appareil (notifications push)',
    description=(
        'Upsert sur jeton_push : un meme appareil qui se reenregistre (ex. apres '
        'reinstallation) met a jour sa ligne plutot que d\'en creer une en double.'
    ),
    request=AppareilSerializer,
    responses={201: AppareilSerializer},
)
class AppareilCreationView(APIView):
    """Upsert sur jeton_push : un meme appareil qui se reenregistre (ex. apres
    reinstallation) met a jour sa ligne plutot que d'en creer une en double."""

    def post(self, request):
        serializer = AppareilSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        appareil, _ = Appareil.objects.update_or_create(
            utilisateur=request.user,
            jeton_push=serializer.validated_data['jeton_push'],
            defaults={**serializer.validated_data, 'est_actif': True},
        )
        return Response(AppareilSerializer(appareil).data, status=status.HTTP_201_CREATED)


@extend_schema(
    tags=['Account'],
    summary='Desenregistrer un appareil',
    responses={204: None, 404: OpenApiResponse(description='Device not found for this user.')},
)
class AppareilSuppressionView(APIView):
    def delete(self, request, id):
        appareil = get_object_or_404(Appareil, id=id, utilisateur=request.user)
        appareil.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    tags=['Configuration'],
    summary='Configuration publique de l\'application mobile',
    description=(
        'Lue par le mobile a chaque lancement : permet de faire evoluer '
        'villes/types/version minimale sans publication sur les stores.'
    ),
    responses={
        200: inline_serializer(
            name='ConfigReponse',
            fields={
                'cities': drf_serializers.ListField(child=drf_serializers.CharField()),
                'vehicle_types': drf_serializers.DictField(),
                'incident_types': drf_serializers.DictField(),
                'minimum_app_version': drf_serializers.CharField(),
            },
        )
    },
)
class ConfigView(APIView):
    """Configuration publique lue par le mobile a chaque lancement : permet de
    faire evoluer villes/types/version minimale sans publication sur les stores."""

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({
            'cities': VILLES_DISPONIBLES,
            'vehicle_types': dict(TypeVehicule.choices),
            'incident_types': dict(TypeIncident.choices),
            'minimum_app_version': VERSION_MINIMALE_APP,
        })


@extend_schema(
    tags=['Staff Users'],
    summary='Lister les utilisateurs (moderation)',
    description=(
        "Reserve au staff (is_staff). `search` filtre (icontains) sur telephone/nom/"
        "email ; `banned=true`/`false` filtre sur l'etat de bannissement."
    ),
    parameters=[
        OpenApiParameter('search', OpenApiTypes.STR),
        OpenApiParameter('banned', OpenApiTypes.BOOL),
        OpenApiParameter('status', OpenApiTypes.STR, description='active/banned/suspect/staff.'),
        OpenApiParameter('plan', OpenApiTypes.STR, description='GRATUITE/PREMIUM.'),
        OpenApiParameter('city', OpenApiTypes.STR),
        OpenApiParameter('ordering', OpenApiTypes.STR, description='recent (defaut)/oldest/reputation/points/reports.'),
        OpenApiParameter('page', OpenApiTypes.INT),
        OpenApiParameter('page_size', OpenApiTypes.INT),
    ],
    responses={200: UtilisateurModerationSerializer(many=True)},
)
class UtilisateurModerationListView(APIView):
    """GET /api/staff/users/?search=&status=&plan=&city=&ordering=&page= : liste des comptes."""

    permission_classes = [IsAdminUser]
    pagination_class = StaffPagination

    TRIS = {
        'recent': '-date_joined',
        'oldest': 'date_joined',
        'reputation': '-score_reputation',
        'points': '-points',
        'reports': '-nb_signalements',
    }

    def get(self, request):
        utilisateurs = Utilisateur.objects.annotate(
            nb_signalements=Count('incidents_signales', distinct=True),
            nb_retraits_7j=Count(
                'incidents_signales',
                filter=Q(
                    incidents_signales__statut=StatutIncident.RETIRE,
                    incidents_signales__cree_le__gte=timezone.now() - FENETRE_ABUS,
                ),
                distinct=True,
            ),
            vu_le=Max('appareils__vu_le'),
        )

        recherche = request.query_params.get('search', '').strip()
        if recherche:
            utilisateurs = utilisateurs.filter(
                Q(telephone__icontains=recherche)
                | Q(nom_complet__icontains=recherche)
                | Q(email__icontains=recherche)
            )

        banni = request.query_params.get('banned')
        if banni is not None:
            utilisateurs = utilisateurs.filter(est_banni=banni.lower() == 'true')

        etat = request.query_params.get('status')
        if etat == 'active':
            utilisateurs = utilisateurs.filter(est_banni=False, is_staff=False)
        elif etat == 'banned':
            utilisateurs = utilisateurs.filter(est_banni=True)
        elif etat == 'suspect':
            utilisateurs = utilisateurs.filter(nb_retraits_7j__gte=SEUIL_RETRAITS_ABUS)
        elif etat == 'staff':
            utilisateurs = utilisateurs.filter(is_staff=True)

        if request.query_params.get('plan'):
            utilisateurs = utilisateurs.filter(formule=request.query_params['plan'])
        if request.query_params.get('city', '').strip():
            utilisateurs = utilisateurs.filter(ville__icontains=request.query_params['city'].strip())

        tri = self.TRIS.get(request.query_params.get('ordering', ''), '-date_joined')
        utilisateurs = utilisateurs.order_by(tri, '-date_joined')

        paginateur = self.pagination_class()
        page = paginateur.paginate_queryset(utilisateurs, request)
        return paginateur.get_paginated_response(UtilisateurModerationSerializer(page, many=True).data)


@extend_schema(
    tags=['Staff Users'],
    summary='Bannir un utilisateur',
    description=(
        "Reserve au staff (is_staff). cf. Utilisateur.bannir(). `until` absent/null "
        "= ban permanent. Refuse avec 400 si la cible est elle-meme staff."
    ),
    request=BanUtilisateurSerializer,
    responses={200: UtilisateurModerationSerializer, 400: MessageSerializer},
)
class UtilisateurBanView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, id):
        serializer = BanUtilisateurSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        utilisateur = get_object_or_404(Utilisateur, id=id)
        if utilisateur.is_staff:
            return Response({'detail': 'Cannot ban a staff account.'}, status=400)

        utilisateur.bannir(jusqu_a=serializer.validated_data['until'])
        jusqu_a = serializer.validated_data['until']
        journaliser(
            request.user, 'users.ban', utilisateur, libelle=_libelle_utilisateur(utilisateur),
            apres={'jusqu_a': jusqu_a.isoformat() if jusqu_a else None, 'motif': serializer.validated_data['reason']},
        )
        return Response(UtilisateurModerationSerializer(utilisateur).data)


@extend_schema(
    tags=['Staff Users'],
    summary='Debannir un utilisateur',
    description='Reserve au staff (is_staff). cf. Utilisateur.debannir().',
    responses={200: UtilisateurModerationSerializer},
)
class UtilisateurUnbanView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, id):
        utilisateur = get_object_or_404(Utilisateur, id=id)
        utilisateur.debannir()
        journaliser(request.user, 'users.unban', utilisateur, libelle=_libelle_utilisateur(utilisateur))
        return Response(UtilisateurModerationSerializer(utilisateur).data)


def _libelle_utilisateur(utilisateur):
    return utilisateur.nom_complet or utilisateur.telephone or str(utilisateur.id)


# Nombre d'elements recents affiches dans chaque section de la fiche.
NB_RECENTS_FICHE = 5


@extend_schema_view(
    get=extend_schema(
        tags=['Staff Users'],
        summary="Fiche d'un utilisateur (moderation)",
        description=(
            'Reserve au staff (is_staff). Profil + statistiques (trajets, signalements, '
            'lieux proposes, votes), elements recents, appareils et historique de moderation '
            "(entrees du journal d'audit visant ce compte)."
        ),
        responses={200: UtilisateurModerationSerializer},
    ),
    patch=extend_schema(
        tags=['Staff Users'],
        summary="Changer la formule d'un utilisateur",
        request=FormuleUtilisateurSerializer,
        responses={200: UtilisateurModerationSerializer},
    ),
)
class UtilisateurFicheView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, id):
        utilisateur = get_object_or_404(Utilisateur, id=id)
        maintenant = timezone.now()

        incidents = Incident.objects.filter(auteur=utilisateur)
        par_statut_incident = dict(incidents.values('statut').annotate(n=Count('pk')).values_list('statut', 'n'))
        trajets = Trajet.objects.filter(utilisateur=utilisateur)
        par_statut_trajet = dict(trajets.values('statut').annotate(n=Count('pk')).values_list('statut', 'n'))
        lieux = dict(
            Lieu.objects.filter(propose_par=utilisateur).values('statut').annotate(n=Count('pk')).values_list('statut', 'n')
        )
        distance_m = trajets.filter(statut=StatutTrajet.TERMINE).aggregate(
            total=Coalesce(Sum('distance_reelle'), Sum('distance_prevue'), 0)
        )['total']

        donnees = UtilisateurModerationSerializer(utilisateur).data
        donnees['stats'] = {
            'trips_total': trajets.count(),
            'trips_completed': par_statut_trajet.get(StatutTrajet.TERMINE, 0),
            'distance_km': round((distance_m or 0) / 1000, 1),
            'reports_total': sum(par_statut_incident.values()),
            'reports_active': par_statut_incident.get(StatutIncident.ACTIF, 0) + par_statut_incident.get(StatutIncident.EN_ATTENTE, 0),
            'reports_removed': par_statut_incident.get(StatutIncident.RETIRE, 0),
            'reports_removed_7d': incidents.filter(
                statut=StatutIncident.RETIRE, cree_le__gte=maintenant - FENETRE_ABUS
            ).count(),
            'places_approved': lieux.get(StatutLieu.APPROUVE, 0),
            'places_rejected': lieux.get(StatutLieu.REJETE, 0),
            'places_pending': lieux.get(StatutLieu.EN_ATTENTE, 0),
            'votes': Vote.objects.filter(votant=utilisateur).count(),
        }
        donnees['recent_reports'] = [
            {
                'id': str(i.id), 'type': i.type, 'type_label': i.get_type_display(), 'street_name': i.nom_voie,
                'city': i.ville, 'status': i.statut, 'confirmations': i.confirmations, 'disputes': i.infirmations,
                'reason': i.motif_retrait, 'created_at': i.cree_le,
            }
            for i in incidents.order_by('-cree_le')[:NB_RECENTS_FICHE]
        ]
        donnees['recent_trips'] = [
            {
                'id': str(t.id), 'origin': t.libelle_origine, 'destination': t.libelle_destination,
                'status': t.statut, 'distance_m': t.distance_reelle or t.distance_prevue,
                'started_at': t.demarre_le, 'rating': t.note,
            }
            for t in trajets.order_by(F('demarre_le').desc(nulls_last=True))[:NB_RECENTS_FICHE]
        ]
        donnees['devices'] = [
            {
                'id': str(a.id), 'platform': a.plateforme, 'app_version': a.version_application,
                'os_version': a.version_systeme, 'last_seen': a.vu_le, 'active': a.est_actif,
            }
            for a in utilisateur.appareils.order_by('-vu_le')
        ]
        donnees['account'] = {
            'vehicle_type': utilisateur.type_vehicule,
            'email_verified': utilisateur.email_verifie,
            'google_linked': bool(utilisateur.identifiant_google),
            'invisible_mode': utilisateur.mode_invisible,
            'terms_accepted_at': utilisateur.cgu_acceptee_le,
            'deletion_requested_at': utilisateur.suppression_demandee_le,
            'is_active': utilisateur.is_active,
            'last_login': utilisateur.last_login,
        }
        donnees['saved_addresses'] = [
            {
                'label': a.get_libelle_display(), 'name': a.nom_personnalise, 'address': a.adresse,
                'lat': a.position.y, 'lon': a.position.x,
            }
            for a in utilisateur.adresses_enregistrees.all()
        ]
        parametres = Parametres.objects.filter(utilisateur=utilisateur).first()
        donnees['settings'] = (
            {
                'avoid_tolls': parametres.eviter_peages,
                'avoid_unpaved': parametres.eviter_non_bitumees,
                'voice_guidance': parametres.guidage_vocal_actif,
                'voice_language': parametres.langue_vocale,
                'units': parametres.unites,
                'speed_alert': parametres.alerte_vitesse_active,
                'speed_tolerance_kmh': parametres.tolerance_vitesse_kmh,
                'notifications': parametres.notifications_globales,
                'police_alerts': parametres.notif_alertes_police,
                'loading_radius_km': parametres.rayon_chargement_km,
            }
            if parametres
            else None
        )
        donnees['history'] = [
            {
                'action': e.action, 'at': e.survenue_le, 'actor': e.acteur.nom_complet if e.acteur_id else None,
                'details': e.valeur_nouvelle,
            }
            for e in EntreeAudit.objects.filter(type_cible='utilisateur', identifiant_cible=utilisateur.id)
            .select_related('acteur')
            .order_by('-survenue_le')[:20]
        ]
        return Response(donnees)

    def patch(self, request, id):
        serializer = FormuleUtilisateurSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        utilisateur = get_object_or_404(Utilisateur, id=id)

        avant = {'formule': utilisateur.formule}
        utilisateur.formule = serializer.validated_data['plan']
        utilisateur.formule_expire_le = serializer.validated_data['plan_expires_at']
        utilisateur.save(update_fields=['formule', 'formule_expire_le'])
        expire = utilisateur.formule_expire_le
        journaliser(
            request.user, 'users.plan', utilisateur, libelle=_libelle_utilisateur(utilisateur), avant=avant,
            apres={'formule': utilisateur.formule, 'expire_le': expire.isoformat() if expire else None},
        )
        return Response(UtilisateurModerationSerializer(utilisateur).data)


@extend_schema(
    tags=['Staff Users'],
    summary="Remettre a zero les points d'un utilisateur",
    description=(
        'Reserve au staff (is_staff). A utiliser apres conversion des points en bons '
        "carburant : le solde precedent est garde dans le journal d'audit."
    ),
    request=RemiseAZeroPointsSerializer,
    responses={200: UtilisateurModerationSerializer},
)
class UtilisateurRemiseAZeroPointsView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, id):
        serializer = RemiseAZeroPointsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        utilisateur = get_object_or_404(Utilisateur, id=id)

        precedent = float(utilisateur.points)
        utilisateur.points = 0
        utilisateur.save(update_fields=['points'])
        journaliser(
            request.user, 'users.points_reset', utilisateur, libelle=_libelle_utilisateur(utilisateur),
            avant={'points': precedent}, apres={'points': 0, 'motif': serializer.validated_data['reason']},
        )
        return Response(UtilisateurModerationSerializer(utilisateur).data)


@extend_schema(
    tags=['Waitlist'],
    summary="S'inscrire sur la liste d'attente",
    description=(
        "Public, sans compte. Le telephone est normalise (E.164) et sert de cle "
        "d'unicite. Reponse neutre, identique que le numero soit nouveau ou deja "
        "inscrit (201, sans aucune donnee) : renvoyer l'inscription existante "
        "permettait a n'importe qui de retrouver nom, email et ville a partir "
        "d'un numero (audit securite du 07/10). Un numero deja inscrit n'est pas modifie."
    ),
    request=ListeAttenteSerializer,
    responses={201: MessageSerializer},
)
class ListeAttenteView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'liste-attente'

    def post(self, request):
        serializer = ListeAttenteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if not InscriptionListeAttente.objects.filter(telephone=serializer.validated_data['telephone']).exists():
            serializer.save()
        return Response({'detail': 'Registration received.'}, status=status.HTTP_201_CREATED)


@extend_schema(
    tags=['Staff Waitlist'],
    summary="Lister la liste d'attente",
    description=(
        "Reserve au staff (is_staff). `search` filtre (icontains) sur telephone/nom/"
        "email/ville ; `profile` filtre sur le profil ; `contacted=true`/`false` sur "
        "l'etat de suivi. Plus recentes d'abord."
    ),
    parameters=[
        OpenApiParameter('search', OpenApiTypes.STR),
        OpenApiParameter('profile', OpenApiTypes.STR, enum=ProfilListeAttente.values),
        OpenApiParameter('contacted', OpenApiTypes.BOOL),
        OpenApiParameter('page', OpenApiTypes.INT),
        OpenApiParameter('page_size', OpenApiTypes.INT),
    ],
    responses={200: ListeAttenteModerationSerializer(many=True)},
)
class ListeAttenteModerationListView(APIView):
    permission_classes = [IsAdminUser]
    pagination_class = StaffPagination

    def get(self, request):
        inscriptions = InscriptionListeAttente.objects.all()

        recherche = request.query_params.get('search', '').strip()
        if recherche:
            inscriptions = inscriptions.filter(
                Q(telephone__icontains=recherche)
                | Q(nom_complet__icontains=recherche)
                | Q(email__icontains=recherche)
                | Q(ville__icontains=recherche)
            )

        profil = request.query_params.get('profile')
        if profil:
            inscriptions = inscriptions.filter(profil=profil)

        contacte = request.query_params.get('contacted')
        if contacte is not None:
            inscriptions = inscriptions.filter(contacte=contacte.lower() == 'true')

        paginateur = self.pagination_class()
        page = paginateur.paginate_queryset(inscriptions.order_by('-cree_le'), request)
        return paginateur.get_paginated_response(ListeAttenteModerationSerializer(page, many=True).data)


@extend_schema(
    tags=['Staff Waitlist'],
    summary="Marquer une inscription comme recontactee",
    description='Reserve au staff (is_staff).',
    request=ListeAttenteSuiviSerializer,
    responses={200: ListeAttenteModerationSerializer},
)
class ListeAttenteSuiviView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, id):
        serializer = ListeAttenteSuiviSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        inscription = get_object_or_404(InscriptionListeAttente, id=id)
        inscription.contacte = serializer.validated_data['contacte']
        inscription.save(update_fields=['contacte'])
        journaliser(
            request.user, 'waitlist.contacted' if inscription.contacte else 'waitlist.uncontacted',
            inscription, libelle=inscription.nom_complet,
        )
        return Response(ListeAttenteModerationSerializer(inscription).data)
