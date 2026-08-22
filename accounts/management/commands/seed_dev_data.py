"""
Jeu de donnees de demo pour tester manuellement via Swagger (/api/docs/).
Idempotent : reexecutable sans creer de doublons.

Usage : python manage.py seed_dev_data
"""

from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import Appareil, Formule, Parametres, Utilisateur
from places.models import AdresseEnregistree, LibelleAdresse, Lieu, RechercheRecente, SourceLieu, StatutLieu
from places.utils import normaliser

MOT_DE_PASSE = 'Demo1234!'

UTILISATEURS = [
    dict(email='demo@easyway.local', nom_complet='Demo Gratuite', ville='Douala',
         type_vehicule='VOITURE', formule=Formule.GRATUITE, email_verifie=True),
    dict(email='premium@easyway.local', nom_complet='Demo Premium', ville='Yaounde',
         type_vehicule='MOTO', formule=Formule.PREMIUM, email_verifie=True),
    dict(email='nonverifie@easyway.local', nom_complet='Demo Non Verifie', ville='Douala',
         type_vehicule='TAXI', formule=Formule.GRATUITE, email_verifie=False),
    dict(email='banni@easyway.local', nom_complet='Demo Banni', ville='Douala',
         type_vehicule='VOITURE', formule=Formule.GRATUITE, email_verifie=True, est_banni=True),
]

# (nom, categorie, ville, quartier, lat, lon)
LIEUX_APPROUVES = [
    ('Marche Central', 'marketplace', 'Douala', 'Akwa', 4.0483, 9.7043),
    ('Hopital General de Douala', 'hospital', 'Douala', 'Bonanjo', 4.0469, 9.6970),
    ('Aeroport International de Douala', 'aerodrome', 'Douala', 'Douala', 4.0061, 9.7195),
    ('Pharmacie du Rond-Point Deido', 'pharmacy', 'Douala', 'Deido', 4.0611, 9.7089),
    ('Arret Bus Bonapriso', 'bus_stop', 'Douala', 'Bonapriso', 4.0350, 9.7090),
    ('Palais des Congres', 'conference_centre', 'Yaounde', 'Centre-ville', 3.8690, 11.5174),
    ('Hopital Central de Yaounde', 'hospital', 'Yaounde', 'Centre-ville', 3.8667, 11.5167),
    ('Marche Mfoundi', 'marketplace', 'Yaounde', 'Mfoundi', 3.8663, 11.5177),
    ('Universite de Yaounde I', 'university', 'Yaounde', 'Ngoa-Ekelle', 3.8500, 11.5010),
    ('Quartier Bastos', 'suburb', 'Yaounde', None, 3.8850, 11.5180),
]

LIEU_EN_ATTENTE = ('Nouveau Restaurant Non Modere', 'restaurant', 'Douala', 'Akwa', 4.0500, 9.7000)


class Command(BaseCommand):
    help = 'Seede des utilisateurs et lieux de demo pour tester via Swagger.'

    def handle(self, *args, **options):
        utilisateurs = {}
        for donnees in UTILISATEURS:
            donnees = dict(donnees)
            email = donnees.pop('email')
            utilisateur, cree = Utilisateur.objects.get_or_create(email=email, defaults=donnees)
            if cree:
                utilisateur.set_password(MOT_DE_PASSE)
                utilisateur.cgu_acceptee_le = timezone.now()
                utilisateur.save()
                Parametres.objects.get_or_create(utilisateur=utilisateur)
            else:
                for champ, valeur in donnees.items():
                    setattr(utilisateur, champ, valeur)
                utilisateur.set_password(MOT_DE_PASSE)
                utilisateur.save()
                Parametres.objects.get_or_create(utilisateur=utilisateur)
            utilisateurs[email] = utilisateur
            self.stdout.write(f"  utilisateur: {email} / {MOT_DE_PASSE}")

        demo = utilisateurs['demo@easyway.local']
        Appareil.objects.update_or_create(
            utilisateur=demo, jeton_push='ExponentPushToken[demo-device]',
            defaults=dict(plateforme='ANDROID', version_application='1.0.0', version_systeme='14'),
        )

        lieux_crees = []
        for nom, categorie, ville, quartier, lat, lon in LIEUX_APPROUVES:
            lieu, _ = Lieu.objects.update_or_create(
                nom=nom, ville=ville,
                defaults=dict(
                    nom_normalise=normaliser(nom), categorie=categorie, quartier=quartier,
                    position=Point(lon, lat, srid=4326),
                    source=SourceLieu.OPENSTREETMAP, statut=StatutLieu.APPROUVE,
                ),
            )
            lieux_crees.append(lieu)
        self.stdout.write(f"  {len(lieux_crees)} lieux approuves (Douala + Yaounde)")

        nom, categorie, ville, quartier, lat, lon = LIEU_EN_ATTENTE
        Lieu.objects.update_or_create(
            nom=nom, ville=ville,
            defaults=dict(
                nom_normalise=normaliser(nom), categorie=categorie, quartier=quartier,
                position=Point(lon, lat, srid=4326),
                source=SourceLieu.UTILISATEUR, statut=StatutLieu.EN_ATTENTE, propose_par=demo,
            ),
        )
        self.stdout.write("  1 lieu EN_ATTENTE (pour tester la moderation admin)")

        marche_central = next(l for l in lieux_crees if l.nom == 'Marche Central')
        AdresseEnregistree.objects.update_or_create(
            utilisateur=demo, libelle=LibelleAdresse.DOMICILE,
            defaults=dict(adresse='Rue de la Joie, Akwa', position=Point(9.705, 4.049, srid=4326)),
        )
        AdresseEnregistree.objects.update_or_create(
            utilisateur=demo, libelle=LibelleAdresse.TRAVAIL,
            defaults=dict(adresse='Zone Industrielle, Bonaberi', position=Point(9.680, 4.070, srid=4326)),
        )
        RechercheRecente.objects.get_or_create(
            utilisateur=demo, libelle=marche_central.nom,
            defaults=dict(sous_libelle=marche_central.quartier, position=marche_central.position),
        )

        self.stdout.write(self.style.SUCCESS(
            f"Termine. Connectez-vous sur /api/docs/ avec l'un des comptes ci-dessus "
            f"(mot de passe: {MOT_DE_PASSE})."
        ))
