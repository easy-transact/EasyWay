from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Incident, TypeIncident


class IncidentSerializer(serializers.ModelSerializer):
    lat = serializers.SerializerMethodField()
    lon = serializers.SerializerMethodField()
    impact_estime = serializers.SerializerMethodField()

    class Meta:
        model = Incident
        fields = [
            'id', 'type', 'sous_type', 'lat', 'lon', 'cap', 'nom_voie',
            'confirmations', 'infirmations', 'score_confiance', 'impact_estime',
            'statut', 'severite', 'expire_le', 'cree_le',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.FloatField())
    def get_lat(self, incident):
        return incident.position.y

    @extend_schema_field(serializers.FloatField())
    def get_lon(self, incident):
        return incident.position.x

    @extend_schema_field(serializers.IntegerField())
    def get_impact_estime(self, incident):
        return incident.impact_estime()


class IncidentAvecDoublonSerializer(IncidentSerializer):
    """Documentation only: forme reelle de la reponse d'IncidentCreationView,
    IncidentSerializer + le flag de fusion ajoute manuellement dans la vue."""

    doublon_de_existant = serializers.BooleanField(read_only=True)

    class Meta(IncidentSerializer.Meta):
        fields = IncidentSerializer.Meta.fields + ['doublon_de_existant']
        read_only_fields = fields


class IncidentCreationSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=TypeIncident.choices)
    sous_type = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    lat = serializers.FloatField()
    lon = serializers.FloatField()
    cap = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=359, default=None)


class VoteIncidentSerializer(serializers.Serializer):
    sens = serializers.ChoiceField(choices=['confirmer', 'infirmer'])
