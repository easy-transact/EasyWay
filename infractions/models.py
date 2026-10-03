import uuid

from django.db import models

from places.utils import normaliser


class Infraction(models.Model):
    """Infraction routiere et son amende (liste officielle de la gendarmerie
    camerounaise, ~82 entrees, cf. reunion du 29/09) -- table de reference
    alimentee par le back-office (import CSV/JSON, cf. importation.py), lue
    telle quelle par l'application. Montants en FCFA."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Numero/article de la liste source, s'il existe -- cle d'import prioritaire.
    code = models.CharField(max_length=30, unique=True, null=True, blank=True)
    libelle = models.CharField(max_length=255)
    # Cle d'unicite et de recherche quand `code` est absent (cf. Lieu.nom_normalise).
    libelle_normalise = models.CharField(max_length=255, unique=True, editable=False)
    categorie = models.CharField(max_length=100, blank=True)
    amende_fcfa = models.PositiveIntegerField()
    # Renseigne seulement quand la source donne une fourchette (min-max).
    amende_max_fcfa = models.PositiveIntegerField(null=True, blank=True)
    reference_legale = models.CharField(max_length=255, blank=True)
    # Fourriere, retrait de permis, immobilisation...
    sanctions_complementaires = models.TextField(blank=True)
    # Masque cote application sans supprimer (historique/correction).
    actif = models.BooleanField(default=True)
    cree_le = models.DateTimeField(auto_now_add=True)
    modifie_le = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'infraction'
        ordering = ['categorie', 'libelle']
        indexes = [models.Index(fields=['actif', 'categorie'])]

    def __str__(self):
        return f"{self.libelle} ({self.amende_fcfa} FCFA)"

    def save(self, *args, **kwargs):
        self.libelle_normalise = normaliser(self.libelle)
        if not self.code:
            self.code = None  # '' violerait l'unicite des la 2e infraction sans code
        super().save(*args, **kwargs)
