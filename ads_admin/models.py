import uuid

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils import timezone


class Emplacement(models.TextChoices):
    BANNIERE_RECHERCHE = 'BANNIERE_RECHERCHE', 'Banniere de recherche'
    RESUME_ITINERAIRE = 'RESUME_ITINERAIRE', "Resume d'itineraire"
    CARTE_ARRIVEE = 'CARTE_ARRIVEE', "Carte d'arrivee"
    EPINGLE_CARTE = 'EPINGLE_CARTE', 'Epingle carte'


class CampagnePublicitaire(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    nom = models.CharField(max_length=255)
    annonceur = models.CharField(max_length=255)
    url_creation = models.URLField()
    url_cible = models.URLField()
    emplacement = models.CharField(max_length=20, choices=Emplacement.choices)

    # Ciblage par ville OU par zone (cas d'utilisation "Cibler par ville ou par
    # zone", relation «extend») : les deux champs sont optionnels, au moins
    # un des deux est renseigne selon le mode de ciblage choisi.
    villes_ciblees = ArrayField(models.CharField(max_length=255), blank=True, default=list)
    zone_ciblee = gis_models.PolygonField(srid=4326, geography=True, null=True, blank=True)

    debute_le = models.DateTimeField()
    termine_le = models.DateTimeField()
    plafond_journalier = models.IntegerField()
    est_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'campagne_publicitaire'
        indexes = [
            models.Index(fields=['est_active', 'debute_le', 'termine_le']),
        ]

    def __str__(self):
        return self.nom

    def est_diffusable(self, utilisateur) -> bool:
        maintenant = timezone.now()
        if not self.est_active or not (self.debute_le <= maintenant <= self.termine_le):
            return False
        if hasattr(utilisateur, 'droits') and not utilisateur.droits.publicite_active:
            return True
        return not getattr(utilisateur, 'est_premium', lambda: False)()

    def plafond_atteint(self) -> bool:
        aujourdhui = timezone.now().date()
        impressions_du_jour = self.impressions.filter(survenue_le__date=aujourdhui).count()
        return impressions_du_jour >= self.plafond_journalier


class Impression(models.Model):
    id = models.BigAutoField(primary_key=True)
    campagne = models.ForeignKey(CampagnePublicitaire, on_delete=models.CASCADE, related_name='impressions')

    evenement = models.CharField(max_length=50, help_text="TypeEvenement : valeurs non enumerees dans le document source")
    survenue_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'impression'
        indexes = [
            models.Index(fields=['campagne', 'survenue_le']),
        ]

    def __str__(self):
        return f"{self.evenement} - {self.campagne_id}"


class EntreeAudit(models.Model):
    id = models.BigAutoField(primary_key=True)

    # Non representee sur la Fig. 9, mais indispensable a un journal d'audit :
    # nullable pour couvrir les actions declenchees par l'Ordonnanceur (systeme).
    acteur = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='entrees_audit',
    )

    action = models.CharField(max_length=255)
    type_cible = models.CharField(max_length=100)
    identifiant_cible = models.UUIDField()
    valeur_precedente = models.JSONField(null=True, blank=True)
    valeur_nouvelle = models.JSONField(null=True, blank=True)
    survenue_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'entree_audit'
        indexes = [
            models.Index(fields=['type_cible', 'identifiant_cible']),
            models.Index(fields=['-survenue_le']),
        ]

    def __str__(self):
        return f"{self.action} - {self.type_cible}:{self.identifiant_cible}"


class TypeNotification(models.TextChoices):
    LIEU = 'LIEU', 'Lieu propose'
    INCIDENT = 'INCIDENT', 'Incident conteste'
    ABUS = 'ABUS', 'Comportement suspect'
    ATTENTE = 'ATTENTE', "Liste d'attente"
    IMPORT = 'IMPORT', 'Import'
    PUB = 'PUB', 'Publicite'


class NiveauNotification(models.TextChoices):
    INFO = 'INFO', 'Info'
    SUCCES = 'SUCCES', 'Succes'
    ATTENTION = 'ATTENTION', 'Attention'
    DANGER = 'DANGER', 'Danger'


class NotificationStaff(models.Model):
    """Notification du back-office, partagee par toute l'equipe staff (pas une
    ligne par destinataire) : lue_par trace qui l'a deja vue. Les evenements
    frequents (lieux proposes, inscriptions) sont regroupes sur une meme ligne
    via cle_regroupement plutot que d'inonder la cloche, cf. services.notifier()."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=20, choices=TypeNotification.choices)
    niveau = models.CharField(max_length=20, choices=NiveauNotification.choices, default=NiveauNotification.INFO)
    titre = models.CharField(max_length=255)
    texte = models.CharField(max_length=500, blank=True)
    # Chemin du back-office (ex. "/places?statut=EN_ATTENTE"), pas une URL absolue.
    lien = models.CharField(max_length=255, blank=True)
    cle_regroupement = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    compteur = models.PositiveIntegerField(default=1)
    cree_le = models.DateTimeField(auto_now_add=True)
    mis_a_jour_le = models.DateTimeField(default=timezone.now)
    lue_par = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='notifications_staff_lues')

    class Meta:
        db_table = 'notification_staff'
        indexes = [models.Index(fields=['-mis_a_jour_le'])]

    def __str__(self):
        return self.titre


class PreferencesNotificationStaff(models.Model):
    utilisateur = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, primary_key=True,
        related_name='preferences_notifications_staff',
    )
    types_desactives = ArrayField(models.CharField(max_length=20), blank=True, default=list)
    navigateur = models.BooleanField(default=False)
    son = models.BooleanField(default=False)

    class Meta:
        db_table = 'preferences_notification_staff'
