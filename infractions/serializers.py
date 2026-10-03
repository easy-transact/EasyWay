from rest_framework import serializers

from places.utils import normaliser

from .models import Infraction


class InfractionSerializer(serializers.ModelSerializer):
    """Lecture publique (GET /api/infractions/) et ecriture staff
    (POST/PATCH /api/staff/infractions/, import)."""

    label = serializers.CharField(source='libelle', max_length=255)
    category = serializers.CharField(source='categorie', max_length=100, required=False, allow_blank=True)
    fine_amount = serializers.IntegerField(source='amende_fcfa', min_value=0)
    fine_amount_max = serializers.IntegerField(
        source='amende_max_fcfa', min_value=0, required=False, allow_null=True
    )
    legal_reference = serializers.CharField(
        source='reference_legale', max_length=255, required=False, allow_blank=True
    )
    additional_penalties = serializers.CharField(
        source='sanctions_complementaires', required=False, allow_blank=True
    )
    is_active = serializers.BooleanField(source='actif', required=False)
    created_at = serializers.DateTimeField(source='cree_le', read_only=True)
    updated_at = serializers.DateTimeField(source='modifie_le', read_only=True)

    class Meta:
        model = Infraction
        fields = [
            'id', 'code', 'label', 'category', 'fine_amount', 'fine_amount_max',
            'legal_reference', 'additional_penalties', 'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
        # `code` : unicite verifiee par la contrainte DB + validate_code
        # (le UniqueValidator auto rejetterait '' comme doublon de '').
        extra_kwargs = {'code': {'required': False, 'allow_null': True, 'allow_blank': True, 'validators': []}}

    def validate_code(self, code):
        code = (code or '').strip() or None
        if code and Infraction.objects.filter(code=code).exclude(pk=getattr(self.instance, 'pk', None)).exists():
            raise serializers.ValidationError('An infraction with this code already exists.')
        return code

    def validate_label(self, libelle):
        libelle = libelle.strip()
        doublon = Infraction.objects.filter(libelle_normalise=normaliser(libelle))
        if self.instance is not None:
            doublon = doublon.exclude(pk=self.instance.pk)
        if doublon.exists():
            raise serializers.ValidationError('An infraction with this label already exists.')
        return libelle

    def validate(self, attrs):
        minimum = attrs.get('amende_fcfa', getattr(self.instance, 'amende_fcfa', None))
        maximum = attrs.get('amende_max_fcfa', getattr(self.instance, 'amende_max_fcfa', None))
        if maximum is not None and minimum is not None and maximum < minimum:
            raise serializers.ValidationError({'fine_amount_max': 'Must be greater than or equal to fine_amount.'})
        return attrs


class CategorieInfractionSerializer(serializers.Serializer):
    category = serializers.CharField()
    count = serializers.IntegerField()


class ImportInfractionsSerializer(serializers.Serializer):
    """POST /api/staff/infractions/import/ : soit `infractions` (JSON), soit
    `file` (CSV ou JSON, multipart)."""

    infractions = serializers.ListField(child=serializers.DictField(), required=False)
    file = serializers.FileField(required=False)
    dry_run = serializers.BooleanField(default=False)

    def validate(self, attrs):
        if bool(attrs.get('infractions')) == bool(attrs.get('file')):
            raise serializers.ValidationError('Provide either `infractions` or `file`, not both.')
        return attrs


class ResultatImportSerializer(serializers.Serializer):
    created = serializers.IntegerField()
    updated = serializers.IntegerField()
    errors = serializers.ListField(child=serializers.DictField())
    dry_run = serializers.BooleanField()
