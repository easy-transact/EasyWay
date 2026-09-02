import itertools

from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import Appareil, Droits, Formule, Parametres, Utilisateur
from .tokens import email_verification_token

MOT_DE_PASSE = 'CorrectHorse9!'

# '677' est un prefixe mobile camerounais valide pour phonenumbers (cf.
# accounts.utils) sur toute la plage 000000-999999 -- suffisant pour generer
# un numero unique et syntaxiquement valide par utilisateur de test, sans
# reutiliser l'email (desormais rejete par ConnexionSerializer : ce n'est
# plus un numero de telephone valide).
_compteur_telephone_test = itertools.count(1)


def numero_telephone_test() -> str:
    return f'+237677{next(_compteur_telephone_test):06d}'


def creer_utilisateur(email='user@easyway.local', **extra):
    telephone = extra.pop('telephone', None) or numero_telephone_test()
    utilisateur = Utilisateur.objects.create_user(
        telephone=telephone, email=email, password=MOT_DE_PASSE, nom_complet='Test User', **extra
    )
    Parametres.objects.create(utilisateur=utilisateur)
    return utilisateur


def connecter(client, identifiant, mot_de_passe=MOT_DE_PASSE):
    """Connexion + en-tete d'auth prets a l'emploi. Vide le cache de throttle
    avant chaque appel : ScopedRateThrottle partage un cache process-wide que
    les TestCase Django ne remettent pas a zero (seule la DB est rollback),
    donc une suite qui enchaine >20 connexions declenche sinon un 429 a tort."""
    cache.clear()
    reponse = client.post(
        reverse('accounts:connexion'),
        {'phone': identifiant, 'password': mot_de_passe},
        content_type='application/json',
    )
    return {'HTTP_AUTHORIZATION': f"Bearer {reponse.json()['tokens']['access']}"}


class DroitsTests(TestCase):
    """Droits est une table de reference par formule (cf. accounts/models.py),
    pas une ligne par utilisateur : ces tests verifient que ca reste vrai."""

    def test_seed_migration_cree_les_deux_formules(self):
        self.assertEqual(Droits.objects.count(), 2)
        self.assertTrue(Droits.objects.filter(formule=Formule.GRATUITE).exists())
        self.assertTrue(Droits.objects.filter(formule=Formule.PREMIUM).exists())

    def test_gratuite_limite_cinq_adresses(self):
        droits = Droits.objects.get(formule=Formule.GRATUITE)
        self.assertEqual(droits.max_adresses_enregistrees, 5)

    def test_premium_illimite(self):
        droits = Droits.objects.get(formule=Formule.PREMIUM)
        self.assertIsNone(droits.max_adresses_enregistrees)

    def test_deux_utilisateurs_meme_formule_partagent_la_ligne(self):
        u1 = creer_utilisateur('a@easyway.local')
        u2 = creer_utilisateur('b@easyway.local')
        self.assertEqual(u1.droits.pk, u2.droits.pk)

    def test_changer_la_limite_gratuite_impacte_tous_les_gratuits_immediatement(self):
        u1 = creer_utilisateur('a@easyway.local')
        u2 = creer_utilisateur('b@easyway.local')
        Droits.objects.filter(formule=Formule.GRATUITE).update(max_adresses_enregistrees=10)
        self.assertEqual(u1.droits.max_adresses_enregistrees, 10)
        self.assertEqual(u2.droits.max_adresses_enregistrees, 10)


class InscriptionTests(TestCase):
    def _payload(self, **overrides):
        payload = {
            'phone': numero_telephone_test(),
            'email': 'nouveau@easyway.local',
            'full_name': 'Nouveau Utilisateur',
            'password': MOT_DE_PASSE,
            'password_confirmation': MOT_DE_PASSE,
            'accepts_terms': True,
        }
        payload.update(overrides)
        return payload

    def test_inscription_cree_compte_et_parametres_pas_de_droits_par_utilisateur(self):
        nb_droits_avant = Droits.objects.count()
        reponse = self.client.post(reverse('accounts:inscription'), self._payload(), content_type='application/json')
        self.assertEqual(reponse.status_code, 201)

        utilisateur = Utilisateur.objects.get(email='nouveau@easyway.local')
        self.assertTrue(Parametres.objects.filter(utilisateur=utilisateur).exists())
        # L'inscription ne doit pas creer de nouvelle ligne Droits.
        self.assertEqual(Droits.objects.count(), nb_droits_avant)
        self.assertEqual(reponse.json()['user']['plan_limits']['plan'], 'GRATUITE')

    def test_inscription_retourne_des_jetons_immediatement(self):
        reponse = self.client.post(reverse('accounts:inscription'), self._payload(), content_type='application/json')
        self.assertIn('access', reponse.json()['tokens'])
        self.assertIn('refresh', reponse.json()['tokens'])

    def test_email_deja_utilise_rejete(self):
        creer_utilisateur('nouveau@easyway.local')
        reponse = self.client.post(reverse('accounts:inscription'), self._payload(), content_type='application/json')
        self.assertEqual(reponse.status_code, 400)

    def test_mots_de_passe_differents_rejetes(self):
        reponse = self.client.post(
            reverse('accounts:inscription'),
            self._payload(password_confirmation='autre-chose'),
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 400)

    def test_cgu_non_acceptees_rejetees(self):
        reponse = self.client.post(
            reverse('accounts:inscription'), self._payload(accepts_terms=False), content_type='application/json'
        )
        self.assertEqual(reponse.status_code, 400)


class ConnexionTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()

    def test_connexion_reussie(self):
        cache.clear()
        reponse = self.client.post(
            reverse('accounts:connexion'),
            {'phone': self.utilisateur.telephone, 'password': MOT_DE_PASSE},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertIn('access', reponse.json()['tokens'])

    def test_mauvais_mot_de_passe_rejete(self):
        cache.clear()
        reponse = self.client.post(
            reverse('accounts:connexion'),
            {'phone': self.utilisateur.telephone, 'password': 'incorrect'},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 400)

    def test_compte_banni_rejete(self):
        self.utilisateur.est_banni = True
        self.utilisateur.save(update_fields=['est_banni'])
        cache.clear()
        reponse = self.client.post(
            reverse('accounts:connexion'),
            {'phone': self.utilisateur.telephone, 'password': MOT_DE_PASSE},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 400)


class RafraichirTests(TestCase):
    """/auth/refresh/ : vue SimpleJWT stock, access/refresh -- desormais la
    meme convention que le reste du module (traduction de la surface API)."""

    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()
        reponse = self.client.post(
            reverse('accounts:connexion'),
            {'phone': self.utilisateur.telephone, 'password': MOT_DE_PASSE},
            content_type='application/json',
        )
        self.jetons = reponse.json()['tokens']

    def test_refresh_renvoie_un_nouvel_access(self):
        reponse = self.client.post(
            reverse('accounts:rafraichir'),
            {'refresh': self.jetons['refresh']},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 200)
        corps = reponse.json()
        self.assertIn('access', corps)
        self.assertIn('refresh', corps)

    def test_ancien_jeton_rafraichissement_est_mis_sur_liste_noire(self):
        """ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION (cf. SIMPLE_JWT) :
        le jeton reutilise apres rotation doit etre refuse."""
        self.client.post(
            reverse('accounts:rafraichir'),
            {'refresh': self.jetons['refresh']},
            content_type='application/json',
        )
        reponse = self.client.post(
            reverse('accounts:rafraichir'),
            {'refresh': self.jetons['refresh']},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 401)


class VerificationEmailTests(TestCase):
    def test_lien_valide_marque_email_verifie(self):
        utilisateur = creer_utilisateur()
        uid = urlsafe_base64_encode(force_bytes(str(utilisateur.pk)))
        jeton = email_verification_token.make_token(utilisateur)

        reponse = self.client.get(
            reverse('accounts:verifier-email', kwargs={'uidb64': uid, 'token': jeton})
        )
        self.assertEqual(reponse.status_code, 200)
        utilisateur.refresh_from_db()
        self.assertTrue(utilisateur.email_verifie)

    def test_lien_invalide_rejete(self):
        utilisateur = creer_utilisateur()
        uid = urlsafe_base64_encode(force_bytes(str(utilisateur.pk)))
        reponse = self.client.get(
            reverse('accounts:verifier-email', kwargs={'uidb64': uid, 'token': 'jeton-invalide'})
        )
        self.assertEqual(reponse.status_code, 400)


class ReinitialisationMotDePasseTests(TestCase):
    def test_confirmation_reinitialise_le_mot_de_passe(self):
        utilisateur = creer_utilisateur()
        uid = urlsafe_base64_encode(force_bytes(str(utilisateur.pk)))
        jeton = default_token_generator.make_token(utilisateur)

        reponse = self.client.post(
            reverse('accounts:mot-de-passe-confirmer'),
            {'uid': uid, 'token': jeton, 'new_password': 'NouveauMotDePasse9!'},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 200)
        utilisateur.refresh_from_db()
        self.assertTrue(utilisateur.check_password('NouveauMotDePasse9!'))

    def test_demande_reinitialisation_reste_neutre_pour_numero_inconnu(self):
        reponse = self.client.post(
            reverse('accounts:mot-de-passe-reinitialiser'),
            {'phone': numero_telephone_test()},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 200)


class CompteAuthentifieTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.telephone)

    def test_moi_non_authentifie_rejete(self):
        reponse = self.client.get(reverse('accounts:moi'))
        self.assertEqual(reponse.status_code, 401)

    def test_moi_authentifie(self):
        reponse = self.client.get(reverse('accounts:moi'), **self.jetons)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['email'], self.utilisateur.email)

    def test_moi_patch_partiel(self):
        reponse = self.client.patch(
            reverse('accounts:moi'), {'city': 'Douala'}, content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 200)
        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.ville, 'Douala')

    def test_moi_patch_ignore_champs_non_autorises(self):
        reponse = self.client.patch(
            reverse('accounts:moi'), {'plan': 'PREMIUM'}, content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 200)
        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.formule, Formule.GRATUITE)

    def test_moi_delete_marque_suppression_sans_effacer(self):
        reponse = self.client.delete(reverse('accounts:moi'), **self.jetons)
        self.assertEqual(reponse.status_code, 204)
        self.utilisateur.refresh_from_db()
        self.assertIsNotNone(self.utilisateur.suppression_demandee_le)

    def test_parametres_get(self):
        reponse = self.client.get(reverse('accounts:moi-parametres'), **self.jetons)
        self.assertEqual(reponse.status_code, 200)
        self.assertIn('notify_police_alerts', reponse.json())

    def test_parametres_patch(self):
        reponse = self.client.patch(
            reverse('accounts:moi-parametres'),
            {'notify_news': False},
            content_type='application/json',
            **self.jetons,
        )
        self.assertEqual(reponse.status_code, 200)
        self.utilisateur.parametres.refresh_from_db()
        self.assertFalse(self.utilisateur.parametres.notif_nouveautes)

    def test_statistiques_stub(self):
        reponse = self.client.get(reverse('accounts:moi-statistiques'), **self.jetons)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['completed_trips'], 0)

    def test_appareil_creation_puis_upsert(self):
        payload = {
            'push_token': 'ExponentPushToken[abc]',
            'platform': 'ANDROID',
            'app_version': '1.0.0',
            'os_version': '14',
        }
        reponse = self.client.post(
            reverse('accounts:appareils'), payload, content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 201)
        self.assertEqual(Appareil.objects.filter(utilisateur=self.utilisateur).count(), 1)

        # Meme push_token -> upsert, pas de doublon.
        reponse = self.client.post(
            reverse('accounts:appareils'), payload, content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 201)
        self.assertEqual(Appareil.objects.filter(utilisateur=self.utilisateur).count(), 1)

    def test_appareil_suppression(self):
        appareil = Appareil.objects.create(
            utilisateur=self.utilisateur, jeton_push='xyz', plateforme='IOS',
            version_application='1.0.0', version_systeme='18',
        )
        reponse = self.client.delete(
            reverse('accounts:appareil-suppression', kwargs={'id': appareil.id}), **self.jetons
        )
        self.assertEqual(reponse.status_code, 204)
        self.assertFalse(Appareil.objects.filter(id=appareil.id).exists())

    def test_appareil_suppression_refuse_pour_autre_utilisateur(self):
        autre = creer_utilisateur('autre@easyway.local')
        appareil = Appareil.objects.create(
            utilisateur=autre, jeton_push='xyz', plateforme='IOS',
            version_application='1.0.0', version_systeme='18',
        )
        reponse = self.client.delete(
            reverse('accounts:appareil-suppression', kwargs={'id': appareil.id}), **self.jetons
        )
        self.assertEqual(reponse.status_code, 404)


class ConfigTests(TestCase):
    def test_config_public_sans_authentification(self):
        reponse = self.client.get(reverse('accounts:config'))
        self.assertEqual(reponse.status_code, 200)
        corps = reponse.json()
        self.assertIn('cities', corps)
        self.assertIn('vehicle_types', corps)
        self.assertIn('incident_types', corps)
        self.assertIn('minimum_app_version', corps)


class UtilisateurModerationApiTests(TestCase):
    def setUp(self):
        self.staff = creer_utilisateur('staff@easyway.local', is_staff=True)
        self.jetons_staff = connecter(self.client, self.staff.telephone)
        self.cible = creer_utilisateur('cible@easyway.local')

    def test_liste_reserve_au_staff_anonyme(self):
        reponse = self.client.get(reverse('accounts:staff-utilisateurs'))
        self.assertEqual(reponse.status_code, 401)

    def test_liste_reserve_au_staff_non_staff(self):
        non_staff = creer_utilisateur('simple@easyway.local')
        jetons = connecter(self.client, non_staff.telephone)
        reponse = self.client.get(reverse('accounts:staff-utilisateurs'), **jetons)
        self.assertEqual(reponse.status_code, 403)

    def test_recherche_filtre_par_telephone_ou_email(self):
        creer_utilisateur('autre@easyway.local', telephone='+237611110000')
        reponse = self.client.get(
            reverse('accounts:staff-utilisateurs'), {'search': 'cible'}, **self.jetons_staff
        )
        self.assertEqual(reponse.status_code, 200)
        emails = [r['email'] for r in reponse.json()['results']]
        self.assertEqual(emails, ['cible@easyway.local'])

    def test_bannir_definit_est_banni_et_expiration(self):
        url = reverse('accounts:staff-utilisateur-bannir', kwargs={'id': self.cible.id})
        reponse = self.client.post(
            url, {'until': '2030-01-01T00:00:00Z'}, content_type='application/json', **self.jetons_staff
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertTrue(reponse.json()['is_banned'])
        self.cible.refresh_from_db()
        self.assertTrue(self.cible.est_banni)
        self.assertIsNotNone(self.cible.banni_jusqu_a)

    def test_bannir_permanent_sans_until(self):
        url = reverse('accounts:staff-utilisateur-bannir', kwargs={'id': self.cible.id})
        reponse = self.client.post(url, {}, content_type='application/json', **self.jetons_staff)
        self.assertEqual(reponse.status_code, 200)
        self.cible.refresh_from_db()
        self.assertTrue(self.cible.est_banni)
        self.assertIsNone(self.cible.banni_jusqu_a)

    def test_bannir_refuse_sur_compte_staff(self):
        autre_staff = creer_utilisateur('staff2@easyway.local', is_staff=True)
        url = reverse('accounts:staff-utilisateur-bannir', kwargs={'id': autre_staff.id})
        reponse = self.client.post(url, {}, content_type='application/json', **self.jetons_staff)
        self.assertEqual(reponse.status_code, 400)
        autre_staff.refresh_from_db()
        self.assertFalse(autre_staff.est_banni)

    def test_debannir_reinitialise_les_champs(self):
        self.cible.bannir()
        url = reverse('accounts:staff-utilisateur-debannir', kwargs={'id': self.cible.id})
        reponse = self.client.post(url, **self.jetons_staff)
        self.assertEqual(reponse.status_code, 200)
        self.cible.refresh_from_db()
        self.assertFalse(self.cible.est_banni)
        self.assertIsNone(self.cible.banni_jusqu_a)

    def test_non_staff_recoit_403_sur_bannir(self):
        non_staff = creer_utilisateur('simple2@easyway.local')
        jetons = connecter(self.client, non_staff.telephone)
        url = reverse('accounts:staff-utilisateur-bannir', kwargs={'id': self.cible.id})
        reponse = self.client.post(url, {}, content_type='application/json', **jetons)
        self.assertEqual(reponse.status_code, 403)
