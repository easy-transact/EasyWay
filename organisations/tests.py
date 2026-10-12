import re
from datetime import timedelta

from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import InscriptionListeAttente, ProfilListeAttente, Utilisateur
from accounts.tests import MOT_DE_PASSE, connecter, creer_utilisateur, numero_telephone_test

from .models import (
    Chauffeur,
    JournalOrganisation,
    MembreOrganisation,
    Organisation,
    RoleOrganisation,
    StatutFiche,
    StatutOrganisation,
    StatutVehicule,
    Vehicule,
)

_compteur_email = iter(range(10**6))


def email_unique():
    return f'membre{next(_compteur_email)}@agence.test'


def creer_organisation(nom='Touristique Express', statut=StatutOrganisation.ACTIVE, **extra):
    return Organisation.objects.create(
        type=extra.pop('type', 'VOYAGE'), raison_sociale=nom, ville='Douala', statut=statut, **extra,
    )


def ajouter_membre(organisation, role):
    utilisateur = creer_utilisateur(email=email_unique())
    MembreOrganisation.objects.create(
        organisation=organisation, utilisateur=utilisateur, role=role, accepte_le=timezone.now(),
    )
    return utilisateur


class OrganisationTestCase(TestCase):
    def jetons(self, utilisateur):
        return connecter(self.client, utilisateur.telephone)

    def get(self, nom, utilisateur, *args, **params):
        return self.client.get(reverse(f'organisations:{nom}', args=args), params, **self.jetons(utilisateur))

    def envoyer(self, methode, nom, utilisateur, *args, corps=None):
        return getattr(self.client, methode)(
            reverse(f'organisations:{nom}', args=args), corps or {}, content_type='application/json',
            **self.jetons(utilisateur),
        )


class CreationParLeStaffTests(OrganisationTestCase):
    def setUp(self):
        self.staff = creer_utilisateur(email='staff@easyway.local', is_staff=True)

    def _creer(self, **corps):
        donnees = {
            'type': 'VOYAGE', 'legal_name': 'Buca Voyages SARL', 'city': 'Douala',
            'owner': {'full_name': 'Awa Ngono', 'phone': '677 99 88 77', 'email': 'Awa@Buca.CM'},
        }
        donnees.update(corps)
        with self.captureOnCommitCallbacks(execute=True):
            return self.envoyer('post', 'staff-organisations', self.staff, corps=donnees)

    def test_nouveau_proprietaire_recoit_un_lien_d_activation(self):
        reponse = self._creer()
        self.assertEqual(reponse.status_code, 201, reponse.content)
        organisation = Organisation.objects.get()
        self.assertEqual(organisation.statut, StatutOrganisation.EN_ATTENTE)
        self.assertEqual(organisation.cree_par, self.staff)

        membre = MembreOrganisation.objects.get(organisation=organisation)
        self.assertEqual(membre.role, RoleOrganisation.PROPRIETAIRE)
        self.assertIsNone(membre.accepte_le)
        self.assertEqual(membre.utilisateur.telephone, '+237677998877')
        self.assertEqual(membre.utilisateur.email, 'Awa@buca.cm')
        self.assertFalse(membre.utilisateur.has_usable_password())

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['Awa@buca.cm'])
        self.assertIn('/activer?uid=', mail.outbox[0].body)
        self.assertTrue(JournalOrganisation.objects.filter(organisation=organisation, action='organisation.creee').exists())

    def test_compte_existant_rattache_et_organisation_active(self):
        existant = creer_utilisateur(email='deja@easyway.local')
        reponse = self._creer(owner={'full_name': 'X', 'phone': existant.telephone, 'email': 'deja@easyway.local'})
        self.assertEqual(reponse.status_code, 201, reponse.content)
        self.assertEqual(reponse.json()['status'], StatutOrganisation.ACTIVE)
        membre = existant.membre_organisation
        self.assertIsNotNone(membre.accepte_le)
        self.assertTrue(Utilisateur.objects.get(pk=existant.pk).check_password(MOT_DE_PASSE))

    def test_telephone_et_email_de_deux_comptes_differents_refuses_sans_rien_creer(self):
        a = creer_utilisateur(email='a@easyway.local')
        creer_utilisateur(email='b@easyway.local')
        reponse = self._creer(owner={'full_name': 'X', 'phone': a.telephone, 'email': 'b@easyway.local'})
        self.assertEqual(reponse.status_code, 400)
        self.assertFalse(Organisation.objects.exists())

    def test_utilisateur_deja_membre_d_une_autre_organisation_refuse(self):
        deja = ajouter_membre(creer_organisation(), RoleOrganisation.ADMIN)
        reponse = self._creer(owner={'full_name': 'X', 'phone': deja.telephone, 'email': deja.email})
        self.assertEqual(reponse.status_code, 400)
        self.assertEqual(Organisation.objects.count(), 1)

    def test_conversion_d_une_inscription_de_liste_d_attente(self):
        inscription = InscriptionListeAttente.objects.create(
            nom_complet='Buca', telephone=numero_telephone_test(), profil=ProfilListeAttente.AGENCE_VOYAGE,
        )
        self.assertEqual(self._creer(waitlist_id=str(inscription.id)).status_code, 201)
        inscription.refresh_from_db()
        self.assertTrue(inscription.contacte)
        self.assertEqual(inscription.organisation.raison_sociale, 'Buca Voyages SARL')

        deuxieme = self._creer(
            waitlist_id=str(inscription.id),
            owner={'full_name': 'Y', 'phone': numero_telephone_test(), 'email': email_unique()},
        )
        self.assertEqual(deuxieme.status_code, 400)

    def test_reserve_au_staff(self):
        self.assertEqual(self.envoyer('post', 'staff-organisations', creer_utilisateur(email='n@e.l')).status_code, 403)

    def test_suspension_bloque_le_portail_puis_reactivation(self):
        organisation = creer_organisation()
        membre = ajouter_membre(organisation, RoleOrganisation.PROPRIETAIRE)
        self.assertEqual(self.envoyer('post', 'staff-organisation-suspendre', self.staff, organisation.id).status_code, 200)

        reponse = self.get('moi', membre)
        self.assertEqual(reponse.status_code, 403)
        self.assertIn('suspended', reponse.json()['detail'])

        reponse = self.envoyer('post', 'staff-organisation-reactiver', self.staff, organisation.id)
        self.assertEqual(reponse.json()['status'], StatutOrganisation.ACTIVE)
        self.assertEqual(self.get('moi', membre).status_code, 200)


class AcceptationInvitationTests(OrganisationTestCase):
    def setUp(self):
        staff = creer_utilisateur(email='staff@easyway.local', is_staff=True)
        with self.captureOnCommitCallbacks(execute=True):
            self.envoyer('post', 'staff-organisations', staff, corps={
                'type': 'FLOTTE', 'legal_name': 'Flotte SARL', 'city': 'Yaounde',
                'owner': {'full_name': 'Paul', 'phone': '677112233', 'email': 'paul@flotte.cm'},
            })
        self.uid, self.token = re.search(r'uid=([^&]+)&token=(\S+)', mail.outbox[0].body).groups()

    def _accepter(self, token=None, mot_de_passe='Un-Mot-De-Passe-Solide-42'):
        return self.client.post(
            reverse('organisations:accepter-invitation'),
            {'uid': self.uid, 'token': token or self.token, 'new_password': mot_de_passe},
            content_type='application/json',
        )

    def test_activation_connecte_et_active_l_organisation(self):
        reponse = self._accepter()
        self.assertEqual(reponse.status_code, 200, reponse.content)
        self.assertIn('access', reponse.json()['tokens'])
        self.assertEqual(reponse.json()['organisation']['status'], StatutOrganisation.ACTIVE)

        connexion = self.client.post(
            reverse('accounts:connexion'), {'phone': '677112233', 'password': 'Un-Mot-De-Passe-Solide-42'},
            content_type='application/json',
        )
        self.assertEqual(connexion.status_code, 200)

    def test_lien_a_usage_unique(self):
        self.assertEqual(self._accepter().status_code, 200)
        self.assertEqual(self._accepter(mot_de_passe='Autre-Mot-De-Passe-99').status_code, 400)

    def test_jeton_invalide_ou_mot_de_passe_faible_refuses(self):
        self.assertEqual(self._accepter(token='faux-jeton').status_code, 400)
        self.assertEqual(self._accepter(mot_de_passe='123').status_code, 400)
        self.assertEqual(Organisation.objects.get().statut, StatutOrganisation.EN_ATTENTE)


class PortailRolesEtIsolementTests(OrganisationTestCase):
    def setUp(self):
        self.agence = creer_organisation()
        self.proprietaire = ajouter_membre(self.agence, RoleOrganisation.PROPRIETAIRE)
        self.admin = ajouter_membre(self.agence, RoleOrganisation.ADMIN)
        self.gestionnaire = ajouter_membre(self.agence, RoleOrganisation.GESTIONNAIRE)
        self.lecteur = ajouter_membre(self.agence, RoleOrganisation.LECTEUR)

        self.autre = creer_organisation('General Express')
        self.autre_admin = ajouter_membre(self.autre, RoleOrganisation.ADMIN)
        self.chauffeur_autre = Chauffeur.objects.create(organisation=self.autre, nom_complet='Chauffeur B')

    def test_moi_renvoie_organisation_et_role(self):
        corps = self.get('moi', self.gestionnaire).json()
        self.assertEqual(corps['role'], RoleOrganisation.GESTIONNAIRE)
        self.assertEqual(corps['organisation']['id'], str(self.agence.id))

    def test_non_membre_refuse(self):
        self.assertEqual(self.get('moi', creer_utilisateur(email='seul@e.l')).status_code, 403)

    def test_une_agence_ne_voit_pas_les_fiches_d_une_autre(self):
        self.assertEqual(self.get('chauffeur-detail', self.admin, self.chauffeur_autre.id).status_code, 404)
        self.assertEqual(self.get('chauffeurs', self.admin).json()['count'], 0)

        vehicule = Vehicule.objects.create(organisation=self.agence, immatriculation='LT1', type='BUS')
        reponse = self.envoyer('patch', 'vehicule-detail', self.admin, vehicule.id,
                               corps={'driver_id': str(self.chauffeur_autre.id)})
        self.assertEqual(reponse.status_code, 400)

    def test_lecteur_lit_mais_n_ecrit_pas(self):
        self.assertEqual(self.get('chauffeurs', self.lecteur).status_code, 200)
        reponse = self.envoyer('post', 'chauffeurs', self.lecteur, corps={'full_name': 'X'})
        self.assertEqual(reponse.status_code, 403)
        self.assertIn('GESTIONNAIRE', reponse.json()['detail'])

    def test_gestionnaire_gere_les_fiches_mais_pas_l_equipe(self):
        self.assertEqual(self.envoyer('post', 'chauffeurs', self.gestionnaire, corps={'full_name': 'X'}).status_code, 201)
        invitation = {'full_name': 'Y', 'phone': numero_telephone_test(), 'email': email_unique(), 'role': 'LECTEUR'}
        self.assertEqual(self.envoyer('post', 'membres', self.gestionnaire, corps=invitation).status_code, 403)
        self.assertEqual(self.envoyer('patch', 'profil', self.gestionnaire, corps={'city': 'Kribi'}).status_code, 403)

    def test_admin_invite_un_membre(self):
        invitation = {'full_name': 'Y', 'phone': numero_telephone_test(), 'email': email_unique(), 'role': 'GESTIONNAIRE'}
        with self.captureOnCommitCallbacks(execute=True):
            reponse = self.envoyer('post', 'membres', self.admin, corps=invitation)
        self.assertEqual(reponse.status_code, 201, reponse.content)
        self.assertIsNone(reponse.json()['accepted_at'])
        self.assertEqual(len(mail.outbox), 1)

        invitation['role'] = 'PROPRIETAIRE'
        invitation['phone'], invitation['email'] = numero_telephone_test(), email_unique()
        self.assertEqual(self.envoyer('post', 'membres', self.admin, corps=invitation).status_code, 400)

    def test_le_proprietaire_ne_peut_etre_ni_modifie_ni_retire_par_un_admin(self):
        id_proprietaire = self.proprietaire.membre_organisation.id
        self.assertEqual(self.envoyer('patch', 'membre-detail', self.admin, id_proprietaire,
                                      corps={'role': 'LECTEUR'}).status_code, 400)
        self.assertEqual(self.envoyer('delete', 'membre-detail', self.admin, id_proprietaire).status_code, 400)
        self.assertEqual(self.envoyer('post', 'transferer-propriete', self.admin,
                                      self.lecteur.membre_organisation.id).status_code, 403)

    def test_transfert_de_propriete(self):
        cible = self.admin.membre_organisation
        reponse = self.envoyer('post', 'transferer-propriete', self.proprietaire, cible.id)
        self.assertEqual(reponse.status_code, 200)
        cible.refresh_from_db()
        self.assertEqual(cible.role, RoleOrganisation.PROPRIETAIRE)
        self.assertEqual(MembreOrganisation.objects.get(utilisateur=self.proprietaire).role, RoleOrganisation.ADMIN)

    def test_un_admin_ne_peut_pas_toucher_un_membre_d_une_autre_agence(self):
        autre_membre = self.autre_admin.membre_organisation.id
        self.assertEqual(self.envoyer('delete', 'membre-detail', self.admin, autre_membre).status_code, 404)

    def test_journal_reserve_aux_admins(self):
        self.envoyer('post', 'chauffeurs', self.gestionnaire, corps={'full_name': 'Z'})
        self.assertEqual(self.get('journal', self.lecteur).status_code, 403)
        actions = [e['action'] for e in self.get('journal', self.admin).json()['results']]
        self.assertIn('chauffeur.cree', actions)


class ChauffeursVehiculesTests(OrganisationTestCase):
    def setUp(self):
        self.agence = creer_organisation()
        self.gestionnaire = ajouter_membre(self.agence, RoleOrganisation.GESTIONNAIRE)

    def test_immatriculation_normalisee_et_unique_dans_l_agence(self):
        reponse = self.envoyer('post', 'vehicules', self.gestionnaire, corps={'plate': 'lt 123-ab', 'type': 'BUS'})
        self.assertEqual(reponse.status_code, 201, reponse.content)
        self.assertEqual(reponse.json()['plate'], 'LT123AB')
        doublon = self.envoyer('post', 'vehicules', self.gestionnaire, corps={'plate': 'LT-123 AB', 'type': 'BUS'})
        self.assertEqual(doublon.status_code, 400)

        autre = ajouter_membre(creer_organisation('Autre'), RoleOrganisation.GESTIONNAIRE)
        self.assertEqual(self.envoyer('post', 'vehicules', autre, corps={'plate': 'LT123AB', 'type': 'BUS'}).status_code, 201)

    def test_affectation_puis_archivage_du_chauffeur(self):
        chauffeur = self.envoyer('post', 'chauffeurs', self.gestionnaire,
                                 corps={'full_name': 'Jean', 'phone': '677445566'}).json()
        self.assertEqual(chauffeur['phone'], '+237677445566')
        vehicule = self.envoyer('post', 'vehicules', self.gestionnaire,
                                corps={'plate': 'CE001', 'type': 'BUS', 'driver_id': chauffeur['id']}).json()
        self.assertEqual(vehicule['driver_name'], 'Jean')
        self.assertEqual(self.get('chauffeur-detail', self.gestionnaire, chauffeur['id']).json()['vehicles'], ['CE001'])

        self.assertEqual(self.envoyer('delete', 'chauffeur-detail', self.gestionnaire, chauffeur['id']).status_code, 204)
        self.assertEqual(Chauffeur.objects.get(id=chauffeur['id']).statut, StatutFiche.ARCHIVE)
        self.assertIsNone(Vehicule.objects.get(id=vehicule['id']).chauffeur)
        self.assertEqual(self.get('chauffeurs', self.gestionnaire).json()['count'], 0)
        self.assertEqual(self.get('chauffeur-detail', self.gestionnaire, chauffeur['id']).status_code, 404)

    def test_archivage_d_un_vehicule(self):
        vehicule = Vehicule.objects.create(organisation=self.agence, immatriculation='CE002', type='BUS')
        self.assertEqual(self.envoyer('delete', 'vehicule-detail', self.gestionnaire, vehicule.id).status_code, 204)
        vehicule.refresh_from_db()
        self.assertEqual(vehicule.statut, StatutVehicule.ARCHIVE)
        self.assertEqual(self.get('vehicules', self.gestionnaire).json()['count'], 0)

    def test_tableau_de_bord_et_echeances(self):
        aujourd_hui = timezone.localdate()
        Chauffeur.objects.create(organisation=self.agence, nom_complet='Bientot', permis_expire_le=aujourd_hui + timedelta(days=10))
        Chauffeur.objects.create(organisation=self.agence, nom_complet='Loin', permis_expire_le=aujourd_hui + timedelta(days=300))
        Vehicule.objects.create(
            organisation=self.agence, immatriculation='CE003', type='BUS',
            assurance_expire_le=aujourd_hui - timedelta(days=2), visite_technique_expire_le=aujourd_hui + timedelta(days=400),
        )
        corps = self.get('tableau-de-bord', self.gestionnaire).json()
        self.assertEqual(corps['drivers']['ACTIF'], 2)
        self.assertEqual(corps['vehicles']['ACTIF'], 1)
        self.assertEqual([(e['kind'], e['label'], e['expired']) for e in corps['deadlines']],
                         [('INSURANCE', 'CE003', True), ('LICENSE', 'Bientot', False)])
