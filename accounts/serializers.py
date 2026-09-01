from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .google_oauth import JetonGoogleInvalide, verifier_jeton_google
from .models import Appareil, Droits, Formule, Parametres, Plateforme, TypeVehicule, Unite, Utilisateur

# Les champs ci-dessous sont delibrement declares avec `source=` plutot que
# de laisser ModelSerializer les auto-generer : la reponse API doit parler
# anglais (cf. discussion), mais les modeles/colonnes DB restent en francais
# (aucune migration ni renommage interne) -- source= est le seul point de
# traduction entre les deux.


class DroitsSerializer(serializers.ModelSerializer):
    plan = serializers.CharField(source='formule', read_only=True)
    max_saved_addresses = serializers.IntegerField(source='max_adresses_enregistrees', read_only=True)
    ads_enabled = serializers.BooleanField(source='publicite_active', read_only=True)
    advanced_routing = serializers.BooleanField(source='routage_avance', read_only=True)
    offline_packs = serializers.BooleanField(source='packs_hors_ligne', read_only=True)
    history_retention_days = serializers.IntegerField(source='retention_historique_jours', read_only=True)

    class Meta:
        model = Droits
        fields = [
            'plan', 'max_saved_addresses', 'ads_enabled',
            'advanced_routing', 'offline_packs', 'history_retention_days',
        ]
        read_only_fields = fields


class UtilisateurSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source='nom_complet', read_only=True)
    phone = serializers.CharField(source='telephone', read_only=True)
    city = serializers.CharField(source='ville', read_only=True)
    vehicle_type = serializers.ChoiceField(choices=TypeVehicule.choices, source='type_vehicule', read_only=True)
    avatar_url = serializers.SerializerMethodField()
    email_verified = serializers.BooleanField(source='email_verifie', read_only=True)
    plan = serializers.ChoiceField(choices=Formule.choices, source='formule', read_only=True)
    reputation_score = serializers.DecimalField(
        source='score_reputation', max_digits=6, decimal_places=1, read_only=True
    )
    invisible_mode = serializers.BooleanField(source='mode_invisible', read_only=True)
    plan_limits = DroitsSerializer(source='droits', read_only=True)

    class Meta:
        model = Utilisateur
        fields = [
            'id', 'email', 'full_name', 'phone', 'city', 'vehicle_type',
            'avatar_url', 'email_verified', 'plan', 'reputation_score', 'points',
            'invisible_mode', 'plan_limits',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_avatar_url(self, utilisateur):
        if utilisateur.avatar:
            url = utilisateur.avatar.url
            request = self.context.get('request')
            return request.build_absolute_uri(url) if request else url
        return utilisateur.url_avatar


class UtilisateurMiseAJourSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source='nom_complet', required=False)
    city = serializers.CharField(source='ville', required=False, allow_null=True)
    vehicle_type = serializers.ChoiceField(
        choices=TypeVehicule.choices, source='type_vehicule', required=False, allow_null=True
    )
    invisible_mode = serializers.BooleanField(source='mode_invisible', required=False)

    class Meta:
        model = Utilisateur
        fields = ['full_name', 'city', 'vehicle_type', 'invisible_mode']


class AvatarSerializer(serializers.Serializer):
    avatar = serializers.ImageField(write_only=True)

    def validate_avatar(self, fichier):
        limite = 5 * 1024 * 1024
        if fichier.size > limite:
            raise serializers.ValidationError('Image must not exceed 5 MB.')
        return fichier


class ParametresSerializer(serializers.ModelSerializer):
    avoid_tolls = serializers.BooleanField(source='eviter_peages', required=False)
    avoid_unpaved_roads = serializers.BooleanField(source='eviter_non_bitumees', required=False)
    avoid_difficult_intersections = serializers.BooleanField(
        source='eviter_intersections_difficiles', required=False
    )
    map_style = serializers.CharField(source='style_carte', required=False)
    voice_guidance_enabled = serializers.BooleanField(source='guidage_vocal_actif', required=False)
    voice_language = serializers.CharField(source='langue_vocale', required=False)
    units = serializers.ChoiceField(choices=Unite.choices, source='unites', required=False)
    speedometer_enabled = serializers.BooleanField(source='compteur_vitesse_actif', required=False)
    speed_alert_enabled = serializers.BooleanField(source='alerte_vitesse_active', required=False)
    notifications_enabled = serializers.BooleanField(source='notifications_globales', required=False)
    notify_announcements = serializers.BooleanField(source='notif_annonces', required=False)
    notify_frequent_incidents = serializers.BooleanField(source='notif_incidents_frequents', required=False)
    notify_police_alerts = serializers.BooleanField(source='notif_alertes_police', required=False)
    notify_route_change = serializers.BooleanField(source='notif_changement_itineraire', required=False)
    notify_news = serializers.BooleanField(source='notif_nouveautes', required=False)

    class Meta:
        model = Parametres
        fields = [
            'avoid_tolls', 'avoid_unpaved_roads', 'avoid_difficult_intersections', 'map_style',
            'voice_guidance_enabled', 'voice_language', 'units', 'speedometer_enabled', 'speed_alert_enabled',
            'notifications_enabled', 'notify_announcements', 'notify_frequent_incidents',
            'notify_police_alerts', 'notify_route_change', 'notify_news',
        ]


class AppareilSerializer(serializers.ModelSerializer):
    push_token = serializers.CharField(source='jeton_push')
    platform = serializers.ChoiceField(choices=Plateforme.choices, source='plateforme')
    app_version = serializers.CharField(source='version_application')
    os_version = serializers.CharField(source='version_systeme')
    language = serializers.CharField(source='langue', required=False)

    class Meta:
        model = Appareil
        fields = ['id', 'push_token', 'platform', 'app_version', 'os_version', 'language']
        read_only_fields = ['id']


class JetonsSerializer(serializers.Serializer):
    """Documentation only: shape of the JWT tokens returned by auth endpoints."""

    access = serializers.CharField(help_text='Short-lived JWT (15 min) to send as Authorization: Bearer <access>.')
    refresh = serializers.CharField(help_text='Long-lived JWT (30 days) used on /auth/refresh/.')


class MessageSerializer(serializers.Serializer):
    """Documentation only: generic {"detail": "..."} response used by several views."""

    detail = serializers.CharField()


class ExisteSerializer(serializers.Serializer):
    """Documentation only: response of VerifierExistenceView."""

    exists = serializers.BooleanField()


class VerifierExistenceSerializer(serializers.Serializer):
    phone = serializers.CharField(source='telephone')


class InscriptionSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source='nom_complet')
    password = serializers.CharField(write_only=True)
    password_confirmation = serializers.CharField(write_only=True)
    phone = serializers.CharField(source='telephone', required=True)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)
    city = serializers.CharField(source='ville', required=False)
    vehicle_type = serializers.ChoiceField(choices=TypeVehicule.choices, source='type_vehicule', required=False)
    accepts_terms = serializers.BooleanField(write_only=True)

    class Meta:
        model = Utilisateur
        fields = [
            'phone', 'email', 'full_name', 'password', 'password_confirmation',
            'city', 'vehicle_type', 'accepts_terms',
        ]

    def validate_phone(self, phone):
        if Utilisateur.objects.filter(telephone=phone).exists():
            raise serializers.ValidationError('Phone number already in use.')
        return phone

    def validate_email(self, email):
        if not email:
            return None
        email = Utilisateur.objects.normalize_email(email)
        if Utilisateur.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError('Email already in use.')
        return email

    def validate(self, attrs):
        if attrs['password'] != attrs.pop('password_confirmation'):
            raise serializers.ValidationError(
                {'password_confirmation': 'Passwords do not match.'}
            )
        if not attrs.get('accepts_terms'):
            raise serializers.ValidationError(
                {'accepts_terms': 'You must accept the terms and conditions.'}
            )
        try:
            validate_password(attrs['password'])
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})
        return attrs

    def create(self, validated_data):
        validated_data.pop('accepts_terms')
        mot_de_passe = validated_data.pop('password')
        utilisateur = Utilisateur.objects.create_user(
            telephone=validated_data.pop('telephone'),
            password=mot_de_passe,
            cgu_acceptee_le=timezone.now(),
            **validated_data,
        )
        Parametres.objects.create(utilisateur=utilisateur)
        return utilisateur


class ConnexionSerializer(serializers.Serializer):
    phone = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        utilisateur = authenticate(
            request=self.context.get('request'),
            username=attrs['phone'],
            password=attrs['password'],
        )
        if utilisateur is None:
            raise serializers.ValidationError('Incorrect credentials.')
        if utilisateur.est_banni:
            raise serializers.ValidationError('This account is banned.')
        attrs['utilisateur'] = utilisateur
        return attrs


class ConnexionGoogleSerializer(serializers.Serializer):
    id_token = serializers.CharField(write_only=True)

    def validate(self, attrs):
        try:
            attrs['identite'] = verifier_jeton_google(attrs['id_token'])
        except JetonGoogleInvalide as exc:
            raise serializers.ValidationError(str(exc))
        return attrs


class DemandeReinitialisationSerializer(serializers.Serializer):
    phone = serializers.CharField(source='telephone')


class ConfirmationReinitialisationSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_new_password(self, valeur):
        try:
            validate_password(valeur)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return valeur
