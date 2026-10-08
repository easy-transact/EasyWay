from datetime import timedelta

from django.contrib.gis.geos import Point
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import InscriptionListeAttente
from accounts.tests import connecter, creer_utilisateur
from community.models import Incident, StatutIncident, TypeIncident
from places.models import Lieu, SourceLieu, StatutLieu
from places.utils import normaliser

from .models import CampagnePublicitaire, EntreeAudit, Emplacement, Impression, NotificationStaff, TypeNotification


def creer_lieu_propose(nom, auteur, statut=StatutLieu.EN_ATTENTE, lat=4.05, lon=9.70):
    return Lieu.objects.create(
        nom=nom, nom_normalise=normaliser(nom), categorie='restaurant', ville='Douala',
        position=Point(lon, lat, srid=4326), source=SourceLieu.UTILISATEUR, statut=statut, propose_par=auteur,
    )


def creer_incident(auteur, **extra):
    return Incident.objects.create(
        auteur=auteur, type=TypeIncident.POLICE, position=Point(9.70, 4.05, srid=4326),
        ville='Douala', nom_voie='Rond-point Deido', expire_le=timezone.now() + timedelta(hours=1), **extra,
    )


class StaffTestCase(TestCase):
    def setUp(self):
        self.staff = creer_utilisateur('staff@easyway.local', is_staff=True)
        self.jetons = connecter(self.client, self.staff.telephone)
        self.membre = creer_utilisateur('membre@easyway.local')


class AccesStaffTests(StaffTestCase):
    def test_endpoints_refuses_aux_non_staff(self):
        jetons = connecter(self.client, self.membre.telephone)
        for nom in ['staff-moi', 'staff-badges', 'staff-stats', 'staff-notifications', 'staff-audit', 'staff-campagnes']:
            with self.subTest(nom=nom):
                self.assertEqual(self.client.get(reverse(f'ads_admin:{nom}'), **jetons).status_code, 403)

    def test_profil_staff(self):
        reponse = self.client.get(reverse('ads_admin:staff-moi'), **self.jetons)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['name'], 'Test User')
        self.assertFalse(reponse.json()['is_admin'])


class NotificationsTests(StaffTestCase):
    def test_lieux_proposes_regroupes_sur_une_notification(self):
        creer_lieu_propose('Boulangerie Saker', self.membre)
        creer_lieu_propose('Station Tradex', self.membre)
        notifications = NotificationStaff.objects.filter(type=TypeNotification.LIEU)
        self.assertEqual(notifications.count(), 1)
        self.assertEqual(notifications.get().compteur, 2)
        self.assertEqual(notifications.get().titre, '2 nouveaux lieux proposés')

    def test_lieu_importe_sans_auteur_ne_notifie_pas(self):
        Lieu.objects.create(
            nom='Import OSM', nom_normalise='import osm', categorie='x', ville='Douala',
            position=Point(9.7, 4.05, srid=4326), source=SourceLieu.OPENSTREETMAP,
        )
        self.assertFalse(NotificationStaff.objects.exists())

    def test_nouvelle_notification_apres_lecture(self):
        creer_lieu_propose('Premier', self.membre)
        NotificationStaff.objects.get().lue_par.add(self.staff)
        creer_lieu_propose('Second', self.membre)
        self.assertEqual(NotificationStaff.objects.count(), 2)

    def test_inscription_liste_attente_notifie(self):
        InscriptionListeAttente.objects.create(nom_complet='Jean', telephone='+237690000001')
        self.assertTrue(NotificationStaff.objects.filter(type=TypeNotification.ATTENTE).exists())

    def test_incident_probablement_faux_notifie_une_seule_fois(self):
        incident = creer_incident(self.membre)
        for infirmations in (3, 4):
            incident.infirmations = infirmations
            incident.save(update_fields=['infirmations'])
        self.assertEqual(NotificationStaff.objects.filter(type=TypeNotification.INCIDENT).count(), 1)

    def test_incident_majoritairement_confirme_ne_notifie_pas(self):
        incident = creer_incident(self.membre, confirmations=10)
        incident.infirmations = 3
        incident.save(update_fields=['infirmations'])
        self.assertFalse(NotificationStaff.objects.exists())

    def test_liste_marquer_lu_et_tout_lu(self):
        creer_lieu_propose('Un', self.membre)
        InscriptionListeAttente.objects.create(nom_complet='Jean', telephone='+237690000002')
        url = reverse('ads_admin:staff-notifications')

        donnees = self.client.get(url, **self.jetons).json()
        self.assertEqual(donnees['unread_count'], 2)
        self.assertFalse(donnees['results'][0]['read'])

        premiere = donnees['results'][0]['id']
        self.client.post(reverse('ads_admin:staff-notification-lue', kwargs={'id': premiere}), **self.jetons)
        self.assertEqual(self.client.get(url, **self.jetons).json()['unread_count'], 1)
        self.assertEqual(len(self.client.get(url, {'unread': '1'}, **self.jetons).json()['results']), 1)

        self.client.post(reverse('ads_admin:staff-notifications-tout-lu'), **self.jetons)
        self.assertEqual(self.client.get(url, **self.jetons).json()['unread_count'], 0)

    def test_types_desactives_masques(self):
        creer_lieu_propose('Un', self.membre)
        reponse = self.client.patch(
            reverse('ads_admin:staff-notifications-preferences'), {'disabled_types': ['LIEU']},
            content_type='application/json', **self.jetons,
        )
        self.assertEqual(reponse.status_code, 200)
        donnees = self.client.get(reverse('ads_admin:staff-notifications'), **self.jetons).json()
        self.assertEqual(donnees['results'], [])
        self.assertEqual(donnees['unread_count'], 0)

    def test_badges(self):
        creer_lieu_propose('Un', self.membre)
        donnees = self.client.get(reverse('ads_admin:staff-badges'), **self.jetons).json()
        self.assertEqual(donnees['unread_notifications'], 1)
        self.assertEqual(donnees['places_pending'], 1)
        self.assertEqual(donnees['latest_notification']['type'], 'LIEU')


class StatsTests(StaffTestCase):
    def test_structure_et_compteurs(self):
        creer_lieu_propose('Un', self.membre)
        creer_incident(self.membre)
        creer_incident(self.membre, statut=StatutIncident.RETIRE)
        donnees = self.client.get(reverse('ads_admin:staff-stats'), **self.jetons).json()
        self.assertEqual(donnees['to_handle']['places_pending'], 1)
        self.assertEqual(donnees['places']['EN_ATTENTE'], 1)
        self.assertEqual(donnees['incidents']['removed'], 1)
        self.assertEqual(len(donnees['reports_30d']), 30)
        self.assertEqual(donnees['reports_30d'][-1]['reported'], 2)
        self.assertEqual(donnees['reports_30d'][-1]['removed'], 1)
        self.assertEqual(donnees['kpis']['users']['series'][-1], donnees['users']['total'])
        self.assertEqual(donnees['incidents_by_city'], [{'city': 'Douala', 'total': 1}])


class AuditTests(StaffTestCase):
    def test_approbation_journalisee_et_filtrable(self):
        lieu = creer_lieu_propose('Pharmacie du Marche', self.membre)
        self.client.post(reverse('places:staff-lieu-approuver', kwargs={'id': lieu.id}), **self.jetons)

        entree = EntreeAudit.objects.get()
        self.assertEqual(entree.action, 'places.approve')
        self.assertEqual(entree.acteur, self.staff)

        url = reverse('ads_admin:staff-audit')
        self.assertEqual(len(self.client.get(url, {'domain': 'places'}, **self.jetons).json()['results']), 1)
        self.assertEqual(len(self.client.get(url, {'domain': 'incidents'}, **self.jetons).json()['results']), 0)
        resultats = self.client.get(url, {'search': 'pharmacie'}, **self.jetons).json()['results']
        self.assertEqual(resultats[0]['target_label'], 'Pharmacie du Marche')

    def test_suppression_garde_le_libelle(self):
        lieu = creer_lieu_propose('Doublon', self.membre, statut=StatutLieu.REJETE)
        self.client.delete(reverse('places:staff-lieu-supprimer', kwargs={'id': lieu.id}), **self.jetons)
        self.assertEqual(EntreeAudit.objects.get().valeur_nouvelle['libelle'], 'Doublon')


class ModerationLieuxTests(StaffTestCase):
    def test_actions_groupees(self):
        a, b = creer_lieu_propose('A', self.membre), creer_lieu_propose('B', self.membre)
        reponse = self.client.post(
            reverse('places:staff-lieux-groupe'), {'ids': [str(a.id), str(b.id)], 'action': 'reject', 'reason': 'Doublon'},
            content_type='application/json', **self.jetons,
        )
        self.assertEqual(reponse.json(), {'processed': 2})
        a.refresh_from_db()
        self.assertEqual((a.statut, a.motif_rejet), (StatutLieu.REJETE, 'Doublon'))
        self.assertEqual(EntreeAudit.objects.filter(action='places.reject').count(), 2)

    def test_rejet_groupe_sans_motif_refuse(self):
        a = creer_lieu_propose('A', self.membre)
        reponse = self.client.post(
            reverse('places:staff-lieux-groupe'), {'ids': [str(a.id)], 'action': 'reject'},
            content_type='application/json', **self.jetons,
        )
        self.assertEqual(reponse.status_code, 400)

    def test_liste_recherche_et_historique_auteur(self):
        creer_lieu_propose('Ancien rejet', self.membre, statut=StatutLieu.REJETE)
        creer_lieu_propose('Boulangerie Saker', self.membre)
        creer_lieu_propose('Garage', self.membre)
        reponse = self.client.get(reverse('places:staff-lieux'), {'search': 'saker'}, **self.jetons).json()
        self.assertEqual([r['name'] for r in reponse['results']], ['Boulangerie Saker'])
        self.assertEqual(reponse['results'][0]['proposer']['rejected'], 1)
        self.assertIsNotNone(reponse['results'][0]['created_at'])

    def test_fiche_signale_les_doublons_proches(self):
        lieu = creer_lieu_propose('Tradex Ndokoti', self.membre)
        creer_lieu_propose('Station Tradex', self.membre, statut=StatutLieu.APPROUVE, lat=4.0503)
        creer_lieu_propose('Loin', self.membre, statut=StatutLieu.APPROUVE, lat=4.10)
        donnees = self.client.get(reverse('places:staff-lieu-supprimer', kwargs={'id': lieu.id}), **self.jetons).json()
        self.assertEqual([p['name'] for p in donnees['nearby']], ['Station Tradex'])


class ModerationIncidentsTests(StaffTestCase):
    def test_filtre_suspect_et_compteur_retraits(self):
        creer_incident(self.membre, infirmations=5, confirmations=1)
        creer_incident(self.membre, confirmations=4)
        creer_incident(self.membre, statut=StatutIncident.RETIRE)
        url = reverse('community:staff-incidents')

        tous = self.client.get(url, **self.jetons).json()['results']
        self.assertEqual(len(tous), 2)
        self.assertEqual(sorted(i['suspect'] for i in tous), [False, True])
        self.assertEqual({i['author_removed_7d'] for i in tous}, {1})
        self.assertEqual(len(self.client.get(url, {'suspect': '1'}, **self.jetons).json()['results']), 1)

    def test_retraits_repetes_notifient_un_abus(self):
        for _ in range(3):
            incident = creer_incident(self.membre)
            self.client.post(
                reverse('community:staff-incident-retirer', kwargs={'id': incident.id}), {'reason': 'Faux'},
                content_type='application/json', **self.jetons,
            )
        self.assertEqual(NotificationStaff.objects.filter(type=TypeNotification.ABUS).count(), 1)
        self.assertEqual(EntreeAudit.objects.filter(action='incidents.remove').count(), 3)


class CampagnesTests(StaffTestCase):
    def donnees(self, **extra):
        maintenant = timezone.now()
        return {
            'name': 'Orange Money', 'advertiser': 'Orange', 'creative_url': 'https://exemple.cm/a.png',
            'target_url': 'https://exemple.cm', 'placement': Emplacement.BANNIERE_RECHERCHE,
            'cities': ['Douala'], 'starts_at': (maintenant - timedelta(days=1)).isoformat(),
            'ends_at': (maintenant + timedelta(days=10)).isoformat(), 'daily_cap': 2, **extra,
        }

    def test_creation_et_statistiques_du_jour(self):
        reponse = self.client.post(
            reverse('ads_admin:staff-campagnes'), self.donnees(), content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 201)
        campagne = CampagnePublicitaire.objects.get()
        Impression.objects.create(campagne=campagne, evenement='AFFICHAGE')
        Impression.objects.create(campagne=campagne, evenement='CLIC')

        donnees = self.client.get(reverse('ads_admin:staff-campagnes'), **self.jetons).json()
        resultat = donnees['results'][0]
        self.assertEqual((resultat['impressions_today'], resultat['clicks_today']), (1, 1))
        self.assertEqual(resultat['status'], 'PLAFOND')
        self.assertEqual(donnees['summary']['ctr_pct'], 100.0)
        self.assertTrue(NotificationStaff.objects.filter(type=TypeNotification.PUB).exists())
        self.assertTrue(EntreeAudit.objects.filter(action='ads.create').exists())

    def test_fin_avant_debut_refusee(self):
        maintenant = timezone.now()
        reponse = self.client.post(
            reverse('ads_admin:staff-campagnes'),
            self.donnees(starts_at=maintenant.isoformat(), ends_at=(maintenant - timedelta(days=1)).isoformat()),
            content_type='application/json', **self.jetons,
        )
        self.assertEqual(reponse.status_code, 400)

    def test_programmee_puis_suppression(self):
        maintenant = timezone.now()
        self.client.post(
            reverse('ads_admin:staff-campagnes'),
            self.donnees(starts_at=(maintenant + timedelta(days=2)).isoformat()),
            content_type='application/json', **self.jetons,
        )
        campagne = CampagnePublicitaire.objects.get()
        self.assertEqual(
            self.client.get(reverse('ads_admin:staff-campagnes'), **self.jetons).json()['results'][0]['status'],
            'PROGRAMMEE',
        )
        reponse = self.client.delete(
            reverse('ads_admin:staff-campagne-detail', kwargs={'id': campagne.id}), **self.jetons
        )
        self.assertEqual(reponse.status_code, 204)
        self.assertFalse(CampagnePublicitaire.objects.exists())
