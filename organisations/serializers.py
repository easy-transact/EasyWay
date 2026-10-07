import re

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from accounts.models import InscriptionListeAttente, Utilisateur
from accounts.utils import NumeroTelephoneInvalide, valider_et_normaliser_telephone

from .models import (
    Chauffeur,
    FormuleOrganisation,
    JournalOrganisation,
    MembreOrganisation,
    Organisation,
    RoleOrganisation,
    StatutFiche,
    StatutVehicule,
    Vehicule,
)

# Champs declares avec `source=` : l'API parle anglais, les modeles restent
# en francais -- meme convention que le reste du projet.


def _telephone(valeur):
    try:
        return valider_et_normaliser_telephone(valeur)
    except NumeroTelephoneInvalide as exc:
        raise serializers.ValidationError(str(exc)) from exc


class OrganisationSerializer(serializers.ModelSerializer):
    legal_name = serializers.CharField(source='raison_sociale')
    trade_name = serializers.CharField(source='nom_commercial', required=False, allow_blank=True)
    city = serializers.CharField(source='ville')
    address = serializers.CharField(source='adresse', required=False, allow_blank=True)
    phone = serializers.CharField(source='telephone', required=False, allow_blank=True)
    status = serializers.CharField(source='statut', read_only=True)
    plan = serializers.ChoiceField(source='formule', choices=FormuleOrganisation.choices, required=False)
    created_at = serializers.DateTimeField(source='cree_le', read_only=True)

    class Meta:
        model = Organisation
        fields = [
            'id', 'type', 'legal_name', 'trade_name', 'rccm', 'niu', 'city', 'address', 'phone', 'email',
            'status', 'plan', 'created_at',
        ]
        read_only_fields = ['id', 'status', 'created_at']

    def validate_phone(self, valeur):
        return _telephone(valeur) if valeur else ''


class OrganisationProfilSerializer(OrganisationSerializer):
    """PATCH /api/org/profile/ : l'agence modifie ses coordonnees, jamais son
    type, sa raison sociale ni sa formule (reserves au staff)."""

    def get_fields(self):
        champs = super().get_fields()
        for nom in ('type', 'legal_name', 'plan'):
            champs[nom].read_only = True
        return champs


class ProprietaireSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=255)
    phone = serializers.CharField()
    # Obligatoire : c'est par email que part le lien d'activation.
    email = serializers.EmailField()

    def validate_phone(self, valeur):
        return _telephone(valeur)

    def validate_email(self, valeur):
        return Utilisateur.objects.normalize_email(valeur)


class OrganisationCreationSerializer(OrganisationSerializer):
    """POST /api/staff/organisations/ : l'organisation + son proprietaire
    (invite par email). waitlist_id : inscription de la liste d'attente
    convertie -- marquee contactee et liee a l'organisation."""

    owner = ProprietaireSerializer(write_only=True)
    waitlist_id = serializers.PrimaryKeyRelatedField(
        queryset=InscriptionListeAttente.objects.all(), source='inscription_liste_attente',
        required=False, allow_null=True, write_only=True,
    )

    class Meta(OrganisationSerializer.Meta):
        fields = OrganisationSerializer.Meta.fields + ['owner', 'waitlist_id']

    def validate_waitlist_id(self, inscription):
        if inscription is not None and Organisation.objects.filter(inscription_liste_attente=inscription).exists():
            raise serializers.ValidationError('This waitlist entry was already converted.')
        return inscription


class MembreSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source='utilisateur.nom_complet', read_only=True)
    phone = serializers.CharField(source='utilisateur.telephone', read_only=True)
    email = serializers.EmailField(source='utilisateur.email', read_only=True)
    active = serializers.BooleanField(source='actif', required=False)
    invited_at = serializers.DateTimeField(source='invite_le', read_only=True)
    accepted_at = serializers.DateTimeField(source='accepte_le', read_only=True)
    invited_by = serializers.CharField(source='invite_par.nom_complet', read_only=True, default=None)

    class Meta:
        model = MembreOrganisation
        fields = ['id', 'full_name', 'phone', 'email', 'role', 'active', 'invited_at', 'accepted_at', 'invited_by']
        read_only_fields = ['id']

    def validate_role(self, role):
        # La propriete change seulement par transfert explicite (make-owner).
        if role == RoleOrganisation.PROPRIETAIRE:
            raise serializers.ValidationError('Use the transfer-ownership action to change the owner.')
        return role


class InvitationSerializer(ProprietaireSerializer):
    role = serializers.ChoiceField(
        choices=[r for r in RoleOrganisation.choices if r[0] != RoleOrganisation.PROPRIETAIRE]
    )


class AccepterInvitationSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_new_password(self, valeur):
        try:
            validate_password(valeur)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from exc
        return valeur


class ChauffeurSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(source='nom_complet', max_length=255)
    phone = serializers.CharField(source='telephone', required=False, allow_blank=True)
    license_number = serializers.CharField(source='numero_permis', required=False, allow_blank=True, max_length=50)
    license_category = serializers.CharField(source='categorie_permis', required=False, allow_blank=True, max_length=20)
    license_expires_on = serializers.DateField(source='permis_expire_le', required=False, allow_null=True)
    status = serializers.ChoiceField(
        source='statut', required=False,
        choices=[StatutFiche.ACTIF, StatutFiche.INACTIF],  # ARCHIVE = DELETE
    )
    vehicles = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(source='cree_le', read_only=True)

    class Meta:
        model = Chauffeur
        fields = [
            'id', 'full_name', 'phone', 'license_number', 'license_category', 'license_expires_on',
            'status', 'vehicles', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']

    def get_vehicles(self, chauffeur) -> list[str]:
        return [v.immatriculation for v in chauffeur.vehicules.all() if v.statut != StatutVehicule.ARCHIVE]

    def validate_phone(self, valeur):
        return _telephone(valeur) if valeur else ''


def normaliser_immatriculation(valeur: str) -> str:
    """'lt 123-ab' -> 'LT123AB' : une meme plaque saisie avec ou sans espaces
    ou tirets ne doit pas creer deux vehicules."""
    return re.sub(r'[\s\-.]', '', valeur).upper()


class VehiculeSerializer(serializers.ModelSerializer):
    plate = serializers.CharField(source='immatriculation', max_length=20)
    make = serializers.CharField(source='marque', required=False, allow_blank=True, max_length=100)
    model = serializers.CharField(source='modele', required=False, allow_blank=True, max_length=100)
    seats = serializers.IntegerField(source='places', required=False, allow_null=True, min_value=1, max_value=200)
    insurance_expires_on = serializers.DateField(source='assurance_expire_le', required=False, allow_null=True)
    inspection_expires_on = serializers.DateField(source='visite_technique_expire_le', required=False, allow_null=True)
    status = serializers.ChoiceField(
        source='statut', required=False,
        choices=[StatutVehicule.ACTIF, StatutVehicule.MAINTENANCE, StatutVehicule.INACTIF],  # ARCHIVE = DELETE
    )
    driver_id = serializers.PrimaryKeyRelatedField(
        source='chauffeur', queryset=Chauffeur.objects.none(), required=False, allow_null=True,
    )
    driver_name = serializers.CharField(source='chauffeur.nom_complet', read_only=True, default=None)
    created_at = serializers.DateTimeField(source='cree_le', read_only=True)

    class Meta:
        model = Vehicule
        fields = [
            'id', 'plate', 'type', 'make', 'model', 'seats', 'insurance_expires_on', 'inspection_expires_on',
            'status', 'driver_id', 'driver_name', 'created_at',
        ]
        read_only_fields = ['id', 'created_at']

    def __init__(self, *args, organisation=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organisation = organisation
        # Un vehicule ne peut etre affecte qu'a un chauffeur de la meme agence :
        # un id d'une autre agence donne "objet introuvable", comme s'il n'existait pas.
        if organisation is not None:
            self.fields['driver_id'].queryset = Chauffeur.objects.filter(
                organisation=organisation, statut__in=[StatutFiche.ACTIF, StatutFiche.INACTIF],
            )

    def validate_plate(self, valeur):
        plaque = normaliser_immatriculation(valeur)
        if not plaque:
            raise serializers.ValidationError('This field may not be blank.')
        doublons = Vehicule.objects.filter(organisation=self.organisation, immatriculation=plaque)
        if self.instance is not None:
            doublons = doublons.exclude(pk=self.instance.pk)
        if doublons.exists():
            raise serializers.ValidationError('A vehicle with this plate already exists in your organisation.')
        return plaque


class JournalSerializer(serializers.ModelSerializer):
    actor = serializers.CharField(source='acteur.nom_complet', read_only=True, default=None)
    target_type = serializers.CharField(source='cible_type')
    target_id = serializers.CharField(source='cible_id')
    created_at = serializers.DateTimeField(source='cree_le')

    class Meta:
        model = JournalOrganisation
        fields = ['id', 'actor', 'action', 'target_type', 'target_id', 'details', 'created_at']
