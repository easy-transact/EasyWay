"""Suivi de flotte (phase 3) : vehicule declare au depart, position en
direct, exces de vitesse, signalements des chauffeurs, notifications,
acces du staff a une agence."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.gis.geos import LineString, Point
from django.urls import reverse
from django.utils import timezone

from accounts.tests import connecter, creer_utilisateur
from community.models import Incident, TypeIncident
from trips.models import StatutTrajet, Trajet, ZoneVitesse

from .flotte import rattacher_signalement, resoudre_vehicule, VehiculeRefuse
from .models import (
    Chauffeur,
    ExcesVitesse,
    NotificationOrganisation,
    RoleOrganisation,
    StatutVehicule,
    TypeNotificationOrganisation,
    Vehicule,
)
from .tests import OrganisationTestCase, ajouter_membre, creer_organisation


class FlotteTestCase(OrganisationTestCase):
    def setUp(self):
        self.organisation = creer_organisation()
        self.gestionnaire = ajouter_membre(self.organisation, RoleOrganisation.GESTIONNAIRE)
        self.conducteur = creer_utilisateur(email='chauffeur@agence.test')
        # Fiche saisie par l'agence avec le meme numero : reliee automatiquement.
        self.fiche = Chauffeur.objects.create(
            organisation=self.organisation, nom_complet='Paul Ekane', telephone=self.conducteur.telephone,
        )
        self.vehicule = Vehicule.objects.create(
            organisation=self.organisation, immatriculation='LT123AB', type='BUS', chauffeur=self.fiche,
        )

    def trajet(self, **extra):
        valeurs = dict(
            utilisateur=self.conducteur, position_origine='POINT(9.7043 4.0483)', libelle_origine='Akwa',
            position_destination='POINT(9.6970 4.0469)', libelle_destination='Bonaberi', itineraire_choisi='x',
            distance_prevue=1000, duree_prevue=100, statut=StatutTrajet.ACTIF, demarre_le=timezone.now(),
            organisation=self.organisation, vehicule=self.vehicule, chauffeur=self.fiche,
        )
        valeurs.update(extra)
        return Trajet.objects.create(**valeurs)

    def envoyer_positions(self, trajet, positions):
        jetons = connecter(self.client, self.conducteur.telephone)
        with patch('trips.views.ProducteurRedisStreams'):
            return self.client.post(
                reverse('trips:telemetrie-positions'), {'trip': str(trajet.id), 'positions': positions},
                content_type='application/json', **jetons,
            )


class LienChauffeurVehiculeTests(FlotteTestCase):
    def test_fiche_reliee_au_compte_par_le_telephone(self):
        self.fiche.refresh_from_db()
        self.assertEqual(self.fiche.utilisateur, self.conducteur)

    def test_inscription_apres_la_fiche(self):
        fiche = Chauffeur.objects.create(organisation=self.organisation, nom_complet='Awa', telephone='+237690000123')
        self.assertIsNone(fiche.utilisateur)
        compte = creer_utilisateur(email='awa@agence.test', telephone='+237690000123')
        fiche.refresh_from_db()
        self.assertEqual(fiche.utilisateur, compte)

    def test_mes_vehicules_preselectionne_le_vehicule_attribue(self):
        Vehicule.objects.create(organisation=self.organisation, immatriculation='AA001AA', type='TAXI')
        Vehicule.objects.create(
            organisation=self.organisation, immatriculation='AA002AA', type='TAXI', statut=StatutVehicule.MAINTENANCE,
        )
        reponse = self.client.get(
            reverse('organisations:mes-vehicules'), **connecter(self.client, self.conducteur.telephone)
        )
        self.assertEqual([(v['plate'], v['assigned']) for v in reponse.json()], [('LT123AB', True), ('AA001AA', False)])

    def test_conducteur_sans_agence_ne_peut_pas_declarer_de_vehicule(self):
        intrus = creer_utilisateur(email='intrus@test.cm')
        with self.assertRaises(VehiculeRefuse):
            resoudre_vehicule(intrus, self.vehicule.id)
        self.assertEqual(resoudre_vehicule(self.conducteur, self.vehicule.id), (self.vehicule, self.fiche))


class TelemetrieFlotteTests(FlotteTestCase):
    def test_position_en_direct_uniquement_pour_un_trajet_d_agence(self):
        trajet = self.trajet()
        perso = self.trajet(organisation=None, vehicule=None, chauffeur=None)
        maintenant = timezone.now().isoformat()
        for t in (trajet, perso):
            self.envoyer_positions(t, [{'lat': 4.05, 'lon': 9.70, 'speed_kmh': 40, 'heading': 90, 'timestamp': maintenant}])
        trajet.refresh_from_db()
        perso.refresh_from_db()
        self.assertEqual((trajet.derniere_position.y, trajet.derniere_vitesse_kmh), (4.05, 40))
        self.assertIsNone(perso.derniere_position)

        en_direct = self.get('en-direct', self.gestionnaire).json()
        self.assertEqual([v['vehicle']['plate'] for v in en_direct], ['LT123AB'])

    def test_exces_regroupes_en_un_episode_et_une_notification(self):
        trajet = self.trajet()
        t0 = timezone.now()
        positions = [
            {'lat': 4.05, 'lon': 9.70, 'speed_kmh': v, 'timestamp': (t0 + timedelta(seconds=5 * i)).isoformat()}
            for i, v in enumerate([50, 70, 82, 75, 55])
        ]
        self.envoyer_positions(trajet, positions)
        episode = ExcesVitesse.objects.get()
        self.assertEqual((episode.limite_kmh, episode.vitesse_max_kmh, episode.nb_positions), (60, 82, 3))
        self.assertEqual(
            NotificationOrganisation.objects.filter(type=TypeNotificationOrganisation.EXCES_VITESSE).count(), 1
        )
        # Nouveau depassement plus d'une minute apres : second episode, pas de 2e notification.
        self.envoyer_positions(trajet, [
            {'lat': 4.05, 'lon': 9.70, 'speed_kmh': 90, 'timestamp': (t0 + timedelta(minutes=5)).isoformat()},
        ])
        self.assertEqual(ExcesVitesse.objects.count(), 2)
        self.assertEqual(NotificationOrganisation.objects.count(), 1)

    def test_limite_de_la_zone_et_tolerance(self):
        ZoneVitesse.objects.create(
            nom='Ecole', point_depart=Point(9.699, 4.05, srid=4326), point_arrivee=Point(9.701, 4.05, srid=4326),
            geometrie=LineString([(9.699, 4.05), (9.701, 4.05)], srid=4326), vitesse_max_kmh=30,
        )
        trajet = self.trajet()
        maintenant = timezone.now().isoformat()
        self.envoyer_positions(trajet, [{'lat': 4.05, 'lon': 9.70, 'speed_kmh': 34, 'timestamp': maintenant}])
        self.assertFalse(ExcesVitesse.objects.exists())  # 34 <= 30 + 5 de tolerance
        self.envoyer_positions(trajet, [{'lat': 4.05, 'lon': 9.70, 'speed_kmh': 45, 'timestamp': maintenant}])
        self.assertEqual(ExcesVitesse.objects.get().nom_zone, 'Ecole')


class SignalementsFlotteTests(FlotteTestCase):
    def test_signalement_pendant_un_trajet_d_agence(self):
        trajet = self.trajet()
        incident = Incident.objects.create(
            auteur=self.conducteur, type=TypeIncident.POLICE, position=Point(9.70, 4.05, srid=4326),
            expire_le=timezone.now() + timedelta(hours=1),
        )
        incident.refresh_from_db()
        self.assertEqual(incident.trajet, trajet)
        donnees = self.get('signalements', self.gestionnaire).json()
        self.assertEqual(donnees['results'][0]['vehicle']['plate'], 'LT123AB')
        self.assertTrue(NotificationOrganisation.objects.filter(type=TypeNotificationOrganisation.SIGNALEMENT).exists())

    def test_signalement_hors_trajet_d_agence_reste_prive(self):
        self.trajet(organisation=None, vehicule=None, chauffeur=None)
        incident = Incident.objects.create(
            auteur=self.conducteur, type=TypeIncident.POLICE, position=Point(9.70, 4.05, srid=4326),
            expire_le=timezone.now() + timedelta(hours=1),
        )
        self.assertIsNone(rattacher_signalement(incident))
        self.assertEqual(self.get('signalements', self.gestionnaire).json()['count'], 0)


class IsolementEtStaffTests(FlotteTestCase):
    def test_une_autre_agence_ne_voit_pas_les_trajets(self):
        trajet = self.trajet()
        autre = creer_organisation(nom='Autre')
        lecteur_autre = ajouter_membre(autre, RoleOrganisation.LECTEUR)
        self.assertEqual(self.get('trajets', lecteur_autre).json()['count'], 0)
        self.assertEqual(self.get('trajet-detail', lecteur_autre, trajet.id).status_code, 404)
        self.assertEqual(self.get('trajets', self.gestionnaire).json()['count'], 1)

    def test_staff_consulte_une_agence_par_en_tete(self):
        self.trajet()
        staff = creer_utilisateur(email='staff@easyway.local', is_staff=True)
        jetons = connecter(self.client, staff.telephone)
        reponse = self.client.get(
            reverse('organisations:trajets'), HTTP_X_EW_ORGANISATION=str(self.organisation.id), **jetons
        )
        self.assertEqual(reponse.json()['count'], 1)
        moi = self.client.get(reverse('organisations:moi'), HTTP_X_EW_ORGANISATION=str(self.organisation.id), **jetons)
        self.assertTrue(moi.json()['staff_access'])
        # Sans l'en-tete, le staff n'est membre d'aucune agence.
        self.assertEqual(self.client.get(reverse('organisations:trajets'), **jetons).status_code, 403)

    def test_non_staff_ne_peut_pas_utiliser_l_en_tete(self):
        autre = creer_organisation(nom='Autre')
        membre_autre = ajouter_membre(autre, RoleOrganisation.ADMIN)
        reponse = self.client.get(
            reverse('organisations:trajets'), HTTP_X_EW_ORGANISATION=str(self.organisation.id),
            **connecter(self.client, membre_autre.telephone),
        )
        self.assertEqual(reponse.json()['count'], 0)  # reste sur sa propre agence

    def test_notifications_et_badges(self):
        NotificationOrganisation.objects.create(
            organisation=self.organisation, type=TypeNotificationOrganisation.EXCES_VITESSE, titre='Exces',
        )
        self.assertEqual(self.get('badges', self.gestionnaire).json()['unread_notifications'], 1)
        self.envoyer('post', 'notifications-tout-lu', self.gestionnaire)
        self.assertEqual(self.get('badges', self.gestionnaire).json()['unread_notifications'], 0)

    def test_tableau_de_bord_inclut_la_flotte(self):
        self.trajet()
        flotte = self.get('tableau-de-bord', self.gestionnaire).json()['fleet']
        self.assertEqual((flotte['trips_today'], flotte['drivers_with_app']), (1, 1))
