"""
Etat des routes (spec ETAT_DES_ROUTES du 29/09, V1 : signalements + vitesses).

Troncon : une arete du graphe Valhalla decoupee en tranches de TAILLE_TRONCON_M
(identifiant "{arete}:{index}"). Les aretes changent quand le graphe est
regenere sur une nouvelle coupe OSM : les notes se recalculent alors depuis
les observations brutes (PassageTroncon, SignalementTroncon), gardees pour ca.
"""

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import models

TAILLE_TRONCON_M = 100


class ClasseEtat(models.TextChoices):
    BON = 'BON', 'Bon'
    DEGRADE = 'DEGRADE', 'Degrade'
    MAUVAIS = 'MAUVAIS', 'Mauvais'
    INCONNU = 'INCONNU', 'Inconnu'


class Troncon(models.Model):
    id = models.CharField(primary_key=True, max_length=40, help_text='"{arete Valhalla}:{index de tranche}"')
    arete = models.BigIntegerField(db_index=True)
    index = models.PositiveIntegerField()
    way_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    classe_route = models.CharField(max_length=20, blank=True, help_text='road_class Valhalla (primary, residential...)')
    nom_voie = models.CharField(max_length=255, blank=True)
    # Geometrie connue de la tranche : entre les deux points les plus eloignes
    # observes (debut_m/fin_m = distance depuis le debut de l'arete), etendue
    # a mesure que d'autres passages couvrent davantage de la tranche.
    geometrie = gis_models.LineStringField(srid=4326)
    debut_m = models.FloatField()
    fin_m = models.FloatField()
    cellule_h3_res8 = models.BigIntegerField(db_index=True)
    # Rond-point, peage, feu : on y ralentit quelle que soit la chaussee --
    # exclu de la penalite de vitesse.
    ralentissement_normal = models.BooleanField(default=False)
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'troncon'

    def __str__(self):
        return self.id

    @property
    def longueur_m(self):
        return max(self.fin_m - self.debut_m, 0)


class PassageTroncon(models.Model):
    """Un trajet sur une tranche : vitesse moyenne mesuree. Conducteur
    anonymise (empreinte), jamais l'identifiant ; garde RETENTION_PASSAGES_JOURS."""

    troncon = models.ForeignKey(Troncon, on_delete=models.CASCADE, related_name='passages')
    conducteur = models.CharField(max_length=16)
    trajet_id = models.UUIDField()
    type_vehicule = models.CharField(max_length=20, blank=True)
    vitesse_kmh = models.FloatField()
    horodatage = models.DateTimeField()
    heure_creuse = models.BooleanField()

    class Meta:
        db_table = 'passage_troncon'
        indexes = [models.Index(fields=['troncon', 'horodatage']), models.Index(fields=['horodatage'])]


class SignalementTroncon(models.Model):
    """Rattachement d'un signalement a sa tranche (recalage Valhalla de sa
    position et de son cap). troncon vide : recalage impossible (hors route)."""

    incident = models.OneToOneField('community.Incident', on_delete=models.CASCADE, related_name='troncon_route')
    troncon = models.ForeignKey(Troncon, on_delete=models.SET_NULL, null=True, blank=True, related_name='signalements')
    rattache_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'signalement_troncon'


class NoteTroncon(models.Model):
    troncon = models.OneToOneField(Troncon, on_delete=models.CASCADE, primary_key=True, related_name='note')
    note = models.PositiveSmallIntegerField(null=True, blank=True)
    classe = models.CharField(max_length=10, choices=ClasseEtat.choices, default=ClasseEtat.INCONNU, db_index=True)
    confiance = models.FloatField(default=0)
    causes = models.JSONField(default=list)
    inondable = models.BooleanField(default=False, db_index=True)
    penalite_signalements = models.FloatField(default=0)
    penalite_vitesse = models.FloatField(default=0)
    nb_passages = models.PositiveIntegerField(default=0)
    nb_conducteurs = models.PositiveIntegerField(default=0)
    nb_signalements_confirmes = models.PositiveIntegerField(default=0)
    derniere_observation = models.DateTimeField(null=True, blank=True)
    mis_a_jour_le = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'note_troncon'
        permissions = [('exporter_classement', "Exporter le classement de l'etat des routes (autorites)")]


class HistoriqueNote(models.Model):
    """Une ligne par tronçon et par semaine (lundi), pour la tendance sur 90 jours."""

    troncon = models.ForeignKey(Troncon, on_delete=models.CASCADE, related_name='historique')
    semaine = models.DateField()
    note = models.PositiveSmallIntegerField(null=True, blank=True)
    classe = models.CharField(max_length=10, choices=ClasseEtat.choices)

    class Meta:
        db_table = 'historique_note_troncon'
        constraints = [models.UniqueConstraint(fields=['troncon', 'semaine'], name='historique_note_unique_semaine')]


class ReglagesEtatRoutes(models.Model):
    """Valeurs de depart de la spec (poids, demi-vies, seuils), modifiables
    depuis le back-office sans redeploiement. Une seule ligne ; les cles
    absentes prennent la valeur de services/reglages.py:DEFAUTS."""

    valeurs = models.JSONField(default=dict)
    modifie_le = models.DateTimeField(auto_now=True)
    modifie_par = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        db_table = 'reglages_etat_routes'
