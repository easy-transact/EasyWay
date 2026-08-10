from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import serializers

from .google_oauth import JetonGoogleInvalide, verifier_jeton_google
from .models import Droits, Parametres, Utilisateur


class UtilisateurSerializer(serializers.ModelSerializer):
    class Meta:
        model = Utilisateur
        fields = [
            'id', 'email', 'nom_complet', 'telephone', 'ville', 'type_vehicule',
            'url_avatar', 'email_verifie', 'formule', 'score_reputation', 'points',
            'mode_invisible',
        ]
        read_only_fields = fields


class VerifierExistenceSerializer(serializers.Serializer):
    email = serializers.EmailField()


class InscriptionSerializer(serializers.ModelSerializer):
    mot_de_passe = serializers.CharField(write_only=True)
    confirmation_mot_de_passe = serializers.CharField(write_only=True)
    accepte_cgu = serializers.BooleanField(write_only=True)

    class Meta:
        model = Utilisateur
        fields = [
            'email', 'nom_complet', 'mot_de_passe', 'confirmation_mot_de_passe',
            'telephone', 'ville', 'type_vehicule', 'accepte_cgu',
        ]
        extra_kwargs = {
            'telephone': {'required': False},
            'ville': {'required': False},
            'type_vehicule': {'required': False},
        }

    def validate_email(self, email):
        email = Utilisateur.objects.normalize_email(email)
        if Utilisateur.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError('Adresse deja utilisee.')
        return email

    def validate(self, attrs):
        if attrs['mot_de_passe'] != attrs.pop('confirmation_mot_de_passe'):
            raise serializers.ValidationError(
                {'confirmation_mot_de_passe': 'Les mots de passe ne correspondent pas.'}
            )
        if not attrs.get('accepte_cgu'):
            raise serializers.ValidationError(
                {'accepte_cgu': "L'acceptation des CGU est obligatoire."}
            )
        try:
            validate_password(attrs['mot_de_passe'])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'mot_de_passe': list(exc.messages)})
        return attrs

    def create(self, validated_data):
        validated_data.pop('accepte_cgu')
        mot_de_passe = validated_data.pop('mot_de_passe')
        utilisateur = Utilisateur.objects.create_user(
            password=mot_de_passe,
            cgu_acceptee_le=timezone.now(),
            **validated_data,
        )
        Parametres.objects.create(utilisateur=utilisateur)
        Droits.objects.create(utilisateur=utilisateur)
        return utilisateur


class ConnexionSerializer(serializers.Serializer):
    email = serializers.EmailField()
    mot_de_passe = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        utilisateur = authenticate(
            request=self.context.get('request'),
            username=attrs['email'],
            password=attrs['mot_de_passe'],
        )
        if utilisateur is None:
            raise serializers.ValidationError('Identifiants incorrects.')
        if utilisateur.est_banni:
            raise serializers.ValidationError('Ce compte est banni.')
        attrs['utilisateur'] = utilisateur
        return attrs


class ConnexionGoogleSerializer(serializers.Serializer):
    jeton_identite = serializers.CharField(write_only=True)

    def validate(self, attrs):
        try:
            attrs['identite'] = verifier_jeton_google(attrs['jeton_identite'])
        except JetonGoogleInvalide as exc:
            raise serializers.ValidationError(str(exc))
        return attrs


class DemandeReinitialisationSerializer(serializers.Serializer):
    email = serializers.EmailField()


class ConfirmationReinitialisationSerializer(serializers.Serializer):
    uid = serializers.CharField()
    jeton = serializers.CharField()
    nouveau_mot_de_passe = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_nouveau_mot_de_passe(self, valeur):
        try:
            validate_password(valeur)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return valeur
