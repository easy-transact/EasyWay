from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from community.models import TypeIncident

from .config_data import VERSION_MINIMALE_APP, VILLES_DISPONIBLES
from .emails import envoyer_email_reinitialisation, envoyer_email_verification
from .models import Appareil, Parametres, TypeVehicule, Utilisateur
from .serializers import (
    AppareilSerializer,
    AvatarSerializer,
    ConfirmationReinitialisationSerializer,
    ConnexionGoogleSerializer,
    ConnexionSerializer,
    DemandeReinitialisationSerializer,
    InscriptionSerializer,
    ParametresSerializer,
    UtilisateurMiseAJourSerializer,
    UtilisateurSerializer,
    VerifierExistenceSerializer,
)
from .tokens import email_verification_token


def _jetons_pour(utilisateur):
    rafraichissement = RefreshToken.for_user(utilisateur)
    return {'acces': str(rafraichissement.access_token), 'rafraichissement': str(rafraichissement)}


def _decoder_uid(uidb64):
    """Retourne l'utilisateur cible d'un lien email, ou None si le lien est mal forme."""
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        return Utilisateur.objects.get(pk=uid)
    except (Utilisateur.DoesNotExist, ValueError, TypeError, DjangoValidationError):
        return None


class VerifierExistenceView(APIView):
    """Premier temps de la connexion en deux temps (section 4.1)."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VerifierExistenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        existe = Utilisateur.objects.filter(email__iexact=serializer.validated_data['email']).exists()
        return Response({'existe': existe})


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
        envoyer_email_verification(utilisateur)
        return Response(
            {
                'utilisateur': UtilisateurSerializer(utilisateur, context={'request': request}).data,
                'jetons': _jetons_pour(utilisateur),
            },
            status=status.HTTP_201_CREATED,
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
                'utilisateur': UtilisateurSerializer(utilisateur, context={'request': request}).data,
                'jetons': _jetons_pour(utilisateur),
            }
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
            return Response({'detail': 'Ce compte est banni.'}, status=status.HTTP_403_FORBIDDEN)

        return Response(
            {
                'utilisateur': UtilisateurSerializer(utilisateur, context={'request': request}).data,
                'jetons': _jetons_pour(utilisateur),
            }
        )


class DeconnexionView(APIView):
    """ServiceAuthentification.revoquerFamille(jeton) : place le refresh token
    sur liste noire pour empecher toute nouvelle rotation."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        rafraichissement = request.data.get('rafraichissement')
        if not rafraichissement:
            return Response(
                {'detail': 'Le jeton de rafraichissement est requis.'}, status=status.HTTP_400_BAD_REQUEST
            )
        try:
            RefreshToken(rafraichissement).blacklist()
        except TokenError:
            return Response({'detail': 'Jeton invalide.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


class VerifierEmailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, uidb64, jeton):
        utilisateur = _decoder_uid(uidb64)
        if utilisateur is None or not email_verification_token.check_token(utilisateur, jeton):
            return Response({'detail': 'Lien invalide ou expire.'}, status=status.HTTP_400_BAD_REQUEST)
        utilisateur.email_verifie = True
        utilisateur.save(update_fields=['email_verifie'])
        return Response({'detail': 'Adresse email verifiee.'})


class DemandeReinitialisationView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'mot-de-passe'

    def post(self, request):
        serializer = DemandeReinitialisationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        utilisateur = Utilisateur.objects.filter(email__iexact=serializer.validated_data['email']).first()
        if utilisateur is not None:
            envoyer_email_reinitialisation(utilisateur, default_token_generator)
        # Reponse identique que le compte existe ou non : evite de reveler
        # l'existence d'une adresse (meme principe que la connexion en deux temps).
        return Response({'detail': "Si ce compte existe, un email a ete envoye."})


class ConfirmationReinitialisationView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ConfirmationReinitialisationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data

        utilisateur = _decoder_uid(donnees['uid'])
        if utilisateur is None or not default_token_generator.check_token(utilisateur, donnees['jeton']):
            return Response({'detail': 'Lien invalide ou expire.'}, status=status.HTTP_400_BAD_REQUEST)

        utilisateur.set_password(donnees['nouveau_mot_de_passe'])
        utilisateur.save(update_fields=['password'])
        return Response({'detail': 'Mot de passe reinitialise.'})


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


class AvatarView(APIView):
    def post(self, request):
        serializer = AvatarSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        request.user.avatar = serializer.validated_data['avatar']
        request.user.save(update_fields=['avatar'])
        return Response(UtilisateurSerializer(request.user, context={'request': request}).data)


class ParametresView(APIView):
    def get(self, request):
        return Response(ParametresSerializer(request.user.parametres).data)

    def patch(self, request):
        serializer = ParametresSerializer(request.user.parametres, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class StatistiquesView(APIView):
    """Stub : le module trajets n'est pas encore branche, retourne des zeros
    pour que l'ecran Statistiques du mobile ait deja une forme stable a consommer."""

    def get(self, request):
        return Response({
            'trajets_completes': 0,
            'distance_totale_km': 0,
            'incidents_signales': 0,
            'temps_gagne_minutes': 0,
        })


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


class AppareilSuppressionView(APIView):
    def delete(self, request, id):
        appareil = get_object_or_404(Appareil, id=id, utilisateur=request.user)
        appareil.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ConfigView(APIView):
    """Configuration publique lue par le mobile a chaque lancement : permet de
    faire evoluer villes/types/version minimale sans publication sur les stores."""

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({
            'villes': VILLES_DISPONIBLES,
            'types_vehicule': dict(TypeVehicule.choices),
            'types_incident': dict(TypeIncident.choices),
            'version_minimale_app': VERSION_MINIMALE_APP,
        })
