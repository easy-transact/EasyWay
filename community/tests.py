from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Parametres, Utilisateur
from accounts.tests import connecter

from .cache_incidents import cle_cache_cellule
from .models import Incident, StatutIncident, TypeIncident, Vote
from .services import QuotaDepasse, ServiceIncident
from .tasks import expirer_incidents

MOT_DE_PASSE = 'CorrectHorse9!'
DOUALA_LAT, DOUALA_LON = 4.0483, 9.7043


def creer_utilisateur(email='user@easyway.local', score_reputation=100, **extra):
    utilisateur = Utilisateur.objects.create_user(
        email=email, password=MOT_DE_PASSE, nom_complet='Test User',
        score_reputation=score_reputation, **extra,
    )
    Parametres.objects.create(utilisateur=utilisateur)
    return utilisateur


def creer_incident(auteur, type_incident=TypeIncident.EMBOUTEILLAGE, lat=DOUALA_LAT, lon=DOUALA_LON, **extra):
    valeurs = dict(
        auteur=auteur, type=type_incident, position=Point(lon, lat, srid=4326),
        statut=StatutIncident.ACTIF, expire_le=timezone.now() + timezone.timedelta(minutes=30),
    )
    valeurs.update(extra)
    return Incident.objects.create(**valeurs)


def patcher_nominatim_incident(test_case, libelle=None):
    patcheur = patch('community.services.ClientNominatim')
    classe_simulee = patcheur.start()
    classe_simulee.return_value.inverser.return_value = (
        {'label': libelle, 'source': 'nominatim'} if libelle else None
    )
    test_case.addCleanup(patcheur.stop)


class H3IndexationTests(TestCase):
    def test_cellules_calculees_a_la_creation(self):
        incident = creer_incident(creer_utilisateur())
        self.assertIsNotNone(incident.cellule_h3_res8)
        self.assertIsNotNone(incident.cellule_h3_res7)

    def test_cellules_stables_sur_les_sauvegardes_suivantes(self):
        incident = creer_incident(creer_utilisateur())
        cellule_initiale = incident.cellule_h3_res8
        incident.severite = 3
        incident.save(update_fields=['severite'])
        incident.refresh_from_db()
        self.assertEqual(incident.cellule_h3_res8, cellule_initiale)


class ServiceIncidentSignalerTests(TestCase):
    def setUp(self):
        patcher_nominatim_incident(self)
        cache.clear()

    def _point(self, decalage_lat=0, decalage_lon=0):
        return Point(DOUALA_LON + decalage_lon, DOUALA_LAT + decalage_lat, srid=4326)

    def test_utilisateur_reputation_normale_cree_actif(self):
        incident, doublon = ServiceIncident().signaler(
            creer_utilisateur(score_reputation=100), TypeIncident.EMBOUTEILLAGE, self._point()
        )
        self.assertFalse(doublon)
        self.assertEqual(incident.statut, StatutIncident.ACTIF)

    def test_utilisateur_faible_reputation_cree_en_attente(self):
        incident, _ = ServiceIncident().signaler(
            creer_utilisateur(score_reputation=10), TypeIncident.EMBOUTEILLAGE, self._point()
        )
        self.assertEqual(incident.statut, StatutIncident.EN_ATTENTE)

    def test_quota_horaire_depasse(self):
        utilisateur = creer_utilisateur()
        for i in range(10):
            creer_incident(utilisateur, type_incident=TypeIncident.DANGER, lat=DOUALA_LAT + i * 0.05)
        with self.assertRaises(QuotaDepasse):
            ServiceIncident().signaler(utilisateur, TypeIncident.EMBOUTEILLAGE, self._point())

    def test_doublon_meme_type_proche_corrobore_sans_creer(self):
        existant = creer_incident(creer_utilisateur('a@easyway.local'), type_incident=TypeIncident.EMBOUTEILLAGE)
        auteur2 = creer_utilisateur('b@easyway.local')

        incident, doublon = ServiceIncident().signaler(
            auteur2, TypeIncident.EMBOUTEILLAGE, self._point(0.0005, 0.0005)
        )
        self.assertTrue(doublon)
        self.assertEqual(incident.id, existant.id)
        self.assertEqual(Incident.objects.count(), 1)
        self.assertTrue(Vote.objects.filter(incident=existant, votant=auteur2).exists())

    def test_type_different_ne_dedoublonne_pas(self):
        creer_incident(creer_utilisateur('a@easyway.local'), type_incident=TypeIncident.EMBOUTEILLAGE)
        incident, doublon = ServiceIncident().signaler(
            creer_utilisateur('b@easyway.local'), TypeIncident.ACCIDENT, self._point()
        )
        self.assertFalse(doublon)
        self.assertEqual(Incident.objects.count(), 2)

    def test_cap_oppose_ne_dedoublonne_pas(self):
        creer_incident(creer_utilisateur('a@easyway.local'), type_incident=TypeIncident.EMBOUTEILLAGE, cap=0)
        incident, doublon = ServiceIncident().signaler(
            creer_utilisateur('b@easyway.local'), TypeIncident.EMBOUTEILLAGE, self._point(), cap=180
        )
        self.assertFalse(doublon)

    def test_loin_ne_dedoublonne_pas(self):
        creer_incident(creer_utilisateur('a@easyway.local'), type_incident=TypeIncident.EMBOUTEILLAGE)
        incident, doublon = ServiceIncident().signaler(
            creer_utilisateur('b@easyway.local'), TypeIncident.EMBOUTEILLAGE, self._point(0.05, 0.05)
        )
        self.assertFalse(doublon)

    def test_nom_voie_depuis_nominatim(self):
        patcher_nominatim_incident(self, libelle='Avenue Test')
        incident, _ = ServiceIncident().signaler(creer_utilisateur(), TypeIncident.EMBOUTEILLAGE, self._point())
        self.assertEqual(incident.nom_voie, 'Avenue Test')

    def test_nominatim_indisponible_nom_voie_vide_pas_derreur(self):
        incident, _ = ServiceIncident().signaler(creer_utilisateur(), TypeIncident.EMBOUTEILLAGE, self._point())
        self.assertEqual(incident.nom_voie, '')


class IncidentCreationApiTests(TestCase):
    def setUp(self):
        patcher_nominatim_incident(self)
        cache.clear()
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.email)

    def _payload(self, **overrides):
        payload = {'type': TypeIncident.EMBOUTEILLAGE, 'lat': DOUALA_LAT, 'lon': DOUALA_LON}
        payload.update(overrides)
        return payload

    def test_sans_idempotency_key_rejete(self):
        reponse = self.client.post(
            reverse('community:incidents'), self._payload(), content_type='application/json', **self.jetons
        )
        self.assertEqual(reponse.status_code, 400)

    def test_creation_reussie(self):
        reponse = self.client.post(
            reverse('community:incidents'), self._payload(), content_type='application/json',
            HTTP_IDEMPOTENCY_KEY='cle-1', **self.jetons,
        )
        self.assertEqual(reponse.status_code, 201)
        self.assertEqual(Incident.objects.count(), 1)

    def test_rejeu_meme_cle_ne_duplique_pas(self):
        entetes = {'HTTP_IDEMPOTENCY_KEY': 'cle-rejeu', **self.jetons}
        premiere = self.client.post(
            reverse('community:incidents'), self._payload(), content_type='application/json', **entetes
        )
        deuxieme = self.client.post(
            reverse('community:incidents'), self._payload(), content_type='application/json', **entetes
        )
        self.assertEqual(premiere.status_code, 201)
        self.assertEqual(deuxieme.status_code, 200)
        self.assertEqual(premiere.json()['id'], deuxieme.json()['id'])
        self.assertEqual(Incident.objects.count(), 1)


class IncidentsProchesApiTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_sans_cellules_rejete(self):
        reponse = self.client.get(reverse('community:incidents-proches'))
        self.assertEqual(reponse.status_code, 400)

    def test_retourne_les_incidents_de_la_cellule(self):
        incident = creer_incident(creer_utilisateur())
        import h3
        cellule_hex = h3.int_to_str(incident.cellule_h3_res8)

        reponse = self.client.get(reverse('community:incidents-proches'), {'cells': cellule_hex})
        corps = reponse.json()
        self.assertEqual(len(corps), 1)
        self.assertEqual(corps[0]['id'], str(incident.id))

    def test_cellule_vide_retourne_liste_vide(self):
        import h3
        cellule_hex = h3.latlng_to_cell(0.0, 0.0, 8)
        reponse = self.client.get(reverse('community:incidents-proches'), {'cells': cellule_hex})
        self.assertEqual(reponse.json(), [])


class InvalidationCacheProchesTests(TestCase):
    """Le point explicitement demande : le cache doit refleter un vote, pas
    seulement servir un hit correct au premier appel."""

    def setUp(self):
        cache.clear()
        self.auteur = creer_utilisateur('auteur@easyway.local')
        self.incident = creer_incident(self.auteur, type_incident=TypeIncident.EMBOUTEILLAGE)
        self.votant = creer_utilisateur('votant@easyway.local')
        self.jetons_votant = connecter(self.client, self.votant.email)

        import h3
        self.cellule_hex = h3.int_to_str(self.incident.cellule_h3_res8)

    def test_vote_invalide_le_cache_de_la_cellule(self):
        premiere_lecture = self.client.get(
            reverse('community:incidents-proches'), {'cells': self.cellule_hex}
        ).json()
        confirmations_avant = premiere_lecture[0]['confirmations']
        self.assertIsNotNone(cache.get(cle_cache_cellule(self.incident.cellule_h3_res8)))

        self.client.post(
            reverse('community:incident-vote', kwargs={'id': self.incident.id}),
            {'direction': 'confirm'}, content_type='application/json', **self.jetons_votant,
        )

        deuxieme_lecture = self.client.get(
            reverse('community:incidents-proches'), {'cells': self.cellule_hex}
        ).json()
        self.assertEqual(deuxieme_lecture[0]['confirmations'], confirmations_avant + 1)

    def test_retrait_invalide_le_cache_de_la_cellule(self):
        self.client.get(reverse('community:incidents-proches'), {'cells': self.cellule_hex})
        jetons_auteur = connecter(self.client, self.auteur.email)

        self.client.delete(reverse('community:incident-detail', kwargs={'id': self.incident.id}), **jetons_auteur)

        deuxieme_lecture = self.client.get(
            reverse('community:incidents-proches'), {'cells': self.cellule_hex}
        ).json()
        self.assertEqual(deuxieme_lecture, [])


class IncidentDetailApiTests(TestCase):
    def setUp(self):
        self.auteur = creer_utilisateur('auteur@easyway.local')
        self.incident = creer_incident(self.auteur, confirmations=3, infirmations=1)

    def test_detail_inclut_impact_estime(self):
        reponse = self.client.get(reverse('community:incident-detail', kwargs={'id': self.incident.id}))
        self.assertEqual(reponse.json()['estimated_impact'], 2)

    def test_retrait_par_lauteur(self):
        jetons = connecter(self.client, self.auteur.email)
        reponse = self.client.delete(
            reverse('community:incident-detail', kwargs={'id': self.incident.id}), **jetons
        )
        self.assertEqual(reponse.status_code, 204)
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.statut, StatutIncident.RETIRE)

    def test_retrait_refuse_pour_non_auteur(self):
        autre = creer_utilisateur('autre@easyway.local')
        jetons = connecter(self.client, autre.email)
        reponse = self.client.delete(
            reverse('community:incident-detail', kwargs={'id': self.incident.id}), **jetons
        )
        self.assertEqual(reponse.status_code, 404)


class VoteApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.auteur = creer_utilisateur('auteur@easyway.local')
        self.votant = creer_utilisateur('votant@easyway.local')
        self.jetons_votant = connecter(self.client, self.votant.email)

    def _voter(self, incident, sens, jetons=None):
        return self.client.post(
            reverse('community:incident-vote', kwargs={'id': incident.id}),
            {'direction': sens}, content_type='application/json', **(jetons or self.jetons_votant),
        )

    def test_confirmer_incremente_et_prolonge(self):
        incident = creer_incident(self.auteur, expire_le=timezone.now() + timezone.timedelta(minutes=10))
        expire_avant = incident.expire_le
        reponse = self._voter(incident, 'confirm')
        self.assertEqual(reponse.status_code, 200)
        incident.refresh_from_db()
        self.assertEqual(incident.confirmations, 1)
        self.assertGreater(incident.expire_le, expire_avant)

    def test_infirmer_incremente_et_reduit(self):
        incident = creer_incident(self.auteur)
        expire_avant = incident.expire_le
        self._voter(incident, 'dispute')
        incident.refresh_from_db()
        self.assertEqual(incident.infirmations, 1)
        self.assertLess(incident.expire_le, expire_avant)

    def test_vote_sur_son_propre_signalement_refuse(self):
        incident = creer_incident(self.auteur)
        jetons_auteur = connecter(self.client, self.auteur.email)
        reponse = self._voter(incident, 'confirm', jetons=jetons_auteur)
        self.assertEqual(reponse.status_code, 400)

    def test_double_vote_refuse(self):
        incident = creer_incident(self.auteur)
        self._voter(incident, 'confirm')
        reponse = self._voter(incident, 'confirm')
        self.assertEqual(reponse.status_code, 400)

    def test_deux_confirmations_promeuvent_en_attente_vers_actif(self):
        incident = creer_incident(self.auteur, statut=StatutIncident.EN_ATTENTE)
        self._voter(incident, 'confirm')
        incident.refresh_from_db()
        self.assertEqual(incident.statut, StatutIncident.EN_ATTENTE)

        autre_votant = creer_utilisateur('votant2@easyway.local')
        self._voter(incident, 'confirm', jetons=connecter(self.client, autre_votant.email))
        incident.refresh_from_db()
        self.assertEqual(incident.statut, StatutIncident.ACTIF)

    def test_score_confiance_pondere_par_reputation(self):
        votant_fort = creer_utilisateur('fort@easyway.local', score_reputation=200)
        incident = creer_incident(self.auteur)
        self._voter(incident, 'confirm', jetons=connecter(self.client, votant_fort.email))
        incident.refresh_from_db()
        self.assertEqual(incident.score_confiance, votant_fort.poids_de_vote())


class MesSignalementsApiTests(TestCase):
    def test_liste_uniquement_les_siens(self):
        moi = creer_utilisateur('moi@easyway.local')
        creer_incident(moi)
        creer_incident(creer_utilisateur('autre@easyway.local'))

        jetons = connecter(self.client, moi.email)
        reponse = self.client.get(reverse('community:mes-signalements'), **jetons)
        self.assertEqual(len(reponse.json()), 1)


class ExpirationTaskTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_incidents_expires_changent_de_statut(self):
        incident = creer_incident(
            creer_utilisateur(), expire_le=timezone.now() - timezone.timedelta(minutes=1)
        )
        expirer_incidents()
        incident.refresh_from_db()
        self.assertEqual(incident.statut, StatutIncident.EXPIRE)

    def test_expiration_invalide_le_cache_de_la_cellule(self):
        incident = creer_incident(
            creer_utilisateur(), expire_le=timezone.now() + timezone.timedelta(seconds=1)
        )
        import h3
        cellule_hex = h3.int_to_str(incident.cellule_h3_res8)
        self.client.get(reverse('community:incidents-proches'), {'cells': cellule_hex})
        self.assertIsNotNone(cache.get(cle_cache_cellule(incident.cellule_h3_res8)))

        incident.expire_le = timezone.now() - timezone.timedelta(seconds=1)
        incident.save(update_fields=['expire_le'])
        expirer_incidents()

        self.assertIsNone(cache.get(cle_cache_cellule(incident.cellule_h3_res8)))
        deuxieme_lecture = self.client.get(
            reverse('community:incidents-proches'), {'cells': cellule_hex}
        ).json()
        self.assertEqual(deuxieme_lecture, [])

    def test_incidents_non_expires_ne_changent_pas(self):
        incident = creer_incident(creer_utilisateur(), expire_le=timezone.now() + timezone.timedelta(hours=1))
        expirer_incidents()
        incident.refresh_from_db()
        self.assertEqual(incident.statut, StatutIncident.ACTIF)
