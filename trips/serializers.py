from django.contrib.gis.geos import LineString, Point
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from places.models import Lieu

from .models import Itineraire, Manoeuvre, StatutTrajet, Trajet
from .polyline import decoder_polyline6

# Champs declares avec `source=` : la reponse API parle anglais, les modeles/
# colonnes DB restent en francais (aucune migration, cf. discussion). Pour
# ItineraireCandidatSerializer/ManoeuvreCandidatSerializer, source= fonctionne
# aussi bien sur les dicts bruts renvoyes par ServiceItineraire que sur des
# instances de modele -- DRF resout `source` par cle ou attribut indifferemment.


class PointEvitementSerializer(serializers.Serializer):
    lat = serializers.FloatField()
    lon = serializers.FloatField()


class CalculItineraireSerializer(serializers.Serializer):
    origin_lat = serializers.FloatField(source='origine_lat')
    origin_lon = serializers.FloatField(source='origine_lon')
    destination_lat = serializers.FloatField()
    destination_lon = serializers.FloatField()
    avoid = PointEvitementSerializer(
        many=True, required=False, default=list, source='eviter',
        help_text="Points a exclure du graphe de routage (ex. position d'un incident).",
    )


class ManoeuvreCandidatSerializer(serializers.Serializer):
    type = serializers.CharField()
    instruction = serializers.CharField(allow_blank=True)
    voice_instruction = serializers.CharField(allow_blank=True, source='instruction_vocale')
    distance = serializers.IntegerField()
    duration = serializers.IntegerField(source='duree')
    street_name = serializers.CharField(allow_blank=True, source='nom_voie')


class ItineraireCandidatSerializer(serializers.Serializer):
    """Forme d'un itineraire calcule mais pas encore persiste (retour de
    POST /api/routes/calculate/). Le client renvoie l'objet choisi tel
    quel a POST /api/trips/ pour le faire persister."""

    route_id = serializers.CharField(source='identifiant')
    label = serializers.CharField(source='libelle')
    distance = serializers.IntegerField()
    duration = serializers.IntegerField(source='duree')
    duration_with_traffic = serializers.IntegerField(allow_null=True, source='duree_avec_trafic')
    traffic_level = serializers.CharField(source='niveau_trafic')
    geometry = serializers.CharField(source='geometrie')
    is_recommended = serializers.BooleanField(source='est_recommande')
    maneuvers = ManoeuvreCandidatSerializer(many=True, source='manoeuvres')
    degraded = serializers.BooleanField(default=False, source='degrade')


class ManoeuvreSerializer(serializers.ModelSerializer):
    order = serializers.IntegerField(source='ordre', read_only=True)
    voice_instruction = serializers.CharField(source='instruction_vocale', read_only=True)
    duration = serializers.IntegerField(source='duree', read_only=True)
    street_name = serializers.CharField(source='nom_voie', read_only=True)

    class Meta:
        model = Manoeuvre
        fields = ['order', 'type', 'instruction', 'voice_instruction', 'distance', 'duration', 'street_name']
        read_only_fields = fields


class ItineraireSerializer(serializers.ModelSerializer):
    route_id = serializers.CharField(source='identifiant', read_only=True)
    is_recommended = serializers.BooleanField(source='est_recommande', read_only=True)
    label = serializers.CharField(source='libelle', read_only=True)
    duration = serializers.IntegerField(source='duree', read_only=True)
    duration_with_traffic = serializers.IntegerField(source='duree_avec_trafic', read_only=True)
    traffic_level = serializers.CharField(source='niveau_trafic', read_only=True)
    geometry = serializers.CharField(source='geometrie', read_only=True)
    maneuvers = ManoeuvreSerializer(many=True, read_only=True, source='manoeuvres')

    class Meta:
        model = Itineraire
        fields = [
            'route_id', 'is_recommended', 'label', 'distance', 'duration',
            'duration_with_traffic', 'traffic_level', 'geometry', 'maneuvers',
        ]
        read_only_fields = fields


class TrajetSerializer(serializers.ModelSerializer):
    origin_label = serializers.CharField(source='libelle_origine', read_only=True)
    origin_lat = serializers.SerializerMethodField()
    origin_lon = serializers.SerializerMethodField()
    destination_label = serializers.CharField(source='libelle_destination', read_only=True)
    destination_lat = serializers.SerializerMethodField()
    destination_lon = serializers.SerializerMethodField()
    destination_place = serializers.PrimaryKeyRelatedField(source='lieu_destination', read_only=True)
    chosen_route_id = serializers.CharField(source='itineraire_choisi', read_only=True)
    planned_distance = serializers.IntegerField(source='distance_prevue', read_only=True)
    planned_duration = serializers.IntegerField(source='duree_prevue', read_only=True)
    actual_distance = serializers.IntegerField(source='distance_reelle', read_only=True)
    actual_duration = serializers.IntegerField(source='duree_reelle', read_only=True)
    status = serializers.CharField(source='statut', read_only=True)
    incidents_avoided = serializers.IntegerField(source='incidents_evites', read_only=True)
    rating = serializers.IntegerField(source='note', read_only=True)
    comment = serializers.CharField(source='commentaire', read_only=True)
    started_at = serializers.DateTimeField(source='demarre_le', read_only=True)
    ended_at = serializers.DateTimeField(source='termine_le', read_only=True)
    routes = ItineraireSerializer(many=True, read_only=True, source='itineraires')

    class Meta:
        model = Trajet
        fields = [
            'id', 'origin_label', 'origin_lat', 'origin_lon',
            'destination_label', 'destination_lat', 'destination_lon',
            'destination_place', 'chosen_route_id',
            'planned_distance', 'planned_duration', 'actual_distance', 'actual_duration',
            'status', 'incidents_avoided', 'rating', 'comment',
            'started_at', 'ended_at', 'routes',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.FloatField())
    def get_origin_lat(self, trajet):
        return trajet.position_origine.y

    @extend_schema_field(serializers.FloatField())
    def get_origin_lon(self, trajet):
        return trajet.position_origine.x

    @extend_schema_field(serializers.FloatField())
    def get_destination_lat(self, trajet):
        return trajet.position_destination.y

    @extend_schema_field(serializers.FloatField())
    def get_destination_lon(self, trajet):
        return trajet.position_destination.x


class TrajetCreationSerializer(serializers.Serializer):
    origin_label = serializers.CharField(max_length=500, source='libelle_origine')
    origin_lat = serializers.FloatField(source='origine_lat')
    origin_lon = serializers.FloatField(source='origine_lon')
    destination_label = serializers.CharField(max_length=500, source='libelle_destination')
    destination_lat = serializers.FloatField()
    destination_lon = serializers.FloatField()
    destination_place = serializers.PrimaryKeyRelatedField(
        source='lieu_destination', queryset=Lieu.objects.all(), required=False, allow_null=True
    )
    route = ItineraireCandidatSerializer(source='itineraire')

    def create(self, validated_data):
        itineraire_data = validated_data.pop('itineraire')
        manoeuvres_data = itineraire_data.pop('manoeuvres')

        origine = Point(validated_data.pop('origine_lon'), validated_data.pop('origine_lat'), srid=4326)
        destination = Point(
            validated_data.pop('destination_lon'), validated_data.pop('destination_lat'), srid=4326
        )
        points = decoder_polyline6(itineraire_data['geometrie'])
        geometrie_ligne = LineString(points, srid=4326) if len(points) >= 2 else None

        trajet = Trajet.objects.create(
            utilisateur=self.context['request'].user,
            position_origine=origine,
            position_destination=destination,
            libelle_origine=validated_data['libelle_origine'],
            libelle_destination=validated_data['libelle_destination'],
            lieu_destination=validated_data.get('lieu_destination'),
            itineraire_choisi=itineraire_data['identifiant'],
            distance_prevue=itineraire_data['distance'],
            duree_prevue=itineraire_data['duree'],
            geometrie=geometrie_ligne,
        )
        trajet.demarrer()  # PLANIFIE -> ACTIF via la machine a etats (fixe demarre_le)

        itineraire = Itineraire.objects.create(
            trajet=trajet,
            identifiant=itineraire_data['identifiant'],
            est_recommande=True,
            libelle=itineraire_data['libelle'],
            distance=itineraire_data['distance'],
            duree=itineraire_data['duree'],
            duree_avec_trafic=itineraire_data['duree_avec_trafic'],
            niveau_trafic=itineraire_data['niveau_trafic'],
            geometrie=itineraire_data['geometrie'],
        )
        Manoeuvre.objects.bulk_create([
            Manoeuvre(itineraire=itineraire, ordre=i, **m) for i, m in enumerate(manoeuvres_data)
        ])
        return trajet


class TrajetMiseAJourSerializer(serializers.ModelSerializer):
    status = serializers.ChoiceField(choices=StatutTrajet.choices, source='statut', required=False)
    actual_distance = serializers.IntegerField(source='distance_reelle', required=False)
    actual_duration = serializers.IntegerField(source='duree_reelle', required=False)
    incidents_avoided = serializers.IntegerField(source='incidents_evites', required=False)

    class Meta:
        model = Trajet
        fields = ['status', 'actual_distance', 'actual_duration', 'incidents_avoided']

    def update(self, instance, validated_data):
        nouveau_statut = validated_data.pop('statut', None)
        if validated_data:
            for champ, valeur in validated_data.items():
                setattr(instance, champ, valeur)
            instance.save(update_fields=list(validated_data.keys()))
        if nouveau_statut:
            instance.changer_statut(nouveau_statut)  # leve TransitionInvalide si illegal
        return instance


class NoterTrajetSerializer(serializers.Serializer):
    rating = serializers.IntegerField(min_value=1, max_value=5, source='note')
    comment = serializers.CharField(required=False, allow_blank=True, default='', source='commentaire')


LIMITE_POSITIONS_PAR_LOT = 500


class PositionTelemetrieSerializer(serializers.Serializer):
    """Une position GPS du lot -- jamais persistee telle quelle (cf.
    TelemetriePositionsSerializer)."""

    lat = serializers.FloatField()
    lon = serializers.FloatField()
    speed_kmh = serializers.FloatField(required=False, allow_null=True, min_value=0, source='vitesse_kmh')
    heading = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=359, source='cap')
    timestamp = serializers.DateTimeField(source='horodatage')


class TelemetriePositionsSerializer(serializers.Serializer):
    """POST /api/telemetry/positions/ : lot de positions GPS pour un trajet
    actif de l'appelant. Ne persiste jamais les positions -- validees ici,
    publiees sur ProducteurEvenements, jamais ecrites en base sur ce chemin
    (section confidentialite du cahier des charges)."""

    trip = serializers.PrimaryKeyRelatedField(queryset=Trajet.objects.none(), source='trajet')
    positions = PositionTelemetrieSerializer(many=True, allow_empty=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get('request')
        if request is not None:
            # Un trajet d'un autre utilisateur, ou pas ACTIF, n'est simplement
            # pas dans ce queryset -- 400 (mauvaise reference), pas 403/404 :
            # on ne confirme jamais l'existence du trajet d'un tiers.
            self.fields['trip'].queryset = Trajet.objects.filter(
                utilisateur=request.user, statut=StatutTrajet.ACTIF
            )

    def validate_positions(self, positions):
        if len(positions) > LIMITE_POSITIONS_PAR_LOT:
            raise serializers.ValidationError(f'Maximum {LIMITE_POSITIONS_PAR_LOT} positions per batch.')
        return positions
