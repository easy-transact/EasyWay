"""Pilotage du back-office (vues_pilotage) et enregistrement des propositions better-route."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.gis.geos import LineString, Point
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.tests import connecter, creer_utilisateur
from community.models import Incident, SousTypeIncident as S, StatutIncident, TypeIncident as T
from etat_routes.models import PassageTroncon, Troncon

from .models import PropositionItineraire, StatutTrajet, Trajet
from .tests import _trajet_actif_pour
from .tests_meilleur_itineraire import ROUTE_SUIVIE, VARIANTE, candidat


class PilotageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = creer_utilisateur(email='staff@easyway.local', is_staff=True)
        self.jetons = connecter(self.client, self.staff.telephone)
        self.conducteur = creer_utilisateur(email='conducteur@test.cm')

    def lire(self, nom, **params):
        reponse = self.client.get(reverse(f'trips:{nom}'), params, **self.jetons)
        self.assertEqual(reponse.status_code, 200, reponse.content)
        return reponse.json()

    def test_reserve_au_staff(self):
        jetons = connecter(self.client, self.conducteur.telephone)
        self.assertEqual(self.client.get(reverse('trips:pilotage-eta'), **jetons).status_code, 403)

    def test_fiabilite_des_temps(self):
        fin = timezone.now()
        for prevue, reelle in ((600, 660), (600, 900), (600, 60)):  # +10 %, +50 %, interrompu
            _trajet_actif_pour(self.conducteur, statut=StatutTrajet.TERMINE, duree_prevue=prevue,
                               duree_reelle=reelle, termine_le=fin)
        donnees = self.lire('pilotage-eta')
        self.assertEqual(donnees['ignored_interrupted'], 1)
        self.assertEqual(donnees['summary']['count'], 2)
        self.assertEqual(donnees['summary']['median_error_pct'], 30.0)
        self.assertEqual(donnees['summary']['within_20_pct'], 50.0)
        self.assertEqual(donnees['by_zone'][0]['key'], 'Douala')
        self.assertEqual(donnees['worst'][0]['error_pct'], 50.0)

    def test_file_de_moderation_par_urgence(self):
        expire = timezone.now() + timedelta(hours=2)
        base = dict(auteur=self.conducteur, statut=StatutIncident.ACTIF, expire_le=expire, score_confiance=0)
        sur_la_route = Incident.objects.create(type=T.ACCIDENT, sous_type=S.ACCIDENT,
                                               position=Point(9.715, 4.05, srid=4326), **base)
        Incident.objects.create(type=T.ACCIDENT, sous_type=S.ACCIDENT, position=Point(9.9, 4.2, srid=4326), **base)
        Incident.objects.create(type=T.POLICE, sous_type=S.POLICE, position=Point(9.715, 4.05, srid=4326), **base)
        Incident.objects.create(type=T.ACCIDENT, sous_type=S.ACCIDENT, position=Point(9.715, 4.05, srid=4326),
                                valide_par_staff=True, **base)
        _trajet_actif_pour(self.conducteur, geometrie=LineString(ROUTE_SUIVIE, srid=4326))
        lignes = self.lire('pilotage-moderation')['results']
        self.assertEqual(len(lignes), 3)  # le valide n'y est plus
        self.assertEqual((lignes[0]['id'], lignes[0]['active_trips_nearby']), (str(sur_la_route.id), 1))
        self.assertEqual(lignes[-1]['type'], T.POLICE)

    def test_propositions_enregistrees_et_acceptees(self):
        trajet = _trajet_actif_pour(self.conducteur, geometrie=LineString(ROUTE_SUIVIE, srid=4326),
                                    position_destination='POINT(9.73 4.05)')
        jetons = connecter(self.client, self.conducteur.telephone)
        candidats = [candidat(ROUTE_SUIVIE, 600, 'suivie', 1200), candidat(VARIANTE, 540, 'nord')]
        with patch('trips.services.meilleur_itineraire.ServiceItineraire') as Service:
            Service.return_value.calculer.return_value = candidats
            for _ in range(2):  # stabilite : proposee au 2e appel
                reponse = self.client.post(
                    reverse('trips:trajet-meilleur-itineraire', kwargs={'id': trajet.id}),
                    {'lat': 4.05, 'lon': 9.705, 'heading': 90}, content_type='application/json', **jetons,
                ).json()
        self.assertEqual(PropositionItineraire.objects.count(), 1)
        self.client.post(
            reverse('trips:trajet-changer-itineraire', kwargs={'id': trajet.id}),
            {'lat': 4.05, 'lon': 9.705, 'route': reponse['better_route']}, content_type='application/json', **jetons,
        )
        self.assertTrue(PropositionItineraire.objects.get().acceptee)
        faster = next(c for c in self.lire('pilotage-propositions')['by_cause'] if c['cause'] == 'FASTER')
        self.assertEqual((faster['count'], faster['accepted'], faster['acceptance_pct']), (1, 1, 100.0))

    def test_densite_et_carte_en_direct(self):
        troncon = Troncon.objects.create(
            id='1:0', arete=1, index=0, classe_route='primary', nom_voie='Bd de la Liberte',
            geometrie=LineString([(9.70, 4.05), (9.701, 4.05)], srid=4326), debut_m=0, fin_m=100,
            cellule_h3_res8=0x88596e0d4bfffff,
        )
        trajet = _trajet_actif_pour(self.conducteur)
        for conducteur in ('a', 'b'):
            PassageTroncon.objects.create(troncon=troncon, conducteur=conducteur, trajet_id=trajet.id if conducteur == 'a'
                                          else _trajet_actif_pour(self.conducteur).id,
                                          vitesse_kmh=30, horodatage=timezone.now(), heure_creuse=False)
        densite = self.lire('pilotage-densite')
        self.assertEqual(densite['segments']['driven_in_period'], 1)
        self.assertEqual(sum(h['passages'] for h in densite['by_hour']), 2)

        direct = self.lire('pilotage-direct', south=4.0, west=9.6, north=4.1, east=9.8)
        self.assertEqual(direct['segments'][0]['ratio'], 0.5)  # 30 km/h sur 60 attendus en ville
        self.assertEqual(direct['zones'][0]['drivers'], 2)


class VehiculesDirectTests(TestCase):
    def setUp(self):
        from .services.positions_direct import CLE_MAJ, CLE_POSITIONS
        from .services.producteur_evenements import connexion_redis_telemetrie

        connexion_redis_telemetrie().delete(CLE_MAJ, CLE_POSITIONS)
        self.conducteur = creer_utilisateur(email='conducteur@test.cm')
        self.trajet = _trajet_actif_pour(self.conducteur, type_vehicule='MOTO')
        self.url = reverse('trips:pilotage-vehicules')

    def envoyer_position(self):
        jetons = connecter(self.client, self.conducteur.telephone)
        with patch('trips.views.ProducteurRedisStreams'):
            reponse = self.client.post(reverse('trips:telemetrie-positions'), {
                'trip': str(self.trajet.id),
                'positions': [{'lat': 4.0483, 'lon': 9.7043, 'speed_kmh': 32.5, 'heading': 180,
                               'timestamp': timezone.now().isoformat()}],
            }, content_type='application/json', **jetons)
        self.assertEqual(reponse.status_code, 202)

    def test_points_anonymes_reserves_aux_admins(self):
        self.envoyer_position()
        moderateur = creer_utilisateur(email='modo@easyway.local', is_staff=True)
        self.assertEqual(self.client.get(self.url, **connecter(self.client, moderateur.telephone)).status_code, 403)

        admin = creer_utilisateur(email='admin@easyway.local', is_staff=True, is_superuser=True)
        vehicules = self.client.get(self.url, **connecter(self.client, admin.telephone)).json()['vehicles']
        self.assertEqual(len(vehicules), 1)
        v = vehicules[0]
        self.assertEqual((v['lat'], v['speed_kmh'], v['heading'], v['vehicle_type']), (4.0483, 32.5, 180, 'MOTO'))
        self.assertNotIn(str(self.trajet.id), str(v))  # aucun identifiant reel
        # Hors de la zone demandee : rien.
        hors_zone = self.client.get(self.url, {'south': 3.8, 'west': 11.4, 'north': 3.9, 'east': 11.6},
                                    **connecter(self.client, admin.telephone)).json()['vehicles']
        self.assertEqual(hors_zone, [])


class TraceReelleTests(TestCase):
    def setUp(self):
        self.conducteur = creer_utilisateur(email='conducteur@test.cm')
        self.jetons = connecter(self.client, self.conducteur.telephone)
        self.trajet = _trajet_actif_pour(self.conducteur)

    def envoyer(self, points, debut):
        with patch('trips.views.ProducteurRedisStreams'):
            reponse = self.client.post(reverse('trips:telemetrie-positions'), {
                'trip': str(self.trajet.id),
                'positions': [{'lat': lat, 'lon': lon, 'timestamp': (debut + timedelta(seconds=10 * i)).isoformat()}
                              for i, (lat, lon) in enumerate(points)],
            }, content_type='application/json', **self.jetons)
        self.assertEqual(reponse.status_code, 202)

    def test_chemin_reel_enregistre_et_affiche(self):
        debut = timezone.now()
        self.envoyer([(4.0500, 9.700), (4.0500, 9.70005), (4.0510, 9.701)], debut)  # le 2e a 5 m : ignore
        self.envoyer([(4.0520, 9.702)], debut + timedelta(minutes=1))
        self.trajet.refresh_from_db()
        self.assertEqual(len(self.trajet.trace_reelle.coords), 3)

        staff = creer_utilisateur(email='staff@easyway.local', is_staff=True)
        fiche = self.client.get(reverse('trips:staff-trajet-detail', kwargs={'id': self.trajet.id}),
                                **connecter(self.client, staff.telephone)).json()
        self.assertEqual(fiche['actual_trace'][0], [4.05, 9.7])
        self.trajet.purger_geometrie()
        self.assertIsNone(Trajet.objects.get(pk=self.trajet.pk).trace_reelle)

    def test_signalements_reellement_evites_vs_declares(self):
        from unittest.mock import MagicMock

        from django.contrib.gis.geos import LineString, Point
        self.trajet.geometrie = LineString([(9.70, 4.05), (9.71, 4.05)], srid=4326)
        self.trajet.trace_reelle = LineString([(9.70, 4.06), (9.71, 4.06)], srid=4326)  # ~1,1 km au nord
        self.trajet.incidents_evites = 3  # declare par le conducteur
        self.trajet.save(update_fields=['geometrie', 'trace_reelle', 'incidents_evites'])
        incident = MagicMock(position=Point(9.705, 4.05, srid=4326))  # sur le prevu, loin du reel
        staff = creer_utilisateur(email='staff@easyway.local', is_staff=True)
        with patch('trips.serializers.incidents_actifs_par_topologie', return_value=[incident]):
            fiche = self.client.get(reverse('trips:staff-trajet-detail', kwargs={'id': self.trajet.id}),
                                    **connecter(self.client, staff.telephone)).json()
        self.assertEqual(fiche['incidents_on_route'], 1)
        self.assertEqual(fiche['incidents_avoided_real'], 1)  # le trajet reel l'a contourne
        self.assertEqual(fiche['declared_incidents_avoided'], 3)  # ce que le conducteur a declare

    def test_chronometre_reste_ouvert_corrige(self):
        debut = self.trajet.demarre_le
        self.envoyer([(4.05, 9.70), (4.06, 9.71)], debut + timedelta(minutes=37))  # positions jusqu'a ~37 min
        reponse = self.client.patch(
            reverse('trips:trajet-detail', kwargs={'id': self.trajet.id}),
            {'actual_duration': 37860, 'status': 'TERMINE'}, content_type='application/json', **self.jetons,
        )
        self.assertEqual(reponse.status_code, 200, reponse.content)
        self.trajet.refresh_from_db()
        self.assertAlmostEqual(self.trajet.duree_reelle, 37 * 60 + 10, delta=5)
