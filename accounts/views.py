from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from .emails import envoyer_email_reinitialisation, envoyer_email_verification
from .models import Droits, Parametres, Utilisateur
from .serializers import (
    ConfirmationReinitialisationSerializer,
    ConnexionGoogleSerializer,
    ConnexionSerializer,
    DemandeReinitialisationSerializer,
    InscriptionSerializer,
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
    """UC-01 : cree le compte, ses Parametres/Droits par defaut, envoie le lien
    de verification et retourne directement les jetons (acces complet des la
    creation, cf. postconditions de UC-01)."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = InscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        utilisateur = serializer.save()
        envoyer_email_verification(utilisateur)
        return Response(
            {'utilisateur': UtilisateurSerializer(utilisateur).data, 'jetons': _jetons_pour(utilisateur)},
            status=status.HTTP_201_CREATED,
        )


class ConnexionView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ConnexionSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        utilisateur = serializer.validated_data['utilisateur']
        return Response(
            {'utilisateur': UtilisateurSerializer(utilisateur).data, 'jetons': _jetons_pour(utilisateur)}
        )


class ConnexionGoogleView(APIView):
    """Section 4.1 'Connexion avec Google' : lie un compte existant si l'adresse
    email verifiee est deja connue, sinon en cree un nouveau."""

    permission_classes = [AllowAny]

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
            Droits.objects.create(utilisateur=utilisateur)

        if utilisateur.est_banni:
            return Response({'detail': 'Ce compte est banni.'}, status=status.HTTP_403_FORBIDDEN)

        return Response(
            {'utilisateur': UtilisateurSerializer(utilisateur).data, 'jetons': _jetons_pour(utilisateur)}
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
