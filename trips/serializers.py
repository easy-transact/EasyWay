from django.contrib.gis.geos import LineString, Point
from rest_framework import serializers

from places.models import Lieu

from .models import Itineraire, Manoeuvre, StatutTrajet, Trajet
from .polyline import decoder_polyline6


class CalculItineraireSerializer(serializers.Serializer):
    origine_lat = serializers.FloatField()
    origine_lon = serializers.FloatField()
    destination_lat = serializers.FloatField()
    destination_lon = serializers.FloatField()


class ManoeuvreCandidatSerializer(serializers.Serializer):
    type = serializers.CharField()
    instruction = serializers.CharField(allow_blank=True)
    instruction_vocale = serializers.CharField(allow_blank=True)
    distance = serializers.IntegerField()
    duree = serializers.IntegerField()
    nom_voie = serializers.CharField(allow_blank=True)


class ItineraireCandidatSerializer(serializers.Serializer):
    """Forme d'un itineraire calcule mais pas encore persiste (retour de
    POST /api/itineraires/calculer/). Le client renvoie l'objet choisi tel
    quel a POST /api/trajets/ pour le faire persister."""

    identifiant = serializers.CharField()
    libelle = serializers.CharField()
    distance = serializers.IntegerField()
    duree = serializers.IntegerField()
    duree_avec_trafic = serializers.IntegerField(allow_null=True)
    niveau_trafic = serializers.CharField()
    geometrie = serializers.CharField()
    est_recommande = serializers.BooleanField()
    manoeuvres = ManoeuvreCandidatSerializer(many=True)
    degrade = serializers.BooleanField(default=False)


class ManoeuvreSerializer(serializers.ModelSerializer):
    class Meta:
        model = Manoeuvre
        fields = ['ordre', 'type', 'instruction', 'instruction_vocale', 'distance', 'duree', 'nom_voie']
        read_only_fields = fields


class ItineraireSerializer(serializers.ModelSerializer):
    manoeuvres = ManoeuvreSerializer(many=True, read_only=True)

    class Meta:
        model = Itineraire
        fields = [
            'identifiant', 'est_recommande', 'libelle', 'distance', 'duree',
            'duree_avec_trafic', 'niveau_trafic', 'geometrie', 'manoeuvres',
        ]
        read_only_fields = fields


class TrajetSerializer(serializers.ModelSerializer):
    origine_lat = serializers.SerializerMethodField()
    origine_lon = serializers.SerializerMethodField()
    destination_lat = serializers.SerializerMethodField()
    destination_lon = serializers.SerializerMethodField()
    itineraires = ItineraireSerializer(many=True, read_only=True)

    class Meta:
        model = Trajet
        fields = [
            'id', 'libelle_origine', 'origine_lat', 'origine_lon',
            'libelle_destination', 'destination_lat', 'destination_lon',
            'lieu_destination', 'itineraire_choisi',
            'distance_prevue', 'duree_prevue', 'distance_reelle', 'duree_reelle',
            'statut', 'incidents_evites', 'note', 'commentaire',
            'demarre_le', 'termine_le', 'itineraires',
        ]
        read_only_fields = fields

    def get_origine_lat(self, trajet):
        return trajet.position_origine.y

    def get_origine_lon(self, trajet):
        return trajet.position_origine.x

    def get_destination_lat(self, trajet):
        return trajet.position_destination.y

    def get_destination_lon(self, trajet):
        return trajet.position_destination.x


class TrajetCreationSerializer(serializers.Serializer):
    libelle_origine = serializers.CharField(max_length=500)
    origine_lat = serializers.FloatField()
    origine_lon = serializers.FloatField()
    libelle_destination = serializers.CharField(max_length=500)
    destination_lat = serializers.FloatField()
    destination_lon = serializers.FloatField()
    lieu_destination = serializers.PrimaryKeyRelatedField(
        queryset=Lieu.objects.all(), required=False, allow_null=True
    )
    itineraire = ItineraireCandidatSerializer()

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
    class Meta:
        model = Trajet
        fields = ['statut', 'distance_reelle', 'duree_reelle', 'incidents_evites']
        extra_kwargs = {champ: {'required': False} for champ in fields}

    def validate_statut(self, valeur):
        if valeur not in StatutTrajet.values:
            raise serializers.ValidationError('Statut inconnu.')
        return valeur

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
    note = serializers.IntegerField(min_value=1, max_value=5)
    commentaire = serializers.CharField(required=False, allow_blank=True, default='')
