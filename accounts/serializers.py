from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenRefreshSerializer

from .google_oauth import JetonGoogleInvalide, verifier_jeton_google
from .models import Appareil, Droits, Parametres, Utilisateur


class DroitsSerializer(serializers.ModelSerializer):
    class Meta:
        model = Droits
        fields = [
            'formule', 'max_adresses_enregistrees', 'publicite_active',
            'routage_avance', 'packs_hors_ligne', 'retention_historique_jours',
        ]
        read_only_fields = fields


class UtilisateurSerializer(serializers.ModelSerializer):
    url_avatar = serializers.SerializerMethodField()
    droits = DroitsSerializer(read_only=True)

    class Meta:
        model = Utilisateur
        fields = [
            'id', 'email', 'nom_complet', 'telephone', 'ville', 'type_vehicule',
            'url_avatar', 'email_verifie', 'formule', 'score_reputation', 'points',
            'mode_invisible', 'droits',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_url_avatar(self, utilisateur):
        if utilisateur.avatar:
            url = utilisateur.avatar.url
            request = self.context.get('request')
            return request.build_absolute_uri(url) if request else url
        return utilisateur.url_avatar


class UtilisateurMiseAJourSerializer(serializers.ModelSerializer):
    class Meta:
        model = Utilisateur
        fields = ['nom_complet', 'ville', 'type_vehicule', 'mode_invisible']
        extra_kwargs = {field: {'required': False} for field in fields}


class AvatarSerializer(serializers.Serializer):
    avatar = serializers.ImageField(write_only=True)

    def validate_avatar(self, fichier):
        limite = 5 * 1024 * 1024
        if fichier.size > limite:
            raise serializers.ValidationError("L'image ne doit pas depasser 5 Mo.")
        return fichier


class ParametresSerializer(serializers.ModelSerializer):
    class Meta:
        model = Parametres
        exclude = ['id', 'utilisateur']


class AppareilSerializer(serializers.ModelSerializer):
    class Meta:
        model = Appareil
        fields = [
            'id', 'jeton_push', 'plateforme', 'version_application',
            'version_systeme', 'langue',
        ]
        read_only_fields = ['id']


class JetonsSerializer(serializers.Serializer):
    """Documentation only: forme des jetons JWT retournes par les vues d'authentification."""

    acces = serializers.CharField(help_text='Jeton JWT court (15 min) a joindre en Authorization: Bearer <acces>.')
    rafraichissement = serializers.CharField(help_text='Jeton JWT long (30 jours) utilise sur /auth/rafraichir/.')


class RafraichirSerializer(TokenRefreshSerializer):
    """SimpleJWT nomme ses champs access/refresh (anglais) par defaut -- seule
    incoherence face au reste du module, qui utilise acces/rafraichissement
    partout ailleurs (cf. JetonsSerializer). Repere par l'integration
    frontend : deux conventions dans le meme module produisent des bugs.

    Renomme la requete et la reponse sans toucher a la logique de rotation/
    blacklist heritee de TokenRefreshSerializer.validate() : le renommage du
    champ d'entree passe par `source` (le wire-name change, la cle lue par
    validate() reste 'refresh') ; la reponse, elle-meme une simple recopie du
    dict retourne par validate() (TokenViewBase.post ne passe jamais par
    to_representation()), est reconstruite explicitement ici."""

    def get_fields(self):
        fields = super().get_fields()

        champ_entree = fields.pop('refresh')
        champ_entree.source = 'refresh'
        fields['rafraichissement'] = champ_entree

        champ_sortie = fields.pop('access')
        champ_sortie.source = 'access'
        fields['acces'] = champ_sortie

        return fields

    def validate(self, attrs):
        resultat = super().validate(attrs)
        reponse = {'acces': resultat['access']}
        if 'refresh' in resultat:  # present seulement si ROTATE_REFRESH_TOKENS (cf. SIMPLE_JWT)
            reponse['rafraichissement'] = resultat['refresh']
        return reponse


class MessageSerializer(serializers.Serializer):
    """Documentation only: reponse generique {"detail": "..."} utilisee par plusieurs vues."""

    detail = serializers.CharField()


class ExisteSerializer(serializers.Serializer):
    """Documentation only: reponse de VerifierExistenceView."""

    existe = serializers.BooleanField()


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
