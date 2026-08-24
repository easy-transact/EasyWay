from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import AdresseEnregistree, LibelleAdresse, Lieu, RechercheRecente

# Champs declares avec `source=` : la reponse API parle anglais, les modeles/
# colonnes DB restent en francais (aucune migration, cf. discussion).


class LieuRechercheSerializer(serializers.ModelSerializer):
    """Resultat de GET /api/places/search/ : forme allegee attendue par
    l'ecran de resultats (label/sublabel/distance), pas le detail complet."""

    label = serializers.CharField(source='nom')
    sublabel = serializers.SerializerMethodField()
    category = serializers.CharField(source='categorie')
    lat = serializers.SerializerMethodField()
    lon = serializers.SerializerMethodField()
    distance_m = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()

    class Meta:
        model = Lieu
        fields = ['id', 'label', 'sublabel', 'category', 'lat', 'lon', 'distance_m', 'source']

    @extend_schema_field(serializers.CharField())
    def get_sublabel(self, lieu):
        return lieu.quartier or lieu.adresse or lieu.ville

    @extend_schema_field(serializers.CharField())
    def get_source(self, lieu):
        return 'local'

    @extend_schema_field(serializers.FloatField())
    def get_lat(self, lieu):
        return lieu.position.y

    @extend_schema_field(serializers.FloatField())
    def get_lon(self, lieu):
        return lieu.position.x

    @extend_schema_field(serializers.IntegerField(allow_null=True))
    def get_distance_m(self, lieu):
        distance = getattr(lieu, 'distance', None)
        return round(distance.m) if distance is not None else None


class LieuDetailSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source='nom', read_only=True)
    category = serializers.CharField(source='categorie', read_only=True)
    address = serializers.CharField(source='adresse', read_only=True)
    neighborhood = serializers.CharField(source='quartier', read_only=True)
    city = serializers.CharField(source='ville', read_only=True)
    lat = serializers.SerializerMethodField()
    lon = serializers.SerializerMethodField()
    status = serializers.CharField(source='statut', read_only=True)
    popularity_score = serializers.IntegerField(source='score_popularite', read_only=True)

    class Meta:
        model = Lieu
        fields = [
            'id', 'name', 'category', 'address', 'neighborhood', 'city',
            'lat', 'lon', 'source', 'status', 'popularity_score',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.FloatField())
    def get_lat(self, lieu):
        return lieu.position.y

    @extend_schema_field(serializers.FloatField())
    def get_lon(self, lieu):
        return lieu.position.x


class LieuPropositionSerializer(serializers.Serializer):
    """POST /api/places/propose/ : soumission utilisateur, mise en file de
    moderation (statut EN_ATTENTE, cf. Lieu.approuver/rejeter)."""

    name = serializers.CharField(max_length=255, source='nom')
    category = serializers.CharField(max_length=100, source='categorie')
    address = serializers.CharField(max_length=500, required=False, allow_blank=True, source='adresse')
    neighborhood = serializers.CharField(max_length=255, required=False, allow_blank=True, source='quartier')
    city = serializers.CharField(max_length=255, source='ville')
    lat = serializers.FloatField()
    lon = serializers.FloatField()

    def create(self, validated_data):
        from django.contrib.gis.geos import Point

        from .models import SourceLieu, StatutLieu
        from .utils import normaliser

        lat = validated_data.pop('lat')
        lon = validated_data.pop('lon')
        return Lieu.objects.create(
            position=Point(lon, lat, srid=4326),
            nom_normalise=normaliser(validated_data['nom']),
            source=SourceLieu.UTILISATEUR,
            statut=StatutLieu.EN_ATTENTE,
            propose_par=self.context['request'].user,
            **validated_data,
        )


class AdresseEnregistreeSerializer(serializers.ModelSerializer):
    place = serializers.PrimaryKeyRelatedField(
        source='lieu', queryset=Lieu.objects.all(), required=False, allow_null=True
    )
    label = serializers.ChoiceField(choices=LibelleAdresse.choices, source='libelle')
    custom_name = serializers.CharField(source='nom_personnalise', required=False, allow_null=True)
    address = serializers.CharField(source='adresse')
    lat = serializers.FloatField(write_only=True)
    lon = serializers.FloatField(write_only=True)
    position_lat = serializers.SerializerMethodField()
    position_lon = serializers.SerializerMethodField()

    class Meta:
        model = AdresseEnregistree
        fields = [
            'id', 'place', 'label', 'custom_name', 'address',
            'lat', 'lon', 'position_lat', 'position_lon',
        ]

    @extend_schema_field(serializers.FloatField())
    def get_position_lat(self, adresse):
        return adresse.position.y

    @extend_schema_field(serializers.FloatField())
    def get_position_lon(self, adresse):
        return adresse.position.x

    def create(self, validated_data):
        from django.contrib.gis.geos import Point

        lat = validated_data.pop('lat')
        lon = validated_data.pop('lon')
        return AdresseEnregistree.objects.create(
            position=Point(lon, lat, srid=4326),
            utilisateur=self.context['request'].user,
            **validated_data,
        )

    def update(self, instance, validated_data):
        lat = validated_data.pop('lat', None)
        lon = validated_data.pop('lon', None)
        if lat is not None and lon is not None:
            from django.contrib.gis.geos import Point
            instance.position = Point(lon, lat, srid=4326)
        for champ, valeur in validated_data.items():
            setattr(instance, champ, valeur)
        instance.save()
        return instance


class RechercheRecenteSerializer(serializers.ModelSerializer):
    label = serializers.CharField(source='libelle')
    sublabel = serializers.CharField(source='sous_libelle', required=False, allow_null=True)
    lat = serializers.FloatField(write_only=True)
    lon = serializers.FloatField(write_only=True)
    searched_at = serializers.DateTimeField(source='recherche_le', read_only=True)

    class Meta:
        model = RechercheRecente
        fields = ['id', 'label', 'sublabel', 'lat', 'lon', 'searched_at']
        read_only_fields = ['id', 'searched_at']

    def create(self, validated_data):
        from django.contrib.gis.geos import Point

        lat = validated_data.pop('lat')
        lon = validated_data.pop('lon')
        return RechercheRecente.objects.create(
            position=Point(lon, lat, srid=4326),
            utilisateur=self.context['request'].user,
            **validated_data,
        )
