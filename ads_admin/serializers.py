from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import (
    CampagnePublicitaire,
    Emplacement,
    EntreeAudit,
    NotificationStaff,
    PreferencesNotificationStaff,
    TypeNotification,
)

# Valeur de Impression.evenement qui compte comme un clic ; tout autre
# evenement compte comme une impression (cf. CampagneSerializer).
EVENEMENT_CLIC = 'CLIC'


class NotificationStaffSerializer(serializers.ModelSerializer):
    level = serializers.CharField(source='niveau', read_only=True)
    title = serializers.CharField(source='titre', read_only=True)
    text = serializers.CharField(source='texte', read_only=True)
    link = serializers.CharField(source='lien', read_only=True)
    count = serializers.IntegerField(source='compteur', read_only=True)
    created_at = serializers.DateTimeField(source='cree_le', read_only=True)
    updated_at = serializers.DateTimeField(source='mis_a_jour_le', read_only=True)
    read = serializers.SerializerMethodField()

    class Meta:
        model = NotificationStaff
        fields = ['id', 'type', 'level', 'title', 'text', 'link', 'count', 'created_at', 'updated_at', 'read']
        read_only_fields = fields

    @extend_schema_field(serializers.BooleanField())
    def get_read(self, notification):
        # Annote par la vue (lue_par_moi) pour eviter une requete par ligne.
        lue = getattr(notification, 'lue_par_moi', None)
        if lue is not None:
            return bool(lue)
        return notification.lue_par.filter(pk=self.context['request'].user.pk).exists()


class PreferencesNotificationSerializer(serializers.ModelSerializer):
    disabled_types = serializers.ListField(
        source='types_desactives', child=serializers.ChoiceField(choices=TypeNotification.choices), required=False
    )
    browser = serializers.BooleanField(source='navigateur', required=False)
    sound = serializers.BooleanField(source='son', required=False)

    class Meta:
        model = PreferencesNotificationStaff
        fields = ['disabled_types', 'browser', 'sound']


class EntreeAuditSerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()
    target_type = serializers.CharField(source='type_cible', read_only=True)
    target_id = serializers.UUIDField(source='identifiant_cible', read_only=True)
    target_label = serializers.SerializerMethodField()
    before = serializers.JSONField(source='valeur_precedente', read_only=True)
    after = serializers.JSONField(source='valeur_nouvelle', read_only=True)
    at = serializers.DateTimeField(source='survenue_le', read_only=True)

    class Meta:
        model = EntreeAudit
        fields = ['id', 'actor', 'action', 'target_type', 'target_id', 'target_label', 'before', 'after', 'at']
        read_only_fields = fields

    @extend_schema_field(serializers.DictField())
    def get_actor(self, entree):
        if not entree.acteur_id:
            return None
        return {
            'id': str(entree.acteur_id),
            'name': entree.acteur.nom_complet,
            'is_admin': entree.acteur.is_superuser,
        }

    @extend_schema_field(serializers.CharField())
    def get_target_label(self, entree):
        return (entree.valeur_nouvelle or {}).get('libelle', '')


class CampagneSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='nom')
    advertiser = serializers.CharField(source='annonceur')
    creative_url = serializers.URLField(source='url_creation')
    target_url = serializers.URLField(source='url_cible')
    placement = serializers.ChoiceField(source='emplacement', choices=Emplacement.choices)
    placement_label = serializers.CharField(source='get_emplacement_display', read_only=True)
    cities = serializers.ListField(source='villes_ciblees', child=serializers.CharField(max_length=255), required=False)
    starts_at = serializers.DateTimeField(source='debute_le')
    ends_at = serializers.DateTimeField(source='termine_le')
    daily_cap = serializers.IntegerField(source='plafond_journalier', min_value=1)
    active = serializers.BooleanField(source='est_active', required=False)
    impressions_today = serializers.SerializerMethodField()
    clicks_today = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()

    class Meta:
        model = CampagnePublicitaire
        fields = [
            'id', 'name', 'advertiser', 'creative_url', 'target_url', 'placement', 'placement_label',
            'cities', 'starts_at', 'ends_at', 'daily_cap', 'active',
            'impressions_today', 'clicks_today', 'status',
        ]

    def validate(self, donnees):
        debut = donnees.get('debute_le', getattr(self.instance, 'debute_le', None))
        fin = donnees.get('termine_le', getattr(self.instance, 'termine_le', None))
        if debut and fin and fin <= debut:
            raise serializers.ValidationError({'ends_at': 'La fin doit etre apres le debut.'})
        return donnees

    @extend_schema_field(serializers.IntegerField())
    def get_impressions_today(self, campagne):
        if hasattr(campagne, 'impressions_jour'):
            return campagne.impressions_jour
        return campagne.impressions.filter(survenue_le__date=timezone.localdate()).exclude(evenement=EVENEMENT_CLIC).count()

    @extend_schema_field(serializers.IntegerField())
    def get_clicks_today(self, campagne):
        if hasattr(campagne, 'clics_jour'):
            return campagne.clics_jour
        return campagne.impressions.filter(survenue_le__date=timezone.localdate(), evenement=EVENEMENT_CLIC).count()

    @extend_schema_field(serializers.ChoiceField(choices=['INACTIVE', 'PROGRAMMEE', 'TERMINEE', 'PLAFOND', 'ACTIVE']))
    def get_status(self, campagne):
        maintenant = timezone.now()
        if not campagne.est_active:
            return 'INACTIVE'
        if campagne.debute_le > maintenant:
            return 'PROGRAMMEE'
        if campagne.termine_le < maintenant:
            return 'TERMINEE'
        total_jour = self.get_impressions_today(campagne) + self.get_clicks_today(campagne)
        if total_jour >= campagne.plafond_journalier:
            return 'PLAFOND'
        return 'ACTIVE'
