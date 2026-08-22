import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone


class TypeVehicule(models.TextChoices):
    VOITURE = 'VOITURE', 'Voiture'
    MOTO = 'MOTO', 'Moto'
    TAXI = 'TAXI', 'Taxi'
    CAMION = 'CAMION', 'Camion'
    BUS = 'BUS', 'Bus'
    UTILITAIRE = 'UTILITAIRE', 'Utilitaire'


class Formule(models.TextChoices):
    GRATUITE = 'GRATUITE', 'Gratuite'
    PREMIUM = 'PREMIUM', 'Premium'


class Plateforme(models.TextChoices):
    IOS = 'IOS', 'iOS'
    ANDROID = 'ANDROID', 'Android'


class Unite(models.TextChoices):
    KILOMETRES = 'KM', 'Kilometres'
    MILES = 'MI', 'Miles'


class UtilisateurManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("L'adresse email est obligatoire")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('email_verifie', True)
        return self._create_user(email, password, **extra_fields)


class Utilisateur(AbstractBaseUser, PermissionsMixin):
    """
    Acteurs Visiteur/Conducteur/Conducteur Premium sont tous portes par ce modele :
    un Visiteur est un Utilisateur non authentifie (pas de ligne en base tant qu'il
    n'a pas cree de compte), un Conducteur Premium est un Utilisateur dont `formule`
    vaut PREMIUM. Moderateur/Administrateur s'appuient sur is_staff/is_superuser
    (standard Django) plutot que sur un champ role dedie, aucune classe distincte
    n'existant pour eux dans le diagramme de classes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    email = models.EmailField(unique=True)
    identifiant_google = models.CharField(max_length=255, unique=True, null=True, blank=True)
    nom_complet = models.CharField(max_length=255)
    telephone = models.CharField(max_length=32, null=True, blank=True)
    ville = models.CharField(max_length=255, null=True, blank=True)
    type_vehicule = models.CharField(max_length=20, choices=TypeVehicule.choices, null=True, blank=True)
    url_avatar = models.URLField(null=True, blank=True)
    avatar = models.ImageField(upload_to='avatars/', null=True, blank=True)

    email_verifie = models.BooleanField(default=False)
    mode_invisible = models.BooleanField(default=False)

    formule = models.CharField(max_length=20, choices=Formule.choices, default=Formule.GRATUITE)
    formule_expire_le = models.DateTimeField(null=True, blank=True)

    score_reputation = models.IntegerField(default=100)
    points = models.IntegerField(default=0)

    est_banni = models.BooleanField(default=False)
    banni_jusqu_a = models.DateTimeField(null=True, blank=True)

    cgu_acceptee_le = models.DateTimeField(null=True, blank=True)

    # Suppression logique (RGPD) : conserve le compte 30 jours avant purge
    # definitive par une tache planifiee (hors-perimetre pour l'instant).
    suppression_demandee_le = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = UtilisateurManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['nom_complet']

    class Meta:
        db_table = 'utilisateur'

    def __str__(self):
        return self.email

    def est_premium(self):
        return self.formule == Formule.PREMIUM and (
            self.formule_expire_le is None or self.formule_expire_le > timezone.now()
        )

    def poids_de_vote(self):
        # Poids borne entre 0,2 et 2,0 : evite qu'un compte tres repute
        # ecrase le consensus et qu'un compte peu repute soit sans voix.
        poids = self.score_reputation / 100
        return max(0.2, min(2.0, poids))

    def peut_signaler(self):
        return self.is_active and not self.est_banni

    @property
    def droits(self):
        # Droits est une table de reference par formule (2 lignes), pas une
        # ligne par utilisateur : cf. Droits ci-dessous.
        return Droits.objects.get(formule=self.formule)


class Parametres(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    utilisateur = models.OneToOneField(
        Utilisateur, on_delete=models.CASCADE, related_name='parametres'
    )

    eviter_peages = models.BooleanField(default=False)
    eviter_non_bitumees = models.BooleanField(default=False)
    eviter_intersections_difficiles = models.BooleanField(default=False)
    style_carte = models.CharField(max_length=50, default='STANDARD')

    guidage_vocal_actif = models.BooleanField(default=True)
    langue_vocale = models.CharField(max_length=10, default='fr')
    unites = models.CharField(max_length=5, choices=Unite.choices, default=Unite.KILOMETRES)
    compteur_vitesse_actif = models.BooleanField(default=False)
    alerte_vitesse_active = models.BooleanField(default=True)

    notifications_globales = models.BooleanField(default=True)
    notif_annonces = models.BooleanField(default=True)
    notif_incidents_frequents = models.BooleanField(default=True)
    notif_alertes_police = models.BooleanField(default=True)
    notif_changement_itineraire = models.BooleanField(default=True)
    notif_nouveautes = models.BooleanField(default=True)

    class Meta:
        db_table = 'parametres'

    def __str__(self):
        return f"Parametres de {self.utilisateur_id}"


class Appareil(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    utilisateur = models.ForeignKey(
        Utilisateur, on_delete=models.CASCADE, related_name='appareils'
    )

    jeton_push = models.CharField(max_length=255)
    plateforme = models.CharField(max_length=10, choices=Plateforme.choices)
    version_application = models.CharField(max_length=20)
    version_systeme = models.CharField(max_length=20)
    langue = models.CharField(max_length=10, default='fr')
    vu_le = models.DateTimeField(auto_now=True)
    est_actif = models.BooleanField(default=True)

    class Meta:
        db_table = 'appareil'
        indexes = [
            models.Index(fields=['jeton_push']),
            models.Index(fields=['utilisateur', 'est_actif']),
        ]

    def __str__(self):
        return f"{self.plateforme} - {self.utilisateur_id}"


class Droits(models.Model):
    """
    Une ligne par Formule (GRATUITE, PREMIUM), pas une ligne par utilisateur :
    l'entitlement est une propriete du plan, pas du compte. Faire evoluer une
    limite (ex. 5 -> 10 adresses enregistrees pour la formule gratuite) reste
    ainsi une seule UPDATE au lieu d'une migration sur toute la table utilisateur.
    Voir Utilisateur.droits pour la resolution formule -> ligne.
    """

    formule = models.CharField(max_length=20, choices=Formule.choices, primary_key=True)
    max_adresses_enregistrees = models.IntegerField(null=True, blank=True, help_text='null = illimite')
    publicite_active = models.BooleanField(default=True)
    routage_avance = models.BooleanField(default=False)
    packs_hors_ligne = models.BooleanField(default=False)
    retention_historique_jours = models.IntegerField(default=30)

    class Meta:
        db_table = 'droits'
        verbose_name_plural = 'droits'

    def __str__(self):
        return f"Droits {self.formule}"

    def autorise(self, fonction: str) -> bool:
        return {
            'adresse_illimitee': self.max_adresses_enregistrees is None,
            'routage_avance': self.routage_avance,
            'packs_hors_ligne': self.packs_hors_ligne,
        }.get(fonction, False)
