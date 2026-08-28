import uuid
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import models
from django.utils import timezone

from places.models import Lieu

from .exceptions import TransitionInvalide

# PositionGps est un objet «transitoire» (section 3.3) : il ne franchit jamais la
# couche de persistance durable. Les positions brutes vivent uniquement dans
# Redis/le flux de traitement (Fig. 14) ; seul leur agregat de 5 minutes,
# EchantillonVitesse, est ecrit en base. Aucun modele Django n'est donc cree
# pour PositionGps.


class StatutTrajet(models.TextChoices):
    PLANIFIE = 'PLANIFIE', 'Planifie'
    ACTIF = 'ACTIF', 'Actif'
    TERMINE = 'TERMINE', 'Termine'
    ANNULE = 'ANNULE', 'Annule'


class NiveauTrafic(models.TextChoices):
    NORMAL = 'NORMAL', 'Normal'
    MODERE = 'MODERE', 'Modere'
    DENSE = 'DENSE', 'Dense'


class Trajet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    utilisateur = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='trajets'
    )
    lieu_destination = models.ForeignKey(
        Lieu, on_delete=models.SET_NULL, null=True, blank=True, related_name='trajets'
    )

    position_origine = gis_models.PointField(srid=4326, geography=True)
    libelle_origine = models.CharField(max_length=500)
    position_destination = gis_models.PointField(srid=4326, geography=True)
    libelle_destination = models.CharField(max_length=500)

    itineraire_choisi = models.CharField(max_length=64, help_text="identifiant de l'Itineraire retenu")

    distance_prevue = models.IntegerField(help_text='metres')
    duree_prevue = models.IntegerField(help_text='secondes')
    distance_reelle = models.IntegerField(null=True, blank=True, help_text='metres')
    duree_reelle = models.IntegerField(null=True, blank=True, help_text='secondes')
    geometrie = gis_models.LineStringField(srid=4326, geography=True, null=True, blank=True)

    statut = models.CharField(max_length=20, choices=StatutTrajet.choices, default=StatutTrajet.PLANIFIE)
    incidents_evites = models.IntegerField(default=0)

    note = models.PositiveSmallIntegerField(null=True, blank=True)
    commentaire = models.TextField(null=True, blank=True)

    demarre_le = models.DateTimeField(null=True, blank=True)
    termine_le = models.DateTimeField(null=True, blank=True)

    # Machine a etats (diagramme d'etats du document UML) : source de verite
    # unique pour les transitions autorisees, appliquee par changer_statut()
    # -- ni le PATCH ni les methodes demarrer/terminer/annuler ne l'esquivent.
    TRANSITIONS_AUTORISEES = {
        StatutTrajet.PLANIFIE: {StatutTrajet.ACTIF, StatutTrajet.ANNULE},
        StatutTrajet.ACTIF: {StatutTrajet.TERMINE, StatutTrajet.ANNULE},
        StatutTrajet.TERMINE: set(),
        StatutTrajet.ANNULE: set(),
    }

    class Meta:
        db_table = 'trajet'
        indexes = [
            models.Index(fields=['utilisateur', 'statut']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(note__isnull=True) | models.Q(note__gte=1, note__lte=5),
                name='note_trajet_entre_1_et_5',
            ),
        ]

    def __str__(self):
        return f"{self.libelle_origine} -> {self.libelle_destination}"

    def changer_statut(self, nouveau_statut: str):
        if nouveau_statut == self.statut:
            return
        if nouveau_statut not in self.TRANSITIONS_AUTORISEES.get(self.statut, set()):
            raise TransitionInvalide(f"{self.statut} -> {nouveau_statut} n'est pas une transition autorisee.")

        champs = ['statut']
        self.statut = nouveau_statut
        if nouveau_statut == StatutTrajet.ACTIF:
            self.demarre_le = timezone.now()
            champs.append('demarre_le')
        elif nouveau_statut == StatutTrajet.TERMINE:
            self.termine_le = timezone.now()
            champs.append('termine_le')
        self.save(update_fields=champs)

    def demarrer(self):
        self.changer_statut(StatutTrajet.ACTIF)

    def terminer(self):
        self.changer_statut(StatutTrajet.TERMINE)

    def annuler(self):
        self.changer_statut(StatutTrajet.ANNULE)

    def noter(self, note: int, commentaire: str = ''):
        self.note = note
        self.commentaire = commentaire
        self.save(update_fields=['note', 'commentaire'])

    def ecart_duree_pourcent(self):
        if not self.duree_reelle or not self.duree_prevue:
            return None
        return round((self.duree_reelle - self.duree_prevue) / self.duree_prevue * 100, 1)

    def purger_geometrie(self):
        self.geometrie = None
        self.save(update_fields=['geometrie'])


class Itineraire(models.Model):
    """Objet-valeur (section 3.3) : n'a pas d'existence propre en dehors du Trajet
    qui le porte. Toujours accede via trajet.itineraires, jamais consulte isolement."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trajet = models.ForeignKey(Trajet, on_delete=models.CASCADE, related_name='itineraires')

    identifiant = models.CharField(max_length=64, help_text='cle correlee a Trajet.itineraire_choisi')
    est_recommande = models.BooleanField(default=False)
    libelle = models.CharField(max_length=255)
    distance = models.IntegerField(help_text='metres')
    duree = models.IntegerField(help_text='secondes')
    duree_avec_trafic = models.IntegerField(null=True, blank=True, help_text='secondes')
    niveau_trafic = models.CharField(max_length=10, choices=NiveauTrafic.choices, default=NiveauTrafic.NORMAL)
    geometrie = models.TextField(help_text='polyligne encodee, telle que retournee par Valhalla')

    class Meta:
        db_table = 'itineraire'
        constraints = [
            models.UniqueConstraint(fields=['trajet', 'identifiant'], name='uniq_itineraire_par_trajet'),
        ]

    def __str__(self):
        return self.libelle

    def nombre_manoeuvres(self) -> int:
        return self.manoeuvres.count()


class Manoeuvre(models.Model):
    """Objet-valeur (section 3.3), portee par un Itineraire lui-meme deja
    non-persistant hors de son Trajet."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    itineraire = models.ForeignKey(Itineraire, on_delete=models.CASCADE, related_name='manoeuvres')

    ordre = models.PositiveIntegerField(help_text='position dans la sequence de guidage (non documente, ajoute par necessite)')
    type = models.CharField(max_length=50, help_text="TypeManoeuvre : valeurs non enumerees dans le document source")
    instruction = models.CharField(max_length=500)
    instruction_vocale = models.CharField(max_length=500)
    distance = models.IntegerField(help_text='metres')
    duree = models.IntegerField(help_text='secondes')
    nom_voie = models.CharField(max_length=255, blank=True)
    road_class = models.CharField(max_length=50, blank=True)

    class Meta:
        db_table = 'manoeuvre'
        ordering = ['itineraire', 'ordre']
        constraints = [
            models.UniqueConstraint(fields=['itineraire', 'ordre'], name='uniq_manoeuvre_ordre_par_itineraire'),
        ]

    def __str__(self):
        return f"{self.ordre}. {self.instruction}"


FUSEAU_TRAFIC = ZoneInfo('Africa/Douala')  # toutes les VILLES_DISPONIBLES sont dans ce fuseau unique, sans heure d'ete --
# jour_semaine/heure_jour doivent etre derives en heure locale, pas UTC (TIME_ZONE du
# projet), sans quoi l'heure de pointe serait systematiquement decalee d'une heure.


class EchantillonVitesse(models.Model):
    """Agregat persistant de trafic (5 minutes) ; seule trace durable des positions
    GPS transitoires consommees par ConsommateurPositions (Fig. 11 et 14)."""

    id = models.BigAutoField(primary_key=True)

    identifiant_arete = models.BigIntegerField(help_text='identifiant de segment sur le graphe routier')
    debut_intervalle = models.DateTimeField()
    jour_semaine = models.PositiveSmallIntegerField()
    heure_jour = models.PositiveSmallIntegerField()
    vitesse_moyenne = models.DecimalField(max_digits=6, decimal_places=2, help_text='km/h')
    nombre_echantillons = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'echantillon_vitesse'
        constraints = [
            models.UniqueConstraint(
                fields=['identifiant_arete', 'debut_intervalle'], name='uniq_echantillon_par_arete_intervalle'
            ),
        ]
        indexes = [
            models.Index(fields=['identifiant_arete', '-debut_intervalle']),
        ]

    def __str__(self):
        return f"Arete {self.identifiant_arete} @ {self.debut_intervalle}"

    # Pas de niveau_congestion() ici base sur un seuil absolu (ex. >=50km/h) --
    # 25 km/h est rapide en centre-ville et lent sur une penetrante. Le niveau
    # se calcule en comparant une arete a son propre historique au meme jour/
    # heure (cf. services/service_trafic.py:niveau_relatif), jamais dans l'absolu.
