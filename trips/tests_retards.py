"""Itineraires voiture/moto alourdis par les signalements (spec
ITINERAIRES_VOITURE_MOTO, etape 1)."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.tests import connecter, creer_utilisateur
from community.models import Incident, SousTypeIncident as S, StatutIncident, TypeIncident as T

from .models import PrevisionItineraire, Trajet
from .polyline import encoder_polyline6
from .services import retards_signalements

# Deux itineraires de ~3,3 km vers l'est : le direct (latitude 4.05) et un
# detour par le nord. Le direct est le plus rapide a vide.
DIRECT = [(9.70, 4.05), (9.715, 4.05), (9.73, 4.05)]
NORD = [(9.70, 4.05), (9.705, 4.06), (9.725, 4.06), (9.73, 4.05)]


def trip_valhalla(points, secondes):
    return {
        'legs': [{'shape': encoder_polyline6(points), 'maneuvers': [
            {'type': 1, 'instruction': 'Partez', 'length': 3.3, 'time': secondes, 'street_names': []},
        ]}],
        'summary': {'length': 3.3, 'time': secondes},
    }


class RetardsSignalementsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()
        self.jetons = connecter(self.client, self.utilisateur.telephone)
        self.auteur = creer_utilisateur(email='auteur@test.cm')
        patcheur_trafic = patch(
            'trips.services.service_itineraire.service_trafic.evaluer_route',
            return_value={'niveau_trafic': 'NORMAL', 'duree_avec_trafic': None},
        )
        patcheur_trafic.start()
        self.addCleanup(patcheur_trafic.stop)

    def signaler(self, type_, sous_type, lon=9.715, lat=4.05, cap=90, **extra):
        valeurs = dict(
            auteur=self.auteur, type=type_, sous_type=sous_type, position=Point(lon, lat, srid=4326), cap=cap,
            statut=StatutIncident.ACTIF, score_confiance=3, expire_le=timezone.now() + timedelta(hours=2),
        )
        valeurs.update(extra)
        return Incident.objects.create(**valeurs)

    def calculer(self, **corps):
        with patch('trips.services.service_itineraire.ClientValhalla') as Client:
            client = Client.return_value
            client.calculer_itineraires.return_value = [trip_valhalla(DIRECT, 400), trip_valhalla(NORD, 520)]
            reponse = self.client.post(
                reverse('trips:calculer-itineraire'),
                {'origin_lat': 4.05, 'origin_lon': 9.70, 'destination_lat': 4.05, 'destination_lon': 9.73, **corps},
                content_type='application/json', **self.jetons,
            )
            if client.calculer_itineraires.call_args:  # absent si le resultat Valhalla vient du cache
                self.options = client.calculer_itineraires.call_args.args[2]
        self.assertEqual(reponse.status_code, 200, reponse.content)
        return reponse.json()

    def test_sans_signalement_rien_ne_change(self):
        routes = self.calculer()
        self.assertEqual([r['duration'] for r in routes], [400, 520])
        self.assertEqual(routes[0]['delay_from_reports'], 0)
        self.assertEqual(routes[0]['vehicle_type'], 'VOITURE')

    def test_accident_sur_le_direct_fait_passer_le_detour_devant(self):
        self.signaler(T.ACCIDENT, S.CARAMBOLAGE)  # 5 km/h sur 500 m
        routes = self.calculer()
        self.assertEqual(routes[0]['duration'], 520)  # le detour est recommande
        self.assertTrue(routes[0]['is_recommended'])
        direct = routes[1]
        self.assertGreater(direct['delay_from_reports'], 250)  # 500 m a 5 km/h au lieu de ~30 km/h : ~5 min
        self.assertEqual(direct['duration_with_traffic'], 400 + direct['delay_from_reports'])
        self.assertEqual(direct['reports_applied'][0]['subtype'], 'CARAMBOLAGE')

    def test_autre_sens_de_circulation_sans_effet(self):
        self.signaler(T.ACCIDENT, S.CARAMBOLAGE, cap=270)
        self.assertEqual(self.calculer()[0]['delay_from_reports'], 0)

    def test_police_et_radar_sans_effet(self):
        self.signaler(T.POLICE, S.POLICE)
        self.signaler(T.RADAR, '')
        self.assertEqual(self.calculer()[0]['delay_from_reports'], 0)

    def test_embouteillage_actif_deja_dans_le_trafic_en_direct_pour_la_voiture(self):
        self.signaler(T.EMBOUTEILLAGE, S.A_L_ARRET)
        self.assertEqual(self.calculer()[0]['delay_from_reports'], 0)

    def test_moto(self):
        self.signaler(T.EMBOUTEILLAGE, S.A_L_ARRET)
        routes = self.calculer(vehicle_type='MOTO')
        self.assertEqual(self.options['costing'], 'motorcycle')
        reglages = self.options['costing_options']['motorcycle']
        self.assertNotIn('current', reglages['speed_types'])  # la moto ignore le trafic en direct
        self.assertEqual(reglages['use_tracks'], 0.5)
        direct = next(r for r in routes if r['duration'] == 400)
        # Moto : embouteillage compte, avec 40 % du retard voiture.
        voiture = retards_signalements._retard_s(retards_signalements.EFFETS[(T.EMBOUTEILLAGE, S.A_L_ARRET)], 3300 / 400)
        self.assertAlmostEqual(direct['delay_from_reports'], round(voiture * 0.4), delta=1)
        self.assertEqual(direct['vehicle_type'], 'MOTO')

    def test_route_barree_confirmee_exclue_sinon_ralentissement(self):
        incident = self.signaler(T.VOIE_BLOQUEE, S.ROUTE_BARREE, confirmations=0)
        routes = self.calculer()
        self.assertNotIn('exclude_locations', self.options)
        self.assertGreater(next(r for r in routes if r['duration'] == 400)['delay_from_reports'], 0)

        incident.confirmations = 1
        incident.save(update_fields=['confirmations'])
        cache.clear()
        self.calculer()
        self.assertEqual(self.options['exclude_locations'], [{'lat': 4.05, 'lon': 9.715}])

    def test_signalement_tout_neuf_compte_moins(self):
        self.signaler(T.ACCIDENT, S.CARAMBOLAGE, score_confiance=0)
        neuf = next(r for r in self.calculer() if r['duration'] == 400)['delay_from_reports']
        Incident.objects.update(score_confiance=3)
        valide = next(r for r in self.calculer() if r['duration'] == 400)['delay_from_reports']
        self.assertAlmostEqual(neuf * 4, valide, delta=4)

    def test_prevision_enregistree_et_reliee_au_trajet(self):
        routes = self.calculer()
        prevision = PrevisionItineraire.objects.get()
        self.assertEqual(prevision.itineraire_recommande, routes[0]['route_id'])
        self.assertEqual(len(prevision.candidats), 2)

        reponse = self.client.post(
            reverse('trips:trajets'),
            {'origin_label': 'A', 'origin_lat': 4.05, 'origin_lon': 9.70, 'destination_label': 'B',
             'destination_lat': 4.05, 'destination_lon': 9.73, 'route': routes[1]},
            content_type='application/json', **self.jetons,
        )
        self.assertEqual(reponse.status_code, 201, reponse.content)
        prevision.refresh_from_db()
        trajet = Trajet.objects.get()
        self.assertEqual((prevision.trajet, prevision.itineraire_choisi), (trajet, routes[1]['route_id']))
        self.assertEqual(trajet.type_vehicule, 'VOITURE')


class ValidationParLaModerationTests(RetardsSignalementsTests):
    """Le back-office valide une route barree : evitee sans attendre les votes."""

    def test_route_barree_validee_par_le_staff_exclue(self):
        incident = self.signaler(T.VOIE_BLOQUEE, S.ROUTE_BARREE, confirmations=0, statut=StatutIncident.EN_ATTENTE)
        self.calculer()
        self.assertNotIn('exclude_locations', self.options)  # non confirmee : simple ralentissement

        staff = creer_utilisateur(email='staff@test.cm', is_staff=True)
        reponse = self.client.post(
            reverse('community:staff-incident-valider', kwargs={'id': incident.id}),
            **connecter(self.client, staff.telephone),
        )
        self.assertEqual(reponse.status_code, 200, reponse.content)
        self.assertTrue(reponse.json()['staff_validated'])
        incident.refresh_from_db()
        self.assertEqual(incident.statut, StatutIncident.ACTIF)

        cache.clear()
        self.calculer()
        self.assertEqual(self.options['exclude_locations'], [{'lat': 4.05, 'lon': 9.715}])

    def test_voie_bloquee_sans_sous_type_validee_exclue(self):
        # Cas du 28/10 (Logbessou) : signalee depuis la carte de test, sans
        # sous-type ni cap -- ignoree jusqu'ici, meme validee.
        incident = self.signaler(T.VOIE_BLOQUEE, '', cap=None, confirmations=0)
        routes = self.calculer()
        self.assertNotIn('exclude_locations', self.options)
        self.assertGreater(next(r for r in routes if r['duration'] == 400)['delay_from_reports'], 0)  # au moins ralentie

        incident.valider_par_moderation()
        cache.clear()
        self.calculer()
        self.assertEqual(self.options['exclude_locations'], [{'lat': 4.05, 'lon': 9.715}])

    def test_validation_reservee_au_staff(self):
        incident = self.signaler(T.VOIE_BLOQUEE, S.ROUTE_BARREE)
        url = reverse('community:staff-incident-valider', kwargs={'id': incident.id})
        self.assertEqual(self.client.post(url, **self.jetons).status_code, 403)

    # Les tests herites de RetardsSignalementsTests ne sont pas rejoues ici.
    test_sans_signalement_rien_ne_change = None
    test_accident_sur_le_direct_fait_passer_le_detour_devant = None
    test_autre_sens_de_circulation_sans_effet = None
    test_police_et_radar_sans_effet = None
    test_embouteillage_actif_deja_dans_le_trafic_en_direct_pour_la_voiture = None
    test_moto = None
    test_route_barree_confirmee_exclue_sinon_ralentissement = None
    test_signalement_tout_neuf_compte_moins = None
    test_prevision_enregistree_et_reliee_au_trajet = None
