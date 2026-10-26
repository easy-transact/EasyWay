"""Cloture des trajets restes ACTIF sans signe de vie (trips.tasks.abandonner_trajets_inactifs)
et points des trajets termines (settings.POINTS_TRAJETS_ACTIF)."""

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.tests import creer_utilisateur

from .models import POINTS_PAR_TRAJET_TERMINE, StatutTrajet, Trajet
from .tasks import DELAI_ABANDON_TRAJET, abandonner_trajets_inactifs


class BaseTrajetTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()
        self.points_depart = self.utilisateur.points

    def trajet(self, **extra):
        valeurs = dict(
            utilisateur=self.utilisateur, position_origine='POINT(9.7043 4.0483)', libelle_origine='A',
            position_destination='POINT(9.6970 4.0469)', libelle_destination='B', itineraire_choisi='x',
            distance_prevue=1280, duree_prevue=108, statut=StatutTrajet.ACTIF,
        )
        valeurs.update(extra)
        return Trajet.objects.create(**valeurs)


class AbandonTrajetsTests(BaseTrajetTests):
    def test_trajet_sans_signe_de_vie_abandonne(self):
        maintenant = timezone.now()
        derniere = maintenant - DELAI_ABANDON_TRAJET - timedelta(minutes=5)
        trajet = self.trajet(demarre_le=maintenant - timedelta(hours=4), telemetrie_derniere_le=derniere)

        self.assertEqual(abandonner_trajets_inactifs(), 1)
        trajet.refresh_from_db()
        self.assertEqual(trajet.statut, StatutTrajet.ABANDONNE)
        self.assertEqual(trajet.termine_le, derniere)
        self.assertEqual(trajet.duree_reelle, int((derniere - trajet.demarre_le).total_seconds()))
        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.points, self.points_depart)  # pas de points pour un abandon

    def test_trajet_recent_ou_termine_intact(self):
        maintenant = timezone.now()
        # Demarre il y a longtemps mais position recente : toujours en route.
        en_route = self.trajet(demarre_le=maintenant - timedelta(hours=5), derniere_position_le=maintenant - timedelta(minutes=10))
        termine = self.trajet(demarre_le=maintenant - timedelta(days=1), statut=StatutTrajet.TERMINE)

        self.assertEqual(abandonner_trajets_inactifs(), 0)
        en_route.refresh_from_db()
        termine.refresh_from_db()
        self.assertEqual((en_route.statut, termine.statut), (StatutTrajet.ACTIF, StatutTrajet.TERMINE))


class PointsTrajetTermineTests(BaseTrajetTests):
    def test_pas_de_points_par_defaut(self):
        trajet = self.trajet()
        trajet.terminer()
        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.points, self.points_depart)

    @override_settings(POINTS_TRAJETS_ACTIF=True)
    def test_points_si_option_activee(self):
        trajet = self.trajet()
        trajet.terminer()
        self.utilisateur.refresh_from_db()
        self.assertEqual(self.utilisateur.points, self.points_depart + POINTS_PAR_TRAJET_TERMINE)
