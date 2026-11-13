"""Trafic dans Valhalla (spec TRAFIC_VALHALLA) : embouteillage signale etendu
en amont, filtres des vitesses avant agregation."""

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from accounts.tests import creer_utilisateur
from community.models import Incident, StatutIncident

from .models import FUSEAU_TRAFIC, StatutTrajet, Trajet
from .services import trafic_temps_reel
from .services.consommateur_positions import ConsommateurPositions
from .services.producteur_evenements import FLUX_POSITIONS


class EmbouteillageAmontTests(TestCase):
    def setUp(self):
        cache.clear()
        self.auteur = creer_utilisateur()

    def signaler(self, cap, **extra):
        valeurs = dict(
            auteur=self.auteur, type='EMBOUTEILLAGE', sous_type='A_L_ARRET', position=Point(9.70, 4.05, srid=4326),
            cap=cap, statut=StatutIncident.ACTIF, expire_le=timezone.now() + timedelta(minutes=30),
            score_confiance=3,  # valide par la communaute : confiance pleine, vitesse imposee telle quelle
        )
        valeurs.update(extra)
        return Incident.objects.create(**valeurs)

    def test_aretes_des_200_m_en_amont_ralenties(self):
        self.signaler(cap=90)  # vers l'est : la file est a l'ouest
        with patch('trips.services.client_meili.tracer', return_value=([{'id': 1}, {'id': 2}, {'id': 3}], [])) as tracer:
            vitesses = trafic_temps_reel.vitesses_signalements()
        self.assertEqual(vitesses, {1: 5, 2: 5, 3: 5})
        points = tracer.call_args.args[0]
        self.assertEqual(len(points), 6)  # 200, 160, ..., 0 m
        self.assertLess(points[0]['lon'], points[-1]['lon'])  # on part de l'ouest (amont)
        self.assertAlmostEqual(points[-1]['lon'], 9.70, places=6)

    def test_sans_cap_aucune_arete(self):
        # Sans cap, le sens de circulation serait devine (arete du sens oppose
        # possible) : rien dans traffic.tar, l'embouteillage compte en retard.
        self.signaler(cap=None)
        with patch('trips.services.client_meili.tracer') as tracer, \
                patch('trips.services.client_locate.localiser', return_value={'graph_id': 7}) as localiser:
            vitesses = trafic_temps_reel.vitesses_signalements()
        tracer.assert_not_called()
        localiser.assert_not_called()
        self.assertEqual(vitesses, {})

    def test_vitesse_ponderee_par_la_confiance(self):
        # Tout neuf (score 0) : confiance 1/4, la vitesse imposee (5 km/h) est
        # ramenee vers la vitesse normale de reference (40 km/h).
        self.signaler(cap=90, score_confiance=0)
        with patch('trips.services.client_meili.tracer', return_value=([{'id': 1}], [])):
            vitesses = trafic_temps_reel.vitesses_signalements()
        self.assertAlmostEqual(vitesses[1], 40 - 35 * 0.25, places=1)

    def test_signalement_conteste_non_publie(self):
        self.signaler(cap=90, score_confiance=-1, infirmations=2)
        with patch('trips.services.client_meili.tracer', return_value=([{'id': 1}], [])):
            self.assertEqual(trafic_temps_reel.vitesses_signalements(), {})

    def test_erreur_sur_un_incident_n_arrete_pas_les_suivants(self):
        from trips.services.client_locate import ErreurLocate

        self.signaler(cap=90, position=Point(9.70, 4.05, srid=4326))
        self.signaler(cap=90, position=Point(9.80, 4.10, srid=4326))
        appels = iter([ErreurLocate('une erreur'), {'graph_id': 9}])

        def localiser(*_args):
            reponse = next(appels)
            if isinstance(reponse, Exception):
                raise reponse
            return reponse

        with patch('trips.services.client_meili.tracer', return_value=([], [])), \
                patch('trips.services.client_locate.localiser', side_effect=localiser):
            self.assertEqual(trafic_temps_reel.vitesses_signalements(), {9: 5})

    def test_recalage_impossible_repli_sur_l_arete(self):
        from trips.services.client_meili import ErreurMeili

        self.signaler(cap=90)
        with patch('trips.services.client_meili.tracer', side_effect=ErreurMeili('panne')), \
                patch('trips.services.client_locate.localiser', return_value={'graph_id': 7}):
            self.assertEqual(trafic_temps_reel.vitesses_signalements(), {7: 5})


def _message(trajet_id, lat, lon, horodatage):
    return {'donnees': json.dumps({
        'trajet_id': trajet_id, 'lat': lat, 'lon': lon, 'vitesse_kmh': None, 'cap': None, 'horodatage': horodatage,
    })}


class FiltresVitessesTests(TestCase):
    def consommer(self, aretes, trajet_id='t1', positions=((4.05, 9.70), (4.06, 9.71))):
        connexion = MagicMock()
        connexion.xgroup_create.return_value = True
        connexion.xautoclaim.return_value = ('0-0', [], [])
        connexion.xreadgroup.return_value = [(FLUX_POSITIONS, [
            ('1-1', _message(trajet_id, *positions[0], '2026-01-01T10:00:00+00:00')),
            ('1-2', _message(trajet_id, *positions[1], '2026-01-01T10:01:00+00:00')),
        ])]
        consommateur = ConsommateurPositions(connexion=connexion, nom_consommateur='test')
        points = [{'edge_index': 0, 'distance_along_edge': 0.0}, {'edge_index': 0, 'distance_along_edge': 1.0}]
        with patch('trips.services.consommateur_positions.tracer', return_value=(aretes, points)), \
                patch('trips.services.consommateur_positions.enregistrer_passages'):
            consommateur.consommer()
        pipe = connexion.pipeline.return_value.__enter__.return_value
        return pipe.hincrbyfloat.call_count > 0

    def test_vitesse_aberrante_selon_la_classe_de_route(self):
        # 1,6 km en 60 s = 96 km/h : aberrant en ville (> 90), plausible sur une nationale (< 135).
        self.assertFalse(self.consommer([{'id': 1, 'length': 1.6, 'road_class': 'residential'}]))
        self.assertTrue(self.consommer([{'id': 1, 'length': 1.6, 'road_class': 'trunk'}]))
        self.assertTrue(self.consommer([{'id': 1, 'length': 1.6}]))  # classe inconnue : plafond 180

    def test_depart_et_arrivee_ignores(self):
        trajet = Trajet.objects.create(
            utilisateur=creer_utilisateur(), position_origine='POINT(9.70 4.05)', libelle_origine='A',
            position_destination='POINT(9.90 4.20)', libelle_destination='B', itineraire_choisi='x',
            distance_prevue=1000, duree_prevue=100, statut=StatutTrajet.ACTIF,
        )
        arete = [{'id': 1, 'length': 0.5, 'road_class': 'primary'}]
        self.assertFalse(self.consommer(arete, str(trajet.id)))  # premiere position au point de depart
        self.assertTrue(self.consommer(arete, str(trajet.id), positions=((4.10, 9.80), (4.11, 9.81))))

    def test_motos_exclues_des_vitesses(self):
        trajet = Trajet.objects.create(
            utilisateur=creer_utilisateur(), position_origine='POINT(9.70 4.05)', libelle_origine='A',
            position_destination='POINT(9.90 4.20)', libelle_destination='B', itineraire_choisi='x',
            distance_prevue=1000, duree_prevue=100, statut=StatutTrajet.ACTIF, type_vehicule='MOTO',
        )
        arete = [{'id': 1, 'length': 0.5, 'road_class': 'primary'}]
        self.assertFalse(self.consommer(arete, str(trajet.id), positions=((4.10, 9.80), (4.11, 9.81))))

    def test_seules_voitures_et_taxis_mesurent_le_trafic(self):
        arete = [{'id': 1, 'length': 0.5, 'road_class': 'primary'}]
        for type_vehicule, mesure in (('BUS', False), ('CAMION', False), ('UTILITAIRE', False),
                                      ('TAXI', True), ('VOITURE', True)):
            trajet = Trajet.objects.create(
                utilisateur=creer_utilisateur(f'{type_vehicule.lower()}@test.cm'), position_origine='POINT(9.70 4.05)',
                libelle_origine='A', position_destination='POINT(9.90 4.20)', libelle_destination='B',
                itineraire_choisi='x', distance_prevue=1000, duree_prevue=100, statut=StatutTrajet.ACTIF,
                type_vehicule=type_vehicule,
            )
            self.assertEqual(
                self.consommer(arete, str(trajet.id), positions=((4.10, 9.80), (4.11, 9.81))), mesure, type_vehicule,
            )


class SurveillanceTraficTests(TestCase):
    def setUp(self):
        cache.clear()

    def _surveiller(self):
        from .tasks import surveiller_trafic_temps_reel

        connexion = MagicMock()
        connexion.xlen.return_value = 12
        connexion.xpending.return_value = {'pending': 0, 'min': None}
        with patch('trips.tasks.connexion_redis_telemetrie', return_value=connexion), \
                self.settings(TRAFIC_TAR_PATH='/tmp/traffic.tar'):
            return surveiller_trafic_temps_reel()

    def test_sans_publication_recente_le_staff_est_prevenu(self):
        from ads_admin.models import NotificationStaff, TypeNotification

        etat = self._surveiller()
        self.assertIsNone(etat['last_publication_at'])
        self.assertEqual(NotificationStaff.objects.get().type, TypeNotification.TRAFIC)

    def test_publication_recente_aucune_alerte(self):
        from ads_admin.models import NotificationStaff

        cache.set(trafic_temps_reel.CLE_DERNIERE_PUBLICATION,
                  {'le': int(timezone.now().timestamp()) - 60, 'ecrites': 3, 'effacees': 0})
        etat = self._surveiller()
        self.assertEqual(etat['last_publication_written'], 3)
        self.assertFalse(NotificationStaff.objects.exists())


class AretesStablesTests(TestCase):
    """L'historique de vitesses survit a une reconstruction des tuiles : il est
    rattache a la voie OSM + sens, puis reporte sur les GraphId actuels."""

    def setUp(self):
        cache.clear()

    def echantillons(self, n, **champs):
        from .models import EchantillonVitesse

        maintenant = timezone.now()
        local = maintenant.astimezone(FUSEAU_TRAFIC)
        for i in range(n):
            EchantillonVitesse.objects.create(
                debut_intervalle=maintenant - timedelta(weeks=1 + i % 6, minutes=5 * i),
                jour_semaine=local.weekday(), heure_jour=local.hour, nombre_echantillons=1, **champs,
            )

    def test_historique_par_voie_reporte_sur_le_nouveau_graph_id(self):
        from .models import CorrespondanceArete
        from .services.service_trafic import NB_MIN_ECHANTILLONS_TYPIQUE, vitesses_typiques_creneau

        # Mesure sous l'ancien GraphId 111, sur la voie OSM 42 dans le sens avant.
        self.echantillons(NB_MIN_ECHANTILLONS_TYPIQUE, identifiant_arete=111, way_id=42, sens_avant=True,
                          vitesse_moyenne=18)
        # Apres reconstruction, la meme voie s'appelle 222 (sens avant) et 223 (sens arriere).
        CorrespondanceArete.objects.create(graph_id=222, way_id=42, sens_avant=True, vu_le=timezone.now())
        CorrespondanceArete.objects.create(graph_id=223, way_id=42, sens_avant=False, vu_le=timezone.now())
        vitesses = vitesses_typiques_creneau()
        self.assertEqual(vitesses.get(222), 18.0)
        self.assertNotIn(223, vitesses)  # autre sens : pas de mesure

    def test_historique_sans_voie_limite_a_la_generation_en_cours(self):
        from .services.service_trafic import (
            CLE_DEBUT_GENERATION_TUILES, NB_MIN_ECHANTILLONS_TYPIQUE, vitesses_typiques_creneau,
        )

        self.echantillons(NB_MIN_ECHANTILLONS_TYPIQUE, identifiant_arete=111, vitesse_moyenne=18)
        self.assertEqual(vitesses_typiques_creneau().get(111), 18.0)
        # Tuiles reconstruites depuis : l'ancien GraphId 111 ne veut plus rien dire.
        cache.clear()
        cache.set(CLE_DEBUT_GENERATION_TUILES, timezone.now(), timeout=None)
        self.assertNotIn(111, vitesses_typiques_creneau())

    def test_reconstruction_detectee_oublie_les_correspondances(self):
        from .models import CorrespondanceArete

        CorrespondanceArete.objects.create(graph_id=222, way_id=42, sens_avant=True, vu_le=timezone.now())
        connexion = MagicMock()
        with patch('trips.services.tuiles_trafic.generation', return_value='1:100'):
            trafic_temps_reel._suivre_generation('/tmp/traffic.tar', connexion)
        self.assertTrue(CorrespondanceArete.objects.exists())  # premiere fois : rien a oublier
        with patch('trips.services.tuiles_trafic.generation', return_value='1:200'):
            trafic_temps_reel._suivre_generation('/tmp/traffic.tar', connexion)
        self.assertFalse(CorrespondanceArete.objects.exists())
        connexion.delete.assert_called_once_with(trafic_temps_reel.CLE_ARETES_PUBLIEES)

    def test_consommateur_apprend_la_voie_de_chaque_arete(self):
        from .models import CorrespondanceArete
        from .services.consommateur_positions import apprendre_correspondances, cle_stable

        aretes = [{'id': 5, 'way_id': 42, 'forward': False}, {'id': 6, 'way_id': 43}]  # 6 : sens inconnu
        apprendre_correspondances(aretes)
        self.assertEqual(
            list(CorrespondanceArete.objects.values_list('graph_id', 'way_id', 'sens_avant')), [(5, 42, False)],
        )
        self.assertIsNone(cle_stable(aretes[1]))


class AlerteConducteursTests(TestCase):
    """route.recheck pousse par WebSocket aux conducteurs dont la route
    restante passe sur un signalement (cf. services/alertes_itineraire.py)."""

    def setUp(self):
        cache.clear()
        from django.contrib.gis.geos import LineString

        self.conducteur = creer_utilisateur('conducteur@test.cm')
        self.trajet = Trajet.objects.create(
            utilisateur=self.conducteur, position_origine='POINT(9.70 4.05)', libelle_origine='A',
            position_destination='POINT(9.73 4.05)', libelle_destination='B', itineraire_choisi='x',
            distance_prevue=3300, duree_prevue=400, statut=StatutTrajet.ACTIF,
            geometrie=LineString([(9.70, 4.05), (9.715, 4.05), (9.73, 4.05)], srid=4326),
        )

    def signaler(self, type_, sous_type, **extra):
        valeurs = dict(
            auteur=creer_utilisateur(f'auteur{Incident.objects.count()}@test.cm'), type=type_, sous_type=sous_type,
            position=Point(9.72, 4.05, srid=4326), cap=90, statut=StatutIncident.ACTIF, score_confiance=3,
            expire_le=timezone.now() + timedelta(hours=1),
        )
        valeurs.update(extra)
        return Incident.objects.create(**valeurs)

    def alerter(self, incident, position=None):
        from .services import derniere_position
        from .services.alertes_itineraire import alerter_conducteurs

        if position:
            derniere_position.memoriser(self.conducteur.id, *position)
        couche = MagicMock()
        with patch('trips.services.alertes_itineraire.get_channel_layer', return_value=couche), \
                patch('trips.services.alertes_itineraire.async_to_sync', side_effect=lambda f: f):
            nb = alerter_conducteurs(incident.id)
        return nb, couche.group_send.call_args_list

    def test_route_barree_devant_previent_le_conducteur(self):
        incident = self.signaler('VOIE_BLOQUEE', 'ROUTE_BARREE')
        nb, envois = self.alerter(incident, position=(4.05, 9.705))
        self.assertEqual(nb, 1)
        groupe, message = envois[0].args
        self.assertEqual(groupe, f'utilisateur.{self.conducteur.id}')
        self.assertEqual(message['payload']['type'], 'route.recheck')
        self.assertEqual(message['payload']['cause'], 'ROAD_CLOSED')
        self.assertEqual(message['payload']['trip_id'], str(self.trajet.id))

    def test_incident_deja_depasse_ou_sans_effet_rien(self):
        nb, _ = self.alerter(self.signaler('EMBOUTEILLAGE', 'A_L_ARRET'), position=(4.05, 9.725))  # deja passe
        self.assertEqual(nb, 0)
        nb, _ = self.alerter(self.signaler('POLICE', 'POLICE'), position=(4.05, 9.705))  # aucun effet
        self.assertEqual(nb, 0)

    def test_une_seule_alerte_par_incident_en_rafale(self):
        incident = self.signaler('EMBOUTEILLAGE', 'A_L_ARRET')
        self.assertEqual(self.alerter(incident, position=(4.05, 9.705))[0], 1)
        cache.delete(f'alerte:conducteur:{self.conducteur.id}')
        self.assertEqual(self.alerter(incident, position=(4.05, 9.705))[0], 0)  # vote suivant : deja traite


class FermeturesTraficTests(TestCase):
    def setUp(self):
        cache.clear()
        self.auteur = creer_utilisateur()

    def signaler(self, **extra):
        valeurs = dict(
            auteur=self.auteur, type='VOIE_BLOQUEE', sous_type='ROUTE_BARREE', position=Point(9.70, 4.05, srid=4326),
            cap=90, statut=StatutIncident.ACTIF, score_confiance=3, expire_le=timezone.now() + timedelta(hours=2),
        )
        valeurs.update(extra)
        return Incident.objects.create(**valeurs)

    def test_fermeture_confirmee_sur_son_arete_et_son_sens(self):
        incident = self.signaler()
        with patch('trips.services.client_locate.localiser', return_value={'graph_id': 7}) as localiser:
            self.assertEqual(trafic_temps_reel._fermetures_signalees(), ({7}, {str(incident.id)}))
        self.assertEqual(localiser.call_args.args[2], 90)  # heading transmis : le bon sens

    def test_non_confirmee_ou_sans_cap_non_publiee(self):
        self.signaler(score_confiance=1)  # pas encore validee : simple ralentissement
        self.signaler(cap=None)           # sens inconnu : reste exclue par point
        with patch('trips.services.client_locate.localiser', return_value={'graph_id': 7}):
            self.assertEqual(trafic_temps_reel._fermetures_signalees(), (set(), set()))


class ClientMatriceTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_durees_dans_l_ordre_des_cibles_et_inaccessible_a_null(self):
        from .services.client_matrice import durees_depuis

        reponse = MagicMock(status_code=200)
        reponse.raise_for_status = lambda: None
        reponse.json.return_value = {'sources_to_targets': [[
            {'from_index': 0, 'to_index': 1, 'time': 120.4, 'distance': 0.3},
            {'from_index': 0, 'to_index': 0, 'time': None, 'distance': None},
        ]]}
        with patch('trips.services.client_matrice.requests.post', return_value=reponse) as post:
            self.assertEqual(durees_depuis((4.05, 9.70), [(4.06, 9.71), (4.05, 9.71)]), [(None, None), (120, 300)])
        self.assertEqual(post.call_args.args[0].rsplit('/', 1)[1], 'sources_to_targets')

    def test_panne_renvoie_none(self):
        import requests

        from .services.client_matrice import durees_depuis

        with patch('trips.services.client_matrice.requests.post', side_effect=requests.exceptions.ConnectionError()):
            self.assertIsNone(durees_depuis((4.05, 9.70), [(4.06, 9.71)]))
