from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import AdresseEnregistree, Lieu, RechercheRecente


class LieuRechercheSerializer(serializers.ModelSerializer):
    """Resultat de GET /api/lieux/recherche/ : forme allegee attendue par
    l'ecran de resultats (libelle/sous-libelle/distance), pas le detail complet."""

    libelle = serializers.CharField(source='nom')
    sous_libelle = serializers.SerializerMethodField()
    lat = serializers.SerializerMethodField()
    lon = serializers.SerializerMethodField()
    distance_m = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()

    class Meta:
        model = Lieu
        fields = ['id', 'libelle', 'sous_libelle', 'categorie', 'lat', 'lon', 'distance_m', 'source']

    @extend_schema_field(serializers.CharField())
    def get_sous_libelle(self, lieu):
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
    lat = serializers.SerializerMethodField()
    lon = serializers.SerializerMethodField()

    class Meta:
        model = Lieu
        fields = [
            'id', 'nom', 'categorie', 'adresse', 'quartier', 'ville',
            'lat', 'lon', 'source', 'statut', 'score_popularite',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.FloatField())
    def get_lat(self, lieu):
        return lieu.position.y

    @extend_schema_field(serializers.FloatField())
    def get_lon(self, lieu):
        return lieu.position.x


class LieuPropositionSerializer(serializers.Serializer):
    """POST /api/lieux/proposer/ : soumission utilisateur, mise en file de
    moderation (statut EN_ATTENTE, cf. Lieu.approuver/rejeter)."""

    nom = serializers.CharField(max_length=255)
    categorie = serializers.CharField(max_length=100)
    adresse = serializers.CharField(max_length=500, required=False, allow_blank=True)
    quartier = serializers.CharField(max_length=255, required=False, allow_blank=True)
    ville = serializers.CharField(max_length=255)
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
    lat = serializers.FloatField(write_only=True)
    lon = serializers.FloatField(write_only=True)
    position_lat = serializers.SerializerMethodField()
    position_lon = serializers.SerializerMethodField()

    class Meta:
        model = AdresseEnregistree
        fields = [
            'id', 'lieu', 'libelle', 'nom_personnalise', 'adresse',
            'lat', 'lon', 'position_lat', 'position_lon',
        ]
        extra_kwargs = {'lieu': {'required': False, 'allow_null': True}}

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
    lat = serializers.FloatField(write_only=True)
    lon = serializers.FloatField(write_only=True)

    class Meta:
        model = RechercheRecente
        fields = ['id', 'libelle', 'sous_libelle', 'lat', 'lon', 'recherche_le']
        read_only_fields = ['id', 'recherche_le']

    def create(self, validated_data):
        from django.contrib.gis.geos import Point

        lat = validated_data.pop('lat')
        lon = validated_data.pop('lon')
        return RechercheRecente.objects.create(
            position=Point(lon, lat, srid=4326),
            utilisateur=self.context['request'].user,
            **validated_data,
        )
