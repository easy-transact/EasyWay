"""Signalements officiels (autorites : mairie, police) -- Phase 1, niveau service/tache."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

from accounts.tests import creer_utilisateur
from organisations.models import Organisation, TypeOrganisation

from .models import Incident, StatutIncident, SousTypeIncident as S, TypeIncident as T
from .services import ServiceIncident
from .tasks import activer_programmes
from .tests import patcher_locate_incident, patcher_nominatim_incident


class SignalementOfficielTests(TestCase):
    def setUp(self):
        patcher_nominatim_incident(self, libelle='Bd de la Liberte', ville='Douala', urbain=True)
        patcher_locate_incident(self)
        self.mairie = Organisation.objects.create(
            type=TypeOrganisation.MAIRIE, raison_sociale='Mairie de Douala', ville='Douala',
        )
        self.agent = creer_utilisateur(email='agent@mairie.cm')
        self.position = Point(9.70, 4.05, srid=4326)

    def poser(self, debut, fin, type_=T.VOIE_BLOQUEE, sous_type=S.ROUTE_BARREE):
        return ServiceIncident().signaler_officiel(
            membre=self.agent, organisation=self.mairie, type_incident=type_, position=self.position,
            debut_le=debut, fin_le=fin, sous_type=sous_type,
        )

    def test_officiel_immediat_est_actif_et_confirme(self):
        maintenant = timezone.now()
        with patch('community.services.temps_reel.publier_incident') as publier:
            incident = self.poser(maintenant, maintenant + timedelta(days=3))
        self.assertEqual(incident.statut, StatutIncident.ACTIF)
        self.assertTrue(incident.est_officiel)
        self.assertTrue(incident.valide_par_staff)  # traite comme confirme -> fermeture evitee par l'itineraire
        self.assertEqual(incident.organisation_id, self.mairie.id)
        publier.assert_called_once()

    def test_officiel_programme_reste_cache_puis_active_a_sa_date(self):
        maintenant = timezone.now()
        with patch('community.services.temps_reel.publier_incident') as publier:
            incident = self.poser(maintenant + timedelta(hours=2), maintenant + timedelta(days=2))
        self.assertEqual(incident.statut, StatutIncident.PROGRAMME)
        publier.assert_not_called()  # pas diffuse tant qu'il n'a pas commence

        # Pas encore l'heure : rien ne s'active.
        self.assertEqual(activer_programmes(), 0)

        # La date de debut est passee -> activation + diffusion.
        incident.debut_le = maintenant - timedelta(minutes=1)
        incident.save(update_fields=['debut_le'])
        with patch('community.temps_reel.publier_incident') as publier:
            self.assertEqual(activer_programmes(), 1)
            publier.assert_called_once()
        incident.refresh_from_db()
        self.assertEqual(incident.statut, StatutIncident.ACTIF)

    def test_officiel_expire_normalement(self):
        maintenant = timezone.now()
        with patch('community.services.temps_reel.publier_incident'):
            incident = self.poser(maintenant - timedelta(days=2), maintenant - timedelta(minutes=1))
        # Fin de periode dans le passe : pas repris par activer_programmes, et
        # hors des listes actives (expire_le depasse).
        self.assertEqual(activer_programmes(), 0)
        self.assertFalse(
            Incident.objects.filter(id=incident.id, statut=StatutIncident.ACTIF, expire_le__gt=maintenant).exists()
        )
