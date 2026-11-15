"""Validation passive des signalements par la telemetrie (cf.
community/validation_passive.py)."""

from datetime import timedelta

from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from accounts.tests import creer_utilisateur
from trips.models import StatutTrajet, Trajet

from .models import Incident, SensVote, SousTypeIncident, StatutIncident, TypeIncident, Vote
from .validation_passive import observer_passages


def passage(vitesse_kmh, cap=90, debut=None, lat=4.05):
    """Trois positions qui traversent (9.70, lat) d'ouest en est (cap 90)."""
    debut = debut or timezone.now() - timedelta(seconds=30)
    return [
        {'lat': lat, 'lon': lon, 'vitesse_kmh': vitesse_kmh, 'cap': cap,
         'horodatage': (debut + timedelta(seconds=5 * i)).isoformat()}
        for i, lon in enumerate((9.6998, 9.7000, 9.7002))
    ]


class ValidationPassiveTests(TestCase):
    def setUp(self):
        cache.clear()
        self.auteur = creer_utilisateur('auteur@easyway.local')
        self.conducteur = creer_utilisateur('conducteur@easyway.local')
        self.trajet = self.trajet_de(self.conducteur, 'VOITURE')

    def trajet_de(self, utilisateur, type_vehicule):
        return Trajet.objects.create(
            utilisateur=utilisateur, position_origine='POINT(9.60 4.00)', libelle_origine='A',
            position_destination='POINT(9.90 4.20)', libelle_destination='B', itineraire_choisi='x',
            distance_prevue=1000, duree_prevue=100, statut=StatutTrajet.ACTIF, type_vehicule=type_vehicule,
        )

    def signaler(self, type_, sous_type, **extra):
        valeurs = dict(
            auteur=self.auteur, type=type_, sous_type=sous_type, position=Point(9.70, 4.05, srid=4326), cap=90,
            statut=StatutIncident.ACTIF, score_confiance=3, confirmations=3,
            expire_le=timezone.now() + timedelta(hours=2),
        )
        valeurs.update(extra)
        incident = Incident.objects.create(**valeurs)
        # cree_le auto : le placer avant le passage
        Incident.objects.filter(pk=incident.pk).update(cree_le=timezone.now() - timedelta(minutes=5))
        incident.refresh_from_db()
        return incident

    def test_route_barree_franchie_infirmee_sans_points(self):
        incident = self.signaler(TypeIncident.VOIE_BLOQUEE, SousTypeIncident.ROUTE_BARREE)
        points_avant = self.conducteur.points
        self.assertEqual(observer_passages(self.trajet.id, passage(40)), 1)
        vote = Vote.objects.get()
        self.assertEqual((vote.sens, vote.passif, vote.votant_id), (SensVote.INFIRMATION, True, self.conducteur.id))
        self.assertEqual(vote.poids, self.conducteur.poids_de_vote() / 2)
        incident.refresh_from_db()
        self.assertEqual(incident.infirmations, 1)
        self.conducteur.refresh_from_db()
        self.assertEqual(self.conducteur.points, points_avant)

    def test_embouteillage_traverse_au_pas_confirme(self):
        incident = self.signaler(TypeIncident.EMBOUTEILLAGE, SousTypeIncident.A_L_ARRET, confirmations=0,
                                 score_confiance=0, statut=StatutIncident.EN_ATTENTE)
        observer_passages(self.trajet.id, passage(5))
        self.assertEqual(Vote.objects.get().sens, SensVote.CONFIRMATION)
        incident.refresh_from_db()
        self.assertEqual(incident.confirmations, 1)
        self.conducteur.refresh_from_db()
        self.assertEqual(self.conducteur.points, 0)  # passif : aucun point de recompense

    def test_embouteillage_traverse_vite_infirme_entre_les_deux_rien(self):
        self.signaler(TypeIncident.EMBOUTEILLAGE, SousTypeIncident.A_L_ARRET)
        observer_passages(self.trajet.id, passage(25))
        self.assertFalse(Vote.objects.exists())
        observer_passages(self.trajet.id, passage(50))
        self.assertEqual(Vote.objects.get().sens, SensVote.INFIRMATION)

    def test_un_seul_vote_par_conducteur(self):
        self.signaler(TypeIncident.EMBOUTEILLAGE, SousTypeIncident.A_L_ARRET)
        observer_passages(self.trajet.id, passage(5))
        observer_passages(self.trajet.id, passage(5))
        self.assertEqual(Vote.objects.count(), 1)

    def test_aucun_vote(self):
        cas = {
            'autre sens': dict(points=passage(40, cap=270)),
            'rue parallele (~110 m)': dict(points=passage(40, lat=4.051)),
            'passage avant le signalement': dict(points=passage(40, debut=timezone.now() - timedelta(hours=1))),
        }
        self.signaler(TypeIncident.VOIE_BLOQUEE, SousTypeIncident.ROUTE_BARREE)
        for nom, donnees in cas.items():
            observer_passages(self.trajet.id, donnees['points'])
            self.assertFalse(Vote.objects.exists(), nom)

    def test_accident_moto_et_auteur_sans_vote(self):
        self.signaler(TypeIncident.ACCIDENT, SousTypeIncident.ACCIDENT)  # la vitesse ne dit rien d'un accident
        observer_passages(self.trajet.id, passage(50))
        self.assertFalse(Vote.objects.exists())

        Incident.objects.all().delete()
        self.signaler(TypeIncident.VOIE_BLOQUEE, SousTypeIncident.ROUTE_BARREE)
        moto = self.trajet_de(creer_utilisateur('moto@easyway.local'), 'MOTO')
        observer_passages(moto.id, passage(40))  # une moto passe ou une voiture ne passe pas
        auteur = self.trajet_de(self.auteur, 'VOITURE')
        observer_passages(auteur.id, passage(40))
        self.assertFalse(Vote.objects.exists())

    def test_vote_a_la_main_remplace_le_vote_passif(self):
        from unittest.mock import patch

        from django.urls import reverse

        from accounts.tests import connecter

        incident = self.signaler(TypeIncident.EMBOUTEILLAGE, SousTypeIncident.A_L_ARRET, confirmations=0,
                                 score_confiance=0, statut=StatutIncident.EN_ATTENTE)
        observer_passages(self.trajet.id, passage(5))  # passif : confirmation a demi-poids
        with patch('trips.services.derniere_position.lire', return_value=(4.05, 9.70)):
            reponse = self.client.post(
                reverse('community:incident-vote', kwargs={'id': incident.id}), {'direction': 'dispute'},
                content_type='application/json', **connecter(self.client, self.conducteur.telephone),
            )
        self.assertEqual(reponse.status_code, 200, reponse.content)
        vote = Vote.objects.get()
        self.assertEqual((vote.sens, vote.passif), (SensVote.INFIRMATION, False))
        incident.refresh_from_db()
        self.assertEqual((incident.confirmations, incident.infirmations), (0, 1))
