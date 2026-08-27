import uuid

from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.contrib.gis.geos import Point
from django.contrib.postgres.indexes import GinIndex
from django.db import models


class TypeOsm(models.TextChoices):
    """Primitive OpenStreetMap referencee par identifiantOsm (non enumeree dans le
    document source ; deduite du schema d'identifiants d'OSM : node/way/relation)."""

    NODE = 'NODE', 'Node'
    WAY = 'WAY', 'Way'
    RELATION = 'RELATION', 'Relation'


class SourceLieu(models.TextChoices):
    """Non enumeree explicitement dans le document ; deduite des cas d'utilisation
    'Proposer un lieu manquant' (UTILISATEUR) vs import OpenStreetMap (OPENSTREETMAP)."""

    OPENSTREETMAP = 'OPENSTREETMAP', 'OpenStreetMap'
    UTILISATEUR = 'UTILISATEUR', 'Soumission utilisateur'


class StatutLieu(models.TextChoices):
    """Non enumeree explicitement dans le document ; deduite des methodes
    approuver()/rejeter()/fusionnerAvec() et des cas d'utilisation de moderation."""

    EN_ATTENTE = 'EN_ATTENTE', 'En attente'
    APPROUVE = 'APPROUVE', 'Approuve'
    REJETE = 'REJETE', 'Rejete'
    FUSIONNE = 'FUSIONNE', 'Fusionne'


class LibelleAdresse(models.TextChoices):
    DOMICILE = 'DOMICILE', 'Domicile'
    TRAVAIL = 'TRAVAIL', 'Travail'
    PERSONNALISE = 'PERSONNALISE', 'Personnalise'


class Lieu(models.Model):
    """Objet public partage, potentiellement reference par plusieurs
    AdresseEnregistree ou Trajet sans en dependre (cf. section 3.3)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    type_osm = models.CharField(max_length=10, choices=TypeOsm.choices, null=True, blank=True)
    identifiant_osm = models.BigIntegerField(null=True, blank=True)

    nom = models.CharField(max_length=255)
    nom_normalise = models.CharField(max_length=255, db_index=True)
    categorie = models.CharField(max_length=100)
    adresse = models.CharField(max_length=500, blank=True)
    quartier = models.CharField(max_length=255, null=True, blank=True)
    ville = models.CharField(max_length=255)
    position = gis_models.PointField(srid=4326, geography=True)

    source = models.CharField(max_length=20, choices=SourceLieu.choices)
    statut = models.CharField(max_length=20, choices=StatutLieu.choices, default=StatutLieu.EN_ATTENTE)
    score_popularite = models.IntegerField(default=0)

    fusionne_dans = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='lieux_fusionnes'
    )

    # Trace du signalement UTILISATEUR (UC "Proposer un lieu manquant") pour la
    # file de moderation ; absent des imports OPENSTREETMAP.
    propose_par = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='lieux_proposes',
    )

    class Meta:
        db_table = 'lieu'
        indexes = [
            models.Index(fields=['ville', 'categorie']),
            gis_models.Index(fields=['position']),
            GinIndex(fields=['nom_normalise'], name='lieu_nom_normalise_trgm', opclasses=['gin_trgm_ops']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['type_osm', 'identifiant_osm'],
                name='uniq_lieu_osm',
                condition=models.Q(identifiant_osm__isnull=False),
            ),
        ]

    def __str__(self):
        return self.nom

    def approuver(self):
        self.statut = StatutLieu.APPROUVE
        self.save(update_fields=['statut'])

    def rejeter(self, motif: str):
        self.statut = StatutLieu.REJETE
        self.save(update_fields=['statut'])

    def fusionner_avec(self, lieu: 'Lieu'):
        self.statut = StatutLieu.FUSIONNE
        self.fusionne_dans = lieu
        self.save(update_fields=['statut', 'fusionne_dans'])

    def incrementer_popularite(self):
        self.score_popularite = models.F('score_popularite') + 1
        self.save(update_fields=['score_popularite'])

    def distance_depuis(self, point: Point) -> int:
        from django.contrib.gis.db.models.functions import Distance

        return Lieu.objects.filter(pk=self.pk).annotate(
            distance=Distance('position', point)
        ).first().distance.m


class AdresseEnregistree(models.Model):
    """Favori prive d'un utilisateur ; peut ou non referencer un Lieu public."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    utilisateur = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='adresses_enregistrees'
    )
    lieu = models.ForeignKey(
        Lieu, on_delete=models.SET_NULL, null=True, blank=True, related_name='adresses_enregistrees'
    )

    libelle = models.CharField(max_length=20, choices=LibelleAdresse.choices)
    nom_personnalise = models.CharField(max_length=255, null=True, blank=True)
    adresse = models.CharField(max_length=500)
    position = gis_models.PointField(srid=4326, geography=True)

    class Meta:
        db_table = 'adresse_enregistree'
        constraints = [
            models.UniqueConstraint(
                fields=['utilisateur', 'libelle'],
                condition=models.Q(libelle__in=[LibelleAdresse.DOMICILE, LibelleAdresse.TRAVAIL]),
                name='uniq_domicile_travail_par_utilisateur',
            ),
        ]

    def __str__(self):
        return self.nom_personnalise or self.libelle

    def est_domicile(self) -> bool:
        return self.libelle == LibelleAdresse.DOMICILE

    def est_travail(self) -> bool:
        return self.libelle == LibelleAdresse.TRAVAIL

    def renommer(self, nom: str):
        self.nom_personnalise = nom
        self.save(update_fields=['nom_personnalise'])


class RechercheRecente(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    utilisateur = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='recherches_recentes'
    )

    libelle = models.CharField(max_length=255)
    sous_libelle = models.CharField(max_length=255, null=True, blank=True)
    position = gis_models.PointField(srid=4326, geography=True)
    recherche_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'recherche_recente'
        ordering = ['-recherche_le']
        indexes = [
            models.Index(fields=['utilisateur', '-recherche_le']),
        ]

    def __str__(self):
        return self.libelle


class Ville(models.Model):
    """Referentiel de villes/villages du Cameroun, importe depuis la couche
    gis_osm_places_free d'un export GeoPackage Geofabrik (cf. management
    command import_villes_gpkg) -- cette couche pre-agregee n'existe pas dans
    l'extrait .osm.pbf utilise par seed_places/Valhalla, d'ou une source et
    un import distincts. Ne remplace pas Lieu.ville (texte libre par lieu,
    issu de Nominatim/OSM) : sert de liste canonique pour normaliser/valider
    des recherches par ville (ex. GET /api/incidents/city/)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    osm_id = models.BigIntegerField(unique=True)

    nom = models.CharField(max_length=255)
    nom_normalise = models.CharField(max_length=255, db_index=True)
    # Valeurs Geofabrik telles quelles (city/town/village/hamlet/locality/
    # suburb/national_capital/...) -- vocabulaire externe, pas un choix de
    # domaine controle par cette appli.
    type = models.CharField(max_length=30)
    population = models.IntegerField(default=0)
    position = gis_models.PointField(srid=4326, geography=True)

    class Meta:
        db_table = 'ville'
        indexes = [
            gis_models.Index(fields=['position']),
            GinIndex(fields=['nom_normalise'], name='ville_nom_normalise_trgm', opclasses=['gin_trgm_ops']),
        ]

    def __str__(self):
        return self.nom
