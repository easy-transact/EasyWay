from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Incident, TypeIncident

# Champs declares avec `source=` : la reponse API parle anglais, les modeles/
# colonnes DB restent en francais (aucune migration, cf. discussion).


class IncidentSerializer(serializers.ModelSerializer):
    lat = serializers.SerializerMethodField()
    lon = serializers.SerializerMethodField()
    subtype = serializers.CharField(source='sous_type', read_only=True)
    heading = serializers.IntegerField(source='cap', read_only=True)
    street_name = serializers.CharField(source='nom_voie', read_only=True)
    disputes = serializers.IntegerField(source='infirmations', read_only=True)
    confidence_score = serializers.DecimalField(source='score_confiance', max_digits=6, decimal_places=2, read_only=True)
    estimated_impact = serializers.SerializerMethodField()
    status = serializers.CharField(source='statut', read_only=True)
    severity = serializers.IntegerField(source='severite', read_only=True)
    expires_at = serializers.DateTimeField(source='expire_le', read_only=True)
    created_at = serializers.DateTimeField(source='cree_le', read_only=True)

    class Meta:
        model = Incident
        fields = [
            'id', 'type', 'subtype', 'lat', 'lon', 'heading', 'street_name',
            'confirmations', 'disputes', 'confidence_score', 'estimated_impact',
            'status', 'severity', 'expires_at', 'created_at',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.FloatField())
    def get_lat(self, incident):
        return incident.position.y

    @extend_schema_field(serializers.FloatField())
    def get_lon(self, incident):
        return incident.position.x

    @extend_schema_field(serializers.IntegerField())
    def get_estimated_impact(self, incident):
        return incident.impact_estime()


class IncidentAvecDoublonSerializer(IncidentSerializer):
    """Documentation only: forme reelle de la reponse d'IncidentCreationView,
    IncidentSerializer + le flag de fusion ajoute manuellement dans la vue."""

    duplicate_of_existing = serializers.BooleanField(read_only=True)

    class Meta(IncidentSerializer.Meta):
        fields = IncidentSerializer.Meta.fields + ['duplicate_of_existing']
        read_only_fields = fields


class IncidentCreationSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=TypeIncident.choices)
    subtype = serializers.CharField(
        max_length=100, required=False, allow_blank=True, default='', source='sous_type'
    )
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    heading = serializers.IntegerField(
        required=False, allow_null=True, min_value=0, max_value=359, default=None, source='cap'
    )


class VoteIncidentSerializer(serializers.Serializer):
    direction = serializers.ChoiceField(choices=['confirm', 'dispute'], source='sens')
