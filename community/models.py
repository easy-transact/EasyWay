import uuid
from decimal import Decimal

import h3
from django.conf import settings
from django.contrib.gis.db import models as gis_models
from django.contrib.gis.geos import Point
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from accounts.models import Utilisateur

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
    # Signalement officiel (mairie, police) programme : date de debut dans le
    # futur -- hors des listes actives jusqu'a ce que activer_programmes
    # (community.tasks) le passe ACTIF a debut_le.
    PROGRAMME = 'PROGRAMME', 'Programme'


class SensVote(models.TextChoices):
    CONFIRMATION = 'CONFIRMATION', 'Confirmation'
    INFIRMATION = 'INFIRMATION', 'Infirmation'


# Durees de vie de base, en minutes, avant prolongation/reduction par les
# votes (cf. Incident.confirmer/infirmer). ACCIDENT/EMBOUTEILLAGE fixes en
# reunion du 29/09 : accident 4h, carambolage 6h (sous-type, cf.
# DUREE_VIE_PAR_SOUS_TYPE), embouteillage 30 min en ville / 45 min hors
# ville (cf. DUREE_VIE_HORS_AGGLOMERATION) -- un bouchon de rase campagne
# met plus longtemps a se resorber, faute d'itineraire de delestage.
DUREE_VIE_BASE_PAR_TYPE = {
    TypeIncident.DANGER: 100,
    TypeIncident.POLICE: 100,
    TypeIncident.EMBOUTEILLAGE: 30,
    TypeIncident.ACCIDENT: 240,
    TypeIncident.ROUTE_BARREE: 240,
    TypeIncident.VOIE_BLOQUEE: 100,
    TypeIncident.MAUVAIS_TEMPS: 120,
    TypeIncident.RADAR: 480,
}
DUREE_VIE_PAR_SOUS_TYPE = {
    SousTypeIncident.CARAMBOLAGE: 360,
    # Un nid-de-poule ne se rebouche pas dans la journee : 30 jours, raccourcis
    # par les votes "n'existe plus" (cf. REDUCTION_PAR_INFIRMATION_LONGUE).
    SousTypeIncident.NID_DE_POULE: 30 * 24 * 60,
}
# Signalements de longue duree (>= 1 jour, ex. nid-de-poule) : chaque vote
# "n'existe plus" retire 1/5 de la duree de base au lieu des 10 min
# habituelles -- sinon il faudrait des milliers de votes pour le faire
# disparaitre. 5 votes suffisent a le retirer.
SEUIL_DUREE_LONGUE_MIN = 24 * 60
REDUCTION_PAR_INFIRMATION_LONGUE = 5
# Remplace DUREE_VIE_BASE_PAR_TYPE quand le signalement est hors agglomeration
# (Incident.en_agglomeration=False). Inconnu (None, Nominatim indisponible)
# = valeur en ville : la plupart des signalements sont urbains, et les votes
# prolongent de toute facon un incident qui dure.
DUREE_VIE_HORS_AGGLOMERATION = {
    TypeIncident.EMBOUTEILLAGE: 45,
}


def duree_de_vie_base(type_incident, sous_type='', en_agglomeration=None) -> int:
    """Duree de vie de base (minutes) d'un signalement : le sous-type prime
    (ex. carambolage), puis le lieu (hors agglomeration), puis le type."""
    if sous_type in DUREE_VIE_PAR_SOUS_TYPE:
        return DUREE_VIE_PAR_SOUS_TYPE[sous_type]
    if en_agglomeration is False and type_incident in DUREE_VIE_HORS_AGGLOMERATION:
        return DUREE_VIE_HORS_AGGLOMERATION[type_incident]
    return DUREE_VIE_BASE_PAR_TYPE.get(type_incident, 60)

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
# Utilisateur.points : points de recompense (convertis en bons carburant via
# une station partenaire), distincts de score_reputation (fiabilite des
# signalements, pese les votes, cf. poids_de_vote) -- sans effet sur le
# fonctionnement du systeme. Bareme fixe en reunion du 29/09 : 1 point par
# signalement personnel valide par la communaute, 0,25 par confirmation du
# signalement d'un autre (vote ou re-signalement d'un doublon).
POINTS_PAR_SIGNALEMENT_VALIDE = Decimal('1')
POINTS_PAR_VALIDATION_TIERCE = Decimal('0.25')
# Malus de reputation quand un signalement est juge faux (retire par la
# moderation) : la fiabilite peut enfin redescendre. Plancher 0 (jamais
# negative) : le poids de vote reste au pire neutre (1,0). 1,0 = annule
# l'equivalent de DEUX signalements valides (gain +0,5 chacun), pour que les
# faux coutent nettement plus cher que les bons ne rapportent. Multiple de
# 0,1 (score_reputation a une seule decimale).
MALUS_REPUTATION_SIGNALEMENT_FAUX = Decimal('1.0')
# Signalement CONTESTE : au moins INFIRMATIONS_MIN_CONTESTE "Plus rien" et un
# score repasse sous zero. Il n'agit plus sur l'itineraire (fermeture, retard,
# risque, traffic.tar) mais reste visible pour que d'autres tranchent. Jamais
# pour un signalement valide par la moderation ou officiel (autorite).
INFIRMATIONS_MIN_CONTESTE = 2
Q_CONTESTE = (
    Q(infirmations__gte=INFIRMATIONS_MIN_CONTESTE, score_confiance__lt=0)
    & Q(valide_par_staff=False, organisation__isnull=True)
)


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
    # En ville (True) ou hors agglomeration (False), deduit du geocodage
    # inverse au signalement (cf. client_nominatim._est_en_agglomeration) --
    # module la duree de vie (cf. duree_de_vie_base). None si Nominatim etait
    # indisponible, ou pour un incident anterieur a cette colonne.
    en_agglomeration = models.BooleanField(null=True, blank=True)
    # Cales sur le graphe routier Valhalla au signalement (cf.
    # ServiceIncident._verifier_position_routiere) : identifiant de voie OSM
    # + sens de circulation dessus. Remplace le matching par distance pure
    # dans IncidentsSurTrajetView -- aucun couloir de tolerance ne separe une
    # contre-allee parallele de la route qu'elle longe, seule la topologie du
    # graphe le peut (cf. discussion). Nullable : absent si Valhalla est
    # indisponible au signalement, ou pour tout incident cree avant cette
    # colonne -- pas de backfill, ces incidents expirent naturellement sous
    # 8h max (DUREE_VIE_BASE_PAR_TYPE) et retombent sur le repli par couloir.
    way_id_osm = models.BigIntegerField(null=True, blank=True)
    forward_osm = models.BooleanField(null=True, blank=True)

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
    # Trajet d'agence en cours chez l'auteur au moment du signalement (cf.
    # organisations.flotte.rattacher_signalement) : rend le signalement
    # visible par l'organisation du vehicule. Vide sinon.
    trajet = models.ForeignKey(
        'trips.Trajet', on_delete=models.SET_NULL, null=True, blank=True, related_name='signalements'
    )

    # Valide par la moderation (back-office) : vaut confirmation, sans attendre
    # les votes -- une route barree validee est evitee tout de suite par le
    # calcul d'itineraire (cf. trips/services/retards_signalements.py).
    valide_par_staff = models.BooleanField(default=False)
    valide_le = models.DateTimeField(null=True, blank=True)

    # Signalement officiel pose par une autorite (mairie, police) depuis le
    # back-office. Vide pour un signalement de conducteur. Un signalement
    # officiel est marque valide_par_staff=True (donc traite comme confirme :
    # une fermeture officielle est evitee tout de suite), et porte une periode
    # d'affichage debut_le -> expire_le ("travaux du 10 au 20").
    organisation = models.ForeignKey(
        'organisations.Organisation', on_delete=models.CASCADE, null=True, blank=True,
        related_name='signalements_officiels',
    )
    debut_le = models.DateTimeField(null=True, blank=True, help_text='Debut de la periode (officiel) ; sinon cree_le')
    # Motif libre affiche au conducteur (officiel) : "Refection de la chaussee",
    # "Cortege officiel"... Vide pour un signalement de conducteur.
    note = models.CharField(max_length=255, blank=True, default='')

    cree_le = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'incident'
        indexes = [
            models.Index(fields=['cellule_h3_res8', 'statut']),
            models.Index(fields=['statut', 'expire_le']),
            models.Index(fields=['ville_normalisee', 'statut']),
            models.Index(fields=['way_id_osm', 'forward_osm']),
            gis_models.Index(fields=['position']),
        ]

    def __str__(self):
        return f"{self.type} - {self.nom_voie}"

    @property
    def est_officiel(self):
        return self.organisation_id is not None

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

    def deplacer(self, lat, lon):
        """Repositionne l'incident (back-office) : recalcule les cellules H3.
        Retourne l'ancienne cellule res8 (a invalider en plus de la nouvelle)."""
        ancienne_cellule = self.cellule_h3_res8
        self.position = Point(lon, lat, srid=4326)
        self.cellule_h3_res8 = h3.str_to_int(h3.latlng_to_cell(lat, lon, RESOLUTION_H3_FIN))
        self.cellule_h3_res7 = h3.str_to_int(h3.latlng_to_cell(lat, lon, RESOLUTION_H3_LARGE))
        self.save(update_fields=['position', 'cellule_h3_res8', 'cellule_h3_res7'])
        return ancienne_cellule

    def duree_de_base(self):
        return duree_de_vie_base(self.type, self.sous_type, self.en_agglomeration)

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

    @property
    def est_conteste(self) -> bool:
        """Cf. Q_CONTESTE (meme regle, cote Python)."""
        if self.valide_par_staff or self.est_officiel:
            return False
        return self.infirmations >= INFIRMATIONS_MIN_CONTESTE and self.score_confiance < 0

    @property
    def confirme_pour_fermeture(self) -> bool:
        """Seuil UNIQUE pour retirer une route du graphe de routage de tous les
        utilisateurs : le meme score pondere que pour devenir ACTIF
        (seuil_validation), ou une validation par la moderation, ou un
        signalement officiel. Le score baisse a chaque "Plus rien" : une
        fermeture contestee est donc levee sans attendre l'expiration. En
        dessous, une route barree reste un fort ralentissement (mecanisme
        FERMETURE non confirme, cf. trips.services.retards_signalements)."""
        if self.valide_par_staff or self.est_officiel:
            return True
        return not self.est_conteste and self.score_confiance >= self.seuil_validation()

    def confirmer(self, vote: 'Vote'):
        # F() plutot qu'un += sur vote.votant : l'instance de l'appelant
        # (request.user) n'a pas a etre verrouillee pour ce simple compteur.
        Utilisateur.objects.filter(pk=vote.votant_id).update(
            points=F('points') + POINTS_PAR_VALIDATION_TIERCE
        )
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
            # que la communaute valide son signalement, et des points de
            # gamification (cf. POINTS_PAR_SIGNALEMENT_VALIDE, deux compteurs
            # distincts -- l'un pese les votes, l'autre est juste affiche).
            self.auteur.score_reputation += POINTS_REPUTATION_PAR_VALIDATION
            self.auteur.save(update_fields=['score_reputation'])
            # F() : un save() de self.auteur.points ecraserait les 0,25
            # gagnes entre-temps par l'auteur en confirmant d'autres signalements.
            Utilisateur.objects.filter(pk=self.auteur_id).update(
                points=F('points') + POINTS_PAR_SIGNALEMENT_VALIDE
            )
        self.save(update_fields=champs)

    def infirmer(self, vote: 'Vote'):
        self.infirmations += 1
        self.score_confiance -= vote.poids
        base = self.duree_de_base()
        reduction_min = base / REDUCTION_PAR_INFIRMATION_LONGUE if base >= SEUIL_DUREE_LONGUE_MIN else 10
        self.expire_le = self.expire_le - timezone.timedelta(minutes=reduction_min)
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

    def valider_par_moderation(self):
        """Le staff confirme le signalement : visible de tous (ACTIF) et
        traite comme confirme par le calcul d'itineraire."""
        self.valide_par_staff = True
        self.valide_le = timezone.now()
        champs = ['valide_par_staff', 'valide_le']
        if self.statut == StatutIncident.EN_ATTENTE:
            self.statut = StatutIncident.ACTIF
            champs.append('statut')
        self.save(update_fields=champs)

    def retirer(self, motif: str):
        self.statut = StatutIncident.RETIRE
        self.motif_retrait = motif
        self.save(update_fields=['statut', 'motif_retrait'])

    def penaliser_auteur(self):
        """Signalement juge faux : l'auteur perd de la reputation (plancher 0).
        Idempotent par l'appelant (appele une fois, au retrait). Un signalement
        officiel (autorite) n'est jamais penalise."""
        if self.organisation_id is not None:
            return
        auteur = self.auteur
        auteur.score_reputation = max(Decimal('0'), auteur.score_reputation - MALUS_REPUTATION_SIGNALEMENT_FAUX)
        auteur.save(update_fields=['score_reputation'])

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
