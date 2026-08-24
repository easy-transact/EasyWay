from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import Appareil, Droits, Formule, Parametres, Utilisateur
from .tokens import email_verification_token

MOT_DE_PASSE = 'CorrectHorse9!'


def creer_utilisateur(email='user@easyway.local', **extra):
    utilisateur = Utilisateur.objects.create_user(
        email=email, password=MOT_DE_PASSE, nom_complet='Test User', **extra
    )
    Parametres.objects.create(utilisateur=utilisateur)
    return utilisateur


def connecter(client, email, mot_de_passe=MOT_DE_PASSE):
    """Connexion + en-tete d'auth prets a l'emploi. Vide le cache de throttle
    avant chaque appel : ScopedRateThrottle partage un cache process-wide que
    les TestCase Django ne remettent pas a zero (seule la DB est rollback),
    donc une suite qui enchaine >20 connexions declenche sinon un 429 a tort."""
    cache.clear()
    reponse = client.post(
        reverse('accounts:connexion'),
        {'email': email, 'mot_de_passe': mot_de_passe},
        content_type='application/json',
    )
    return {'HTTP_AUTHORIZATION': f"Bearer {reponse.json()['jetons']['acces']}"}


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
            'email': 'nouveau@easyway.local',
            'nom_complet': 'Nouveau Utilisateur',
            'mot_de_passe': MOT_DE_PASSE,
            'confirmation_mot_de_passe': MOT_DE_PASSE,
            'accepte_cgu': True,
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
        self.assertEqual(reponse.json()['utilisateur']['droits']['formule'], 'GRATUITE')

    def test_inscription_retourne_des_jetons_immediatement(self):
        reponse = self.client.post(reverse('accounts:inscription'), self._payload(), content_type='application/json')
        self.assertIn('acces', reponse.json()['jetons'])
        self.assertIn('rafraichissement', reponse.json()['jetons'])

    def test_email_deja_utilise_rejete(self):
        creer_utilisateur('nouveau@easyway.local')
        reponse = self.client.post(reverse('accounts:inscription'), self._payload(), content_type='application/json')
        self.assertEqual(reponse.status_code, 400)

    def test_mots_de_passe_differents_rejetes(self):
        reponse = self.client.post(
            reverse('accounts:inscription'),
            self._payload(confirmation_mot_de_passe='autre-chose'),
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 400)

    def test_cgu_non_acceptees_rejetees(self):
        reponse = self.client.post(
            reverse('accounts:inscription'), self._payload(accepte_cgu=False), content_type='application/json'
        )
        self.assertEqual(reponse.status_code, 400)


class ConnexionTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()

    def test_connexion_reussie(self):
        cache.clear()
        reponse = self.client.post(
            reverse('accounts:connexion'),
            {'email': self.utilisateur.email, 'mot_de_passe': MOT_DE_PASSE},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertIn('acces', reponse.json()['jetons'])

    def test_mauvais_mot_de_passe_rejete(self):
        cache.clear()
        reponse = self.client.post(
            reverse('accounts:connexion'),
            {'email': self.utilisateur.email, 'mot_de_passe': 'incorrect'},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 400)

    def test_compte_banni_rejete(self):
        self.utilisateur.est_banni = True
        self.utilisateur.save(update_fields=['est_banni'])
        cache.clear()
        reponse = self.client.post(
            reverse('accounts:connexion'),
            {'email': self.utilisateur.email, 'mot_de_passe': MOT_DE_PASSE},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 400)


class RafraichirTests(TestCase):
    """RafraichirSerializer : /auth/rafraichir/ doit parler la meme convention
    (acces/rafraichissement) que connexion/inscription, pas access/refresh --
    incoherence relevee par l'integration frontend."""

    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()
        reponse = self.client.post(
            reverse('accounts:connexion'),
            {'email': self.utilisateur.email, 'mot_de_passe': MOT_DE_PASSE},
            content_type='application/json',
        )
        self.jetons = reponse.json()['jetons']

    def test_accepte_rafraichissement_et_renvoie_acces(self):
        reponse = self.client.post(
            reverse('accounts:rafraichir'),
            {'rafraichissement': self.jetons['rafraichissement']},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 200)
        corps = reponse.json()
        self.assertIn('acces', corps)
        self.assertIn('rafraichissement', corps)
        self.assertNotIn('access', corps)
        self.assertNotIn('refresh', corps)

    def test_champ_access_anglais_est_rejete(self):
        reponse = self.client.post(
            reverse('accounts:rafraichir'),
            {'refresh': self.jetons['rafraichissement']},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 400)

    def test_ancien_jeton_rafraichissement_est_mis_sur_liste_noire(self):
        """ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION (cf. SIMPLE_JWT) :
        le jeton reutilise apres rotation doit etre refuse."""
        self.client.post(
            reverse('accounts:rafraichir'),
            {'rafraichissement': self.jetons['rafraichissement']},
            content_type='application/json',
        )
        reponse = self.client.post(
            reverse('accounts:rafraichir'),
            {'rafraichissement': self.jetons['rafraichissement']},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 401)


class VerificationEmailTests(TestCase):
    def test_lien_valide_marque_email_verifie(self):
        utilisateur = creer_utilisateur()
        uid = urlsafe_base64_encode(force_bytes(str(utilisateur.pk)))
        jeton = email_verification_token.make_token(utilisateur)

        reponse = self.client.get(
            reverse('accounts:verifier-email', kwargs={'uidb64': uid, 'jeton': jeton})
        )
        self.assertEqual(reponse.status_code, 200)
        utilisateur.refresh_from_db()
        self.assertTrue(utilisateur.email_verifie)

    def test_lien_invalide_rejete(self):
        utilisateur = creer_utilisateur()
        uid = urlsafe_base64_encode(force_bytes(str(utilisateur.pk)))
        reponse = self.client.get(
            reverse('accounts:verifier-email', kwargs={'uidb64': uid, 'jeton': 'jeton-invalide'})
        )
        self.assertEqual(reponse.status_code, 400)


class ReinitialisationMotDePasseTests(TestCase):
    def test_confirmation_reinitialise_le_mot_de_passe(self):
        utilisateur = creer_utilisateur()
        uid = urlsafe_base64_encode(force_bytes(str(utilisateur.pk)))
        jeton = default_token_generator.make_token(utilisateur)

        reponse = self.client.post(
            reverse('accounts:mot-de-passe-confirmer'),
            {'uid': uid, 'jeton': jeton, 'nouveau_mot_de_passe': 'NouveauMotDePasse9!'},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 200)
        utilisateur.refresh_from_db()
        self.assertTrue(utilisateur.check_password('NouveauMotDePasse9!'))

    def test_demande_reinitialisation_reste_neutre_pour_email_inconnu(self):
        reponse = self.client.post(
            reverse('accounts:mot-de-passe-reinitialiser'),
            {'email': 'inconnu@easyway.local'},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 200)


class CompteAuthentifieTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.email)

    def test_moi_non_authentifie_rejete(self):
        reponse = self.client.get(reverse('accounts:moi'))
        self.assertEqual(reponse.status_code, 401)

    def test_moi_authentifie(self):
        reponse = self.client.get(reverse('accounts:moi'), **self.jetons)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['email'], self.utilisateur.email)

    def test_moi_patch_partiel(self):
        reponse = self.client.patch(
            reverse('accounts:moi'), {'ville': 'Douala'}, content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 200)
        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.ville, 'Douala')

    def test_moi_patch_ignore_champs_non_autorises(self):
        reponse = self.client.patch(
            reverse('accounts:moi'), {'formule': 'PREMIUM'}, content_type='application/json', **self.jetons
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
        self.assertIn('notif_alertes_police', reponse.json())

    def test_parametres_patch(self):
        reponse = self.client.patch(
            reverse('accounts:moi-parametres'),
            {'notif_nouveautes': False},
            content_type='application/json',
            **self.jetons,
        )
        self.assertEqual(reponse.status_code, 200)
        self.utilisateur.parametres.refresh_from_db()
        self.assertFalse(self.utilisateur.parametres.notif_nouveautes)

    def test_statistiques_stub(self):
        reponse = self.client.get(reverse('accounts:moi-statistiques'), **self.jetons)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(reponse.json()['trajets_completes'], 0)

    def test_appareil_creation_puis_upsert(self):
        payload = {
            'jeton_push': 'ExponentPushToken[abc]',
            'plateforme': 'ANDROID',
            'version_application': '1.0.0',
            'version_systeme': '14',
        }
        reponse = self.client.post(
            reverse('accounts:appareils'), payload, content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 201)
        self.assertEqual(Appareil.objects.filter(utilisateur=self.utilisateur).count(), 1)

        # Meme jeton_push -> upsert, pas de doublon.
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
        self.assertIn('villes', corps)
        self.assertIn('types_vehicule', corps)
        self.assertIn('types_incident', corps)
        self.assertIn('version_minimale_app', corps)
