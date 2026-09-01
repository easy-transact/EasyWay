from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from trips.polyline import decoder_polyline6

from .models import Incident, TypeIncident, SOUS_TYPES_PAR_TYPE

# Champs declares avec `source=` : la reponse API parle anglais, les modeles/
# colonnes DB restent en francais (aucune migration, cf. discussion).


class IncidentSerializer(serializers.ModelSerializer):
    lat = serializers.SerializerMethodField()
    lon = serializers.SerializerMethodField()
    subtype = serializers.CharField(source='sous_type', read_only=True)
    heading = serializers.IntegerField(source='cap', read_only=True)
    street_name = serializers.CharField(source='nom_voie', read_only=True)
    city = serializers.CharField(source='ville', read_only=True)
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
            'id', 'type', 'subtype', 'lat', 'lon', 'heading', 'street_name', 'city',
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


class IncidentModerationSerializer(IncidentSerializer):
    """GET/POST /api/staff/incidents/... : IncidentSerializer + l'auteur et le
    motif de retrait, utiles en moderation mais absents de la reponse
    publique (jamais expose a un utilisateur autre que l'auteur/le staff)."""

    author_phone = serializers.SerializerMethodField()
    reason = serializers.CharField(source='motif_retrait', read_only=True, allow_null=True)

    class Meta(IncidentSerializer.Meta):
        fields = IncidentSerializer.Meta.fields + ['author_phone', 'reason']
        read_only_fields = fields

    @extend_schema_field(serializers.CharField())
    def get_author_phone(self, incident):
        return incident.auteur.telephone


class IncidentRetraitSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


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

    def validate(self, data):
        incident_type = data.get('type')
        sous_type = data.get('sous_type')
        if sous_type:
            allowed_subtypes = SOUS_TYPES_PAR_TYPE.get(incident_type, [])
            if sous_type not in allowed_subtypes:
                raise serializers.ValidationError(
                    {'subtype': f"Invalid subtype for {incident_type}. Allowed: {', '.join(allowed_subtypes) if allowed_subtypes else 'none'}"}
                )
        return data


class VoteIncidentSerializer(serializers.Serializer):
    direction = serializers.ChoiceField(choices=['confirm', 'dispute'], source='sens')


BUFFER_M_DEFAUT = 300  # couloir de recherche autour du trace -- pas mesure sur donnees reelles, a ajuster
BUFFER_M_MAX = 1000


class IncidentsSurTrajetSerializer(serializers.Serializer):
    """POST /api/incidents/along-route/ : geometrie en corps de requete
    (encodee polyline6, meme format que routes/calculate -> route.geometry),
    jamais en cellules H3 dans l'URL -- cf. discussion frontend, un trajet
    long (ex. Douala-Yaounde, ~250km) generait des centaines de cellules et
    une URL ingerable. validate_geometry() decode directement en liste de
    points (lon, lat) : la vue construit la LineString a partir de ca, pas
    du texte brut, pour ne decoder qu'une seule fois."""

    geometry = serializers.CharField()
    buffer_m = serializers.IntegerField(required=False, default=BUFFER_M_DEFAUT, min_value=1, max_value=BUFFER_M_MAX)

    def validate_geometry(self, valeur):
        try:
            points = decoder_polyline6(valeur)
        except Exception:
            raise serializers.ValidationError('Invalid encoded polyline.')
        if len(points) < 2:
            raise serializers.ValidationError('geometry must decode to at least 2 points.')
        return points
