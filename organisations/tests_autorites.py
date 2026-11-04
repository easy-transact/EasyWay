"""Signalements officiels des autorites (mairie/police) via /api/org/official-reports/."""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from community.models import Incident, StatutIncident
from community.tests import patcher_locate_incident, patcher_nominatim_incident

from .models import RoleOrganisation, StatutOrganisation, TypeOrganisation
from .tests import OrganisationTestCase, ajouter_membre, creer_organisation


class SignalementsOfficielsApiTests(OrganisationTestCase):
    def setUp(self):
        patcher_nominatim_incident(self, libelle='Bd de la Liberte', ville='Douala', urbain=True)
        patcher_locate_incident(self)
        self.mairie = creer_organisation('Mairie de Douala', type=TypeOrganisation.MAIRIE)
        self.gestionnaire = ajouter_membre(self.mairie, RoleOrganisation.GESTIONNAIRE)

    def _corps(self, **extra):
        maintenant = timezone.now()
        corps = {
            'type': 'VOIE_BLOQUEE', 'subtype': 'ROUTE_BARREE', 'lat': 4.05, 'lon': 9.70,
            'ends_at': (maintenant + timedelta(days=3)).isoformat(),
        }
        corps.update(extra)
        return corps

    def test_poser_un_signalement_officiel(self):
        reponse = self.envoyer('post', 'signalements-officiels', self.gestionnaire, corps=self._corps())
        self.assertEqual(reponse.status_code, 201, reponse.content)
        self.assertTrue(reponse.json()['official'])
        self.assertEqual(reponse.json()['official_source']['type'], 'MAIRIE')
        incident = Incident.objects.get()
        self.assertEqual((incident.statut, incident.valide_par_staff), (StatutIncident.ACTIF, True))
        self.assertEqual(incident.organisation_id, self.mairie.id)

    def test_programme_dans_le_futur(self):
        debut = (timezone.now() + timedelta(days=1)).isoformat()
        fin = (timezone.now() + timedelta(days=2)).isoformat()
        reponse = self.envoyer('post', 'signalements-officiels', self.gestionnaire,
                               corps=self._corps(starts_at=debut, ends_at=fin))
        self.assertEqual(reponse.status_code, 201, reponse.content)
        self.assertEqual(reponse.json()['status'], StatutIncident.PROGRAMME)

    def test_fin_avant_debut_refusee(self):
        passe = (timezone.now() - timedelta(hours=1)).isoformat()
        reponse = self.envoyer('post', 'signalements-officiels', self.gestionnaire, corps=self._corps(ends_at=passe))
        self.assertEqual(reponse.status_code, 400)

    def test_organisation_non_autorite_refusee(self):
        flotte = creer_organisation('Transport SARL', type=TypeOrganisation.FLOTTE)
        gestionnaire = ajouter_membre(flotte, RoleOrganisation.GESTIONNAIRE)
        reponse = self.envoyer('post', 'signalements-officiels', gestionnaire, corps=self._corps())
        self.assertEqual(reponse.status_code, 403)

    def test_lecteur_ne_peut_pas_poser(self):
        lecteur = ajouter_membre(self.mairie, RoleOrganisation.LECTEUR)
        reponse = self.envoyer('post', 'signalements-officiels', lecteur, corps=self._corps())
        self.assertEqual(reponse.status_code, 403)

    def test_liste_et_levee(self):
        self.envoyer('post', 'signalements-officiels', self.gestionnaire, corps=self._corps())
        incident = Incident.objects.get()
        liste = self.get('signalements-officiels', self.gestionnaire).json()
        self.assertEqual(len(liste), 1)
        reponse = self.envoyer('delete', 'signalement-officiel-detail', self.gestionnaire, incident.id)
        self.assertEqual(reponse.status_code, 204)
        incident.refresh_from_db()
        self.assertEqual(incident.statut, StatutIncident.RETIRE)
