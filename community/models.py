import uuid
from decimal import Decimal

import h3
from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.db import models
from django.utils import timezone

RESOLUTION_H3_FIN = 8  # ~460m de cote -- rayon de recherche des doublons (150m),
# et depuis le passage de /proches/ en resolution 8, fenetre de requete/cache
# de cet endpoint aussi (beneficie au passage de l'index cellule_h3_res8+statut).
RESOLUTION_H3_LARGE = 7  # ~1.2km de cote -- conserve sur Incident mais non
# consomme par un endpoint actuellement ; reserve a un usage futur plus
# grossier (ex. agregation carte / panning ville).


class TypeIncident(models.TextChoices):
    DANGER = 'DANGER', 'Danger'
    POLICE = 'POLICE', 'Police'
    EMBOUTEILLAGE = 'EMBOUTEILLAGE', 'Embouteillage'
    ACCIDENT = 'ACCIDENT', 'Accident'
    ROUTE_BARREE = 'ROUTE_BARREE', 'Route barree'
    VOIE_BLOQUEE = 'VOIE_BLOQUEE', 'Voie bloquee'
    MAUVAIS_TEMPS = 'MAUVAIS_TEMPS', 'Mauvais temps'
    RADAR = 'RADAR', 'Radar'


class SousTypeIncident(models.TextChoices):
    DANGER = 'DANGER', 'Danger'
    TRAVAUX = 'TRAVAUX', 'Travaux'
    VEHICULE_ARRETE = 'VEHICULE_ARRETE', 'Vehicule arrete'
    FEU_TRICOLORE_HORS_SERVICE = 'FEU_TRICOLORE_HORS_SERVICE', 'Feu tricolore hors service'
    NID_DE_POULE = 'NID_DE_POULE', 'Nid-de-poule'
    OBJET = 'OBJET', 'Objet'

    POLICE = 'POLICE', 'Police'
    POLICE_MUNICIPALE = 'POLICE_MUNICIPALE', 'Police municipale'
    RADAR_MOBILE = 'RADAR_MOBILE', 'Radar mobile'
    CACHEE = 'CACHEE', 'Cachee'
    SENS_INVERSE = 'SENS_INVERSE', 'Sens inverse'

    EMBOUTEILLAGE = 'EMBOUTEILLAGE', 'Embouteillage'
    IMPORTANT = 'IMPORTANT', 'Important'
    A_L_ARRET = 'A_L_ARRET', "A l'arret"

    ACCIDENT = 'ACCIDENT', 'Accident'
    CARAMBOLAGE = 'CARAMBOLAGE', 'Carambolage'

    VOIE_BLOQUEE = 'VOIE_BLOQUEE', 'Voie bloquee'
    VOIE_DE_GAUCHE = 'VOIE_DE_GAUCHE', 'Voie de gauche'
    VOIE_DE_DROITE = 'VOIE_DE_DROITE', 'Voie de droite'
    VOIE_DU_MILIEU = 'VOIE_DU_MILIEU', 'Voie du milieu'

    UNE_VOIE_REDUITE = '1_VOIE_REDUITE', '1 voie reduite'
    ROUTE_BARREE = 'ROUTE_BARREE', 'Route barree'

    MAUVAIS_TEMPS = 'MAUVAIS_TEMPS', 'Mauvais temps'
    ROUTE_GLISSANTE = 'ROUTE_GLISSANTE', 'Route glissante'
    INONDATION = 'INONDATION', 'Inondation'
    BROUILLARD = 'BROUILLARD', 'Brouillard'

    RADAR = 'RADAR', 'Radar'


SOUS_TYPES_PAR_TYPE = {
    TypeIncident.DANGER: [
        SousTypeIncident.DANGER, SousTypeIncident.TRAVAUX, SousTypeIncident.VEHICULE_ARRETE,
        SousTypeIncident.FEU_TRICOLORE_HORS_SERVICE, SousTypeIncident.NID_DE_POULE, SousTypeIncident.OBJET
    ],
    TypeIncident.POLICE: [
        SousTypeIncident.POLICE, SousTypeIncident.POLICE_MUNICIPALE, SousTypeIncident.RADAR_MOBILE,
        SousTypeIncident.CACHEE, SousTypeIncident.SENS_INVERSE
    ],
    TypeIncident.EMBOUTEILLAGE: [
        SousTypeIncident.EMBOUTEILLAGE, SousTypeIncident.IMPORTANT, SousTypeIncident.A_L_ARRET
    ],
    TypeIncident.ACCIDENT: [
        SousTypeIncident.ACCIDENT, SousTypeIncident.CARAMBOLAGE, SousTypeIncident.SENS_INVERSE
    ],
    TypeIncident.ROUTE_BARREE: [
        SousTypeIncident.VOIE_BLOQUEE, SousTypeIncident.VOIE_DE_GAUCHE,
        SousTypeIncident.VOIE_DE_DROITE, SousTypeIncident.VOIE_DU_MILIEU
    ],
    TypeIncident.VOIE_BLOQUEE: [
        SousTypeIncident.UNE_VOIE_REDUITE, SousTypeIncident.ROUTE_BARREE
    ],
    TypeIncident.MAUVAIS_TEMPS: [
        SousTypeIncident.MAUVAIS_TEMPS, SousTypeIncident.ROUTE_GLISSANTE,
        SousTypeIncident.INONDATION, SousTypeIncident.BROUILLARD
    ],
    TypeIncident.RADAR: [
        SousTypeIncident.RADAR
    ],
}


class StatutIncident(models.TextChoices):
    # EN_ATTENTE ajoute par deduction du cycle de vie decrit en section 5.1
    # (le diagramme de classes, Fig. 9, ne liste que ACTIF/EXPIRE/RETIRE/FUSIONNE).
    EN_ATTENTE = 'EN_ATTENTE', 'En attente'
    ACTIF = 'ACTIF', 'Actif'
    EXPIRE = 'EXPIRE', 'Expire'
    RETIRE = 'RETIRE', 'Retire'
    FUSIONNE = 'FUSIONNE', 'Fusionne'


class SensVote(models.TextChoices):
    CONFIRMATION = 'CONFIRMATION', 'Confirmation'
    INFIRMATION = 'INFIRMATION', 'Infirmation'


DUREE_VIE_BASE_PAR_TYPE = {
    TypeIncident.DANGER: 60,
    TypeIncident.POLICE: 60,
    TypeIncident.EMBOUTEILLAGE: 30,
    TypeIncident.ACCIDENT: 90,
    TypeIncident.ROUTE_BARREE: 240,
    TypeIncident.VOIE_BLOQUEE: 60,
    TypeIncident.MAUVAIS_TEMPS: 120,
    TypeIncident.RADAR: 480,
}

# Seuil de score_confiance (somme des poids de vote, cf. Utilisateur.poids_de_vote)
# a atteindre pour qu'un signalement EN_ATTENTE passe ACTIF et devienne visible
# de tous. Equivaut a ~3 confirmations d'utilisateurs de reputation neutre (poids 1.0).
SEUIL_CONFIANCE_VALIDATION = Decimal('3')
# Un auteur suffisamment repute a moins besoin d'etre corrobore : ses
# signalements suivants ne demandent plus que ~2 confirmations neutres.
SEUIL_CONFIANCE_VALIDATION_REDUITE = Decimal('2')
SEUIL_REPUTATION_PALIER_REDUCTION = Decimal('3')
# Gain de reputation de l'auteur a chaque signalement qui atteint le seuil
# de validation ci-dessus (un "bon signalement").
POINTS_REPUTATION_PAR_VALIDATION = Decimal('0.5')


class Incident(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Association non representee explicitement sur la Fig. 9 mais necessaire :
    # UC-03 stipule que le systeme "credite l'auteur" du signalement.
    auteur = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='incidents_signales'
    )

    type = models.CharField(max_length=20, choices=TypeIncident.choices)
    sous_type = models.CharField(max_length=100, null=True, blank=True)
    position = gis_models.PointField(srid=4326, geography=True)
    # Calcules dans save() depuis `position` -- jamais fournis par l'appelant.
    cellule_h3_res8 = models.BigIntegerField(editable=False)
    cellule_h3_res7 = models.BigIntegerField(editable=False)
    cap = models.IntegerField(null=True, blank=True, help_text='degres, 0-359')
    nom_voie = models.CharField(max_length=255, blank=True)
    ville = models.CharField(max_length=255, blank=True)
    # Minuscules/sans accents (places.utils.normaliser) -- meme principe que
    # Lieu.nom_normalise : permet un filtre par ville insensible a la casse
    # et aux accents sans extension Postgres (unaccent) supplementaire.
    ville_normalisee = models.CharField(max_length=255, blank=True)

    confirmations = models.IntegerField(default=0)
    infirmations = models.IntegerField(default=0)
    score_confiance = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    statut = models.CharField(max_length=20, choices=StatutIncident.choices, default=StatutIncident.ACTIF)
    severite = models.IntegerField(default=1)
    expire_le = models.DateTimeField()
    motif_retrait = models.CharField(max_length=255, null=True, blank=True)

    fusionne_dans = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='incidents_fusionnes'
    )

    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'incident'
        indexes = [
            models.Index(fields=['cellule_h3_res8', 'statut']),
            models.Index(fields=['statut', 'expire_le']),
            models.Index(fields=['ville_normalisee', 'statut']),
            gis_models.Index(fields=['position']),
        ]

    def __str__(self):
        return f"{self.type} - {self.nom_voie}"

    def save(self, *args, **kwargs):
        # H3 calcule en Python (pas h3-pg) : evite de compiler l'extension
        # dans l'image Postgres pour un calcul qui est une ligne ici.
        # Position immuable une fois l'incident cree -- calcule une seule fois.
        if self.cellule_h3_res8 is None:
            self.cellule_h3_res8 = h3.str_to_int(
                h3.latlng_to_cell(self.position.y, self.position.x, RESOLUTION_H3_FIN)
            )
            self.cellule_h3_res7 = h3.str_to_int(
                h3.latlng_to_cell(self.position.y, self.position.x, RESOLUTION_H3_LARGE)
            )
        super().save(*args, **kwargs)

    def duree_de_base(self):
        return DUREE_VIE_BASE_PAR_TYPE.get(self.type, 60)

    def est_actif(self) -> bool:
        return self.statut == StatutIncident.ACTIF

    # confirmer()/infirmer() font une lecture-modification-ecriture en Python
    # (pas de F()) : l'appelant doit recuperer l'incident avec
    # select_for_update() dans une transaction, comme pour la detection de
    # doublon. C'est ce qui permet a confirmer() de lire self.score_confiance
    # a jour pour la promotion EN_ATTENTE -> ACTIF juste en dessous -- un F()
    # laisserait l'objet avec une expression non resolue apres save().

    def seuil_validation(self) -> Decimal:
        # Auteur suffisamment repute (>= SEUIL_REPUTATION_PALIER_REDUCTION,
        # atteint apres quelques signalements valides) : seuil reduit, ses
        # signalements suivants ont besoin de moins de corroboration.
        if self.auteur.score_reputation >= SEUIL_REPUTATION_PALIER_REDUCTION:
            return SEUIL_CONFIANCE_VALIDATION_REDUITE
        return SEUIL_CONFIANCE_VALIDATION

    def confirmer(self, vote: 'Vote'):
        self.confirmations += 1
        self.score_confiance += vote.poids
        # Prolongation plafonnee au triple de la duree de base (section 4.5).
        duree_max = timezone.timedelta(minutes=self.duree_de_base() * 3)
        self.expire_le = min(self.expire_le + timezone.timedelta(minutes=10), timezone.now() + duree_max)

        champs = ['confirmations', 'score_confiance', 'expire_le']
        # score_confiance (somme des poids de vote, pas un simple comptage) doit
        # atteindre seuil_validation() pour sortir un signalement de EN_ATTENTE
        # (cf. discussion : EN_ATTENTE reste visible dans /proches/, sinon
        # personne ne peut le confirmer). L'unicite (incident, votant) garantit
        # l'independance des votes qui composent ce score.
        if self.statut == StatutIncident.EN_ATTENTE and self.score_confiance >= self.seuil_validation():
            self.statut = StatutIncident.ACTIF
            champs.append('statut')
            # "Bon signalement" : l'auteur gagne des points de reputation des
            # que la communaute valide son signalement.
            self.auteur.score_reputation += POINTS_REPUTATION_PAR_VALIDATION
            self.auteur.save(update_fields=['score_reputation'])
        self.save(update_fields=champs)

    def infirmer(self, vote: 'Vote'):
        self.infirmations += 1
        self.score_confiance -= vote.poids
        self.expire_le = self.expire_le - timezone.timedelta(minutes=10)
        self.save(update_fields=['infirmations', 'score_confiance', 'expire_le'])

    def prolonger(self, duree: timezone.timedelta):
        self.expire_le = self.expire_le + duree
        self.save(update_fields=['expire_le'])

    def reduire(self, duree: timezone.timedelta):
        self.expire_le = self.expire_le - duree
        self.save(update_fields=['expire_le'])

    def expirer(self):
        self.statut = StatutIncident.EXPIRE
        self.save(update_fields=['statut'])

    def retirer(self, motif: str):
        self.statut = StatutIncident.RETIRE
        self.motif_retrait = motif
        self.save(update_fields=['statut', 'motif_retrait'])

    def fusionner_dans(self, incident: 'Incident'):
        self.statut = StatutIncident.FUSIONNE
        self.fusionne_dans = incident
        self.save(update_fields=['statut', 'fusionne_dans'])

    def est_doublon_de(self, autre: 'Incident') -> bool:
        # Rayon de 150 m, meme type (regle de UC-03). Le secteur de 45 degres
        # (meme sens de circulation) se calcule a partir de `cap` cote service,
        # une comparaison d'angle n'ayant pas sa place dans le modele de donnees.
        # self.position.distance() operant en degres (planaire sur SRID 4326),
        # la distance metrique est calculee via la geography column en base.
        from django.contrib.gis.db.models.functions import Distance
        from django.contrib.gis.measure import D

        if self.type != autre.type:
            return False
        resultat = Incident.objects.filter(pk=self.pk).annotate(
            distance=Distance('position', autre.position)
        ).first()
        return resultat.distance <= D(m=150)

    def impact_estime(self) -> int:
        return self.confirmations - self.infirmations


class Vote(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name='votes')
    votant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='votes')

    sens = models.CharField(max_length=20, choices=SensVote.choices)
    poids = models.DecimalField(max_digits=4, decimal_places=2)
    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'vote'
        constraints = [
            models.UniqueConstraint(fields=['incident', 'votant'], name='uniq_vote_par_utilisateur_et_incident'),
        ]

    def __str__(self):
        return f"{self.sens} - {self.incident_id}"
