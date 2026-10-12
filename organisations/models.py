"""
Espace agences (cf. analyse "Espace Agences EasyWay") : agences de voyage
interurbaines et gestionnaires de flotte partagent un seul modele,
Organisation, distingue par `type`. Tout ce qu'une agence possede (membres,
chauffeurs, vehicules) pointe vers son Organisation -- c'est ce rattachement
qui isole une agence des autres (cf. permissions.py).

Phase 1 : Organisation, MembreOrganisation, JournalOrganisation.
Phase 2 : Chauffeur, Vehicule.
"""

import uuid

from django.conf import settings
from django.db import models

from accounts.models import InscriptionListeAttente, TypeVehicule


class TypeOrganisation(models.TextChoices):
    VOYAGE = 'VOYAGE', 'Agence de voyage interurbaine'
    FLOTTE = 'FLOTTE', 'Gestionnaire de flotte'


class StatutOrganisation(models.TextChoices):
    # EN_ATTENTE : creee par le staff, le proprietaire n'a pas encore active
    # son compte. ACTIVE des que c'est fait. SUSPENDUE : decision du staff,
    # bloque tout /api/org/ pour ses membres sans rien effacer.
    EN_ATTENTE = 'EN_ATTENTE', 'En attente'
    ACTIVE = 'ACTIVE', 'Active'
    SUSPENDUE = 'SUSPENDUE', 'Suspendue'


class FormuleOrganisation(models.TextChoices):
    # Modele economique pas encore decide (cf. analyse) : simple etiquette,
    # aucune limite ni facturation attachee pour l'instant.
    GRATUITE = 'GRATUITE', 'Gratuite'
    PRO = 'PRO', 'Pro'


class Organisation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=10, choices=TypeOrganisation.choices)
    raison_sociale = models.CharField(max_length=255)
    nom_commercial = models.CharField(max_length=255, blank=True)
    # Facultatifs en v1 (cf. analyse, questions ouvertes).
    rccm = models.CharField(max_length=50, blank=True)
    niu = models.CharField(max_length=50, blank=True)
    ville = models.CharField(max_length=100)
    adresse = models.CharField(max_length=255, blank=True)
    telephone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)

    statut = models.CharField(
        max_length=20, choices=StatutOrganisation.choices, default=StatutOrganisation.EN_ATTENTE
    )
    formule = models.CharField(
        max_length=20, choices=FormuleOrganisation.choices, default=FormuleOrganisation.GRATUITE
    )

    cree_par = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='organisations_creees',
    )
    # Inscription de la liste d'attente convertie en cette organisation, s'il y en a une.
    inscription_liste_attente = models.OneToOneField(
        InscriptionListeAttente, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='organisation',
    )
    cree_le = models.DateTimeField(auto_now_add=True)
    modifie_le = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'organisation'

    def __str__(self):
        return self.nom_commercial or self.raison_sociale


class RoleOrganisation(models.TextChoices):
    PROPRIETAIRE = 'PROPRIETAIRE', 'Proprietaire'
    ADMIN = 'ADMIN', 'Administrateur'
    GESTIONNAIRE = 'GESTIONNAIRE', 'Gestionnaire'
    LECTEUR = 'LECTEUR', 'Lecteur'


# Plus le rang est haut, plus le role a de droits (cf. permissions.RoleMinimum).
RANG_ROLE = {
    RoleOrganisation.LECTEUR: 1,
    RoleOrganisation.GESTIONNAIRE: 2,
    RoleOrganisation.ADMIN: 3,
    RoleOrganisation.PROPRIETAIRE: 4,
}


class MembreOrganisation(models.Model):
    """Un utilisateur qui travaille dans une agence. OneToOne sur
    l'utilisateur : en v1, un utilisateur appartient a une seule organisation
    (cf. analyse, questions ouvertes)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name='membres')
    utilisateur = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='membre_organisation'
    )
    role = models.CharField(max_length=20, choices=RoleOrganisation.choices)
    actif = models.BooleanField(default=True)

    invite_par = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    invite_le = models.DateTimeField(auto_now_add=True)
    # null tant que l'invite n'a pas choisi son mot de passe (compte cree par
    # l'invitation). Rempli tout de suite si l'utilisateur existait deja.
    accepte_le = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'membre_organisation'

    def __str__(self):
        return f'{self.utilisateur} ({self.role}, {self.organisation})'

    @property
    def rang(self):
        return RANG_ROLE[self.role]


class StatutFiche(models.TextChoices):
    ACTIF = 'ACTIF', 'Actif'
    INACTIF = 'INACTIF', 'Inactif'
    # Supprime par l'agence : garde pour le journal, exclu des listes.
    ARCHIVE = 'ARCHIVE', 'Archive'


class Chauffeur(models.Model):
    """Fiche chauffeur geree par l'agence. Aucun lien avec l'application
    mobile en v1 : `utilisateur` reste vide jusqu'a la phase 4."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name='chauffeurs')
    nom_complet = models.CharField(max_length=255)
    telephone = models.CharField(max_length=32, blank=True)
    numero_permis = models.CharField(max_length=50, blank=True)
    categorie_permis = models.CharField(max_length=20, blank=True)
    permis_expire_le = models.DateField(null=True, blank=True)
    statut = models.CharField(max_length=10, choices=StatutFiche.choices, default=StatutFiche.ACTIF)
    utilisateur = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='fiches_chauffeur'
    )
    cree_le = models.DateTimeField(auto_now_add=True)
    modifie_le = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'chauffeur'

    def __str__(self):
        return self.nom_complet


class StatutVehicule(models.TextChoices):
    ACTIF = 'ACTIF', 'En service'
    MAINTENANCE = 'MAINTENANCE', 'En maintenance'
    INACTIF = 'INACTIF', 'Hors service'
    ARCHIVE = 'ARCHIVE', 'Archive'


class Vehicule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name='vehicules')
    # Normalisee (majuscules, sans espaces ni tirets) cf. serializers.
    immatriculation = models.CharField(max_length=20)
    type = models.CharField(max_length=20, choices=TypeVehicule.choices)
    marque = models.CharField(max_length=100, blank=True)
    modele = models.CharField(max_length=100, blank=True)
    places = models.PositiveSmallIntegerField(null=True, blank=True)
    assurance_expire_le = models.DateField(null=True, blank=True)
    visite_technique_expire_le = models.DateField(null=True, blank=True)
    statut = models.CharField(max_length=15, choices=StatutVehicule.choices, default=StatutVehicule.ACTIF)
    chauffeur = models.ForeignKey(
        Chauffeur, on_delete=models.SET_NULL, null=True, blank=True, related_name='vehicules'
    )
    cree_le = models.DateTimeField(auto_now_add=True)
    modifie_le = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'vehicule'
        constraints = [
            models.UniqueConstraint(
                fields=['organisation', 'immatriculation'], name='vehicule_immatriculation_unique_par_organisation'
            ),
        ]

    def __str__(self):
        return self.immatriculation


class JournalOrganisation(models.Model):
    """Qui a fait quoi, quand, dans une agence -- actions des membres comme
    du staff EasyWay (cf. analyse, pas d'usurpation : tout passe par ici)."""

    id = models.BigAutoField(primary_key=True)
    organisation = models.ForeignKey(Organisation, on_delete=models.CASCADE, related_name='journal')
    acteur = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    action = models.CharField(max_length=50)
    cible_type = models.CharField(max_length=30, blank=True)
    cible_id = models.CharField(max_length=64, blank=True)
    details = models.JSONField(default=dict, blank=True)
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'journal_organisation'
        indexes = [models.Index(fields=['organisation', '-cree_le'])]
