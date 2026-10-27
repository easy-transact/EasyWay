"""Etat des routes V1 : tranches, passages, rattachement des signalements,
calcul nocturne, endpoints."""

import uuid

import h3
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.contrib.gis.geos import LineString, Point
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.tests import connecter, creer_utilisateur
from community.models import Incident, SensVote, StatutIncident, Vote
from trips.models import StatutTrajet, Trajet

from .models import ClasseEtat, NoteTroncon, PassageTroncon, SignalementTroncon, Troncon
from .services import calcul, score_trajet, troncons
from .services.reglages import DEFAUTS

# Arete de 500 m plein est, lat 4.05, de lon 9.700 a ~9.7045.
ARETE = {'id': 111, 'length': 0.5, 'way_id': 9, 'road_class': 'primary', 'names': ['Boulevard X']}
LON_PAR_M = 1 / (111_320 * 0.99750)


def point(offset_m, edge_index=0):
    return {'edge_index': edge_index, 'distance_along_edge': offset_m / 500, 'lat': 4.05, 'lon': 9.70 + offset_m * LON_PAR_M}


def creer_troncon(tid='111:1', **extra):
    valeurs = dict(
        id=tid, arete=111, index=int(tid.split(':')[1]), way_id=9, classe_route='primary', nom_voie='Boulevard X',
        geometrie=LineString([(9.701, 4.05), (9.7019, 4.05)], srid=4326), debut_m=100, fin_m=199,
        cellule_h3_res8=h3.str_to_int(h3.latlng_to_cell(4.05, 9.7015, 8)), ralentissement_normal=False,
    )
    valeurs.update(extra)
    return Troncon.objects.create(**valeurs)


class TranchesParcouruesTests(SimpleTestCase):
    def test_decoupage_en_tranches_de_100_m(self):
        aretes = [ARETE, {'id': 222, 'length': 0.25}]
        points = [point(150), point(400), {'edge_index': 1, 'distance_along_edge': 0.4}]
        tranches = score_trajet.tranches_parcourues(aretes, points)
        self.assertEqual([t for t, _, _ in tranches], ['111:1', '111:2', '111:3', '111:4', '222:0'])
        longueurs = [round(l) for _, l, _ in tranches]
        # Depart a 150 m sur la premiere arete, arrivee a 100 m sur la seconde.
        self.assertEqual(longueurs, [50, 100, 100, 100, 100])
        self.assertEqual(round(tranches[-1][2]), 350)

    def test_completer_ajoute_l_inconnu(self):
        etat = {'good_m': 400, 'degraded_m': 100, 'bad_m': 0, 'flood_prone_m': 0, 'worst': []}
        self.assertEqual(score_trajet.completer(etat, 1000)['unknown_m'], 500)
        self.assertIsNone(score_trajet.completer(None, 1000))


class PassagesTests(TestCase):
    def setUp(self):
        self.utilisateur = creer_utilisateur()
        self.trajet = Trajet.objects.create(
            utilisateur=self.utilisateur, position_origine='POINT(9.60 4.00)', libelle_origine='A',
            position_destination='POINT(9.80 4.10)', libelle_destination='B', itineraire_choisi='x',
            distance_prevue=1000, duree_prevue=100, statut=StatutTrajet.ACTIF, type_vehicule='VOITURE',
        )
        self.debut = datetime(2026, 10, 26, 23, 0, tzinfo=dt_timezone.utc)  # minuit a Douala : heure creuse

    def paires(self, offsets_s):
        return [
            ('id', {'trajet_id': str(self.trajet.id), 'lat': 4.05, 'lon': 9.70 + o * LON_PAR_M,
                    'horodatage': (self.debut + timedelta(seconds=s)).isoformat()})
            for o, s in offsets_s
        ]

    def test_un_passage_par_tranche_avec_sa_vitesse(self):
        # 110 -> 190 m en 8 s : 36 km/h sur la tranche 1.
        paires = self.paires([(110, 0), (190, 8)])
        nb = troncons.enregistrer_passages(str(self.trajet.id), [ARETE], [point(110), point(190)], paires, DEFAUTS)
        self.assertEqual(nb, 1)
        passage = PassageTroncon.objects.get()
        self.assertEqual((passage.troncon_id, passage.vitesse_kmh, passage.heure_creuse), ('111:1', 36.0, True))
        troncon = Troncon.objects.get()
        self.assertEqual((troncon.way_id, troncon.nom_voie, troncon.classe_route), (9, 'Boulevard X', 'primary'))
        self.assertNotEqual(passage.conducteur, str(self.utilisateur.id))  # anonymise

    def test_pres_du_depart_ignore(self):
        self.trajet.position_origine = Point(9.7012, 4.05, srid=4326)
        self.trajet.save()
        paires = self.paires([(110, 0), (190, 8)])
        self.assertEqual(troncons.enregistrer_passages(str(self.trajet.id), [ARETE], [point(110), point(190)], paires, DEFAUTS), 0)

    def test_embouteillage_actif_ignore(self):
        Incident.objects.create(
            auteur=self.utilisateur, type='EMBOUTEILLAGE', sous_type='A_L_ARRET',
            position=Point(9.7013, 4.05, srid=4326), statut=StatutIncident.ACTIF,
            expire_le=self.debut + timedelta(hours=1),
        )
        Incident.objects.filter().update(cree_le=self.debut - timedelta(minutes=10))
        paires = self.paires([(110, 0), (190, 8)])
        self.assertEqual(troncons.enregistrer_passages(str(self.trajet.id), [ARETE], [point(110), point(190)], paires, DEFAUTS), 0)

    def test_geometrie_etendue_par_un_autre_passage(self):
        troncons.enregistrer_passages(str(self.trajet.id), [ARETE], [point(120), point(150)], self.paires([(120, 0), (150, 3)]), DEFAUTS)
        troncons.enregistrer_passages(str(self.trajet.id), [ARETE], [point(105), point(195)], self.paires([(105, 0), (195, 9)]), DEFAUTS)
        troncon = Troncon.objects.get()
        self.assertEqual((round(troncon.debut_m), round(troncon.fin_m)), (105, 195))


class RattachementTests(TestCase):
    def test_signalement_rattache_a_sa_tranche(self):
        auteur = creer_utilisateur()
        incident = Incident.objects.create(
            auteur=auteur, type='DANGER', sous_type='NID_DE_POULE', position=Point(9.7015, 4.05, srid=4326),
            cap=90, statut=StatutIncident.ACTIF, expire_le=timezone.now() + timedelta(days=30),
        )
        with patch('trips.services.client_meili.tracer', return_value=([ARETE], [point(150), point(170)])):
            troncon = troncons.rattacher_signalement(incident)
        self.assertEqual(troncon.id, '111:1')
        self.assertEqual(SignalementTroncon.objects.get().troncon_id, '111:1')


class CalculNotesTests(TestCase):
    def setUp(self):
        self.troncon = creer_troncon()
        self.maintenant = timezone.now()

    def nid(self, auteur, jours=1, confirmations=0, infirmations=0):
        incident = Incident.objects.create(
            auteur=auteur, type='DANGER', sous_type='NID_DE_POULE', position=Point(9.7015, 4.05, srid=4326),
            statut=StatutIncident.ACTIF, expire_le=self.maintenant + timedelta(days=30),
            confirmations=confirmations, infirmations=infirmations,
        )
        Incident.objects.filter(pk=incident.pk).update(cree_le=self.maintenant - timedelta(days=jours))
        SignalementTroncon.objects.create(incident=incident, troncon=self.troncon)
        return incident

    def passages(self, n, conducteurs, vitesse, creuse=True):
        for i in range(n):
            PassageTroncon.objects.create(
                troncon=self.troncon, conducteur=f'c{i % conducteurs}', trajet_id=uuid.uuid4(), vitesse_kmh=vitesse,
                horodatage=self.maintenant - timedelta(days=1), heure_creuse=creuse,
            )

    def noter(self):
        with patch('etat_routes.services.calcul.rattacher_nouveaux_signalements'):
            calcul.calculer_notes(self.maintenant)
        return NoteTroncon.objects.get(troncon=self.troncon)

    def test_nids_de_poule_de_deux_conducteurs_donnent_mauvais(self):
        self.nid(creer_utilisateur(), confirmations=2)
        self.nid(creer_utilisateur('b@easyway.local'), confirmations=2)
        note = self.noter()
        # 2 x 25 x (1 + 0,25 x 2) = 75, plafonne a 50 -> note 50 : DEGRADE.
        self.assertEqual((note.note, note.classe, note.causes), (50, ClasseEtat.DEGRADE, ['NID_DE_POULE']))

    def test_un_seul_conducteur_reste_inconnu(self):
        auteur = creer_utilisateur()
        self.nid(auteur)
        self.nid(auteur)  # meme auteur : un seul temoin
        self.assertEqual(self.noter().classe, ClasseEtat.INCONNU)

    def test_confirmation_d_un_autre_compte_comme_temoin(self):
        auteur, votant = creer_utilisateur(), creer_utilisateur('v@easyway.local')
        incident = self.nid(auteur, confirmations=1)
        Vote.objects.create(incident=incident, votant=votant, sens=SensVote.CONFIRMATION, poids=1)
        # Un seul signalement : 25 x 1,25 = 31 -> note 69, deux temoins.
        self.assertEqual(self.noter().classe, ClasseEtat.DEGRADE)

    def test_signalement_conteste_ne_pese_rien(self):
        self.nid(creer_utilisateur(), confirmations=0, infirmations=2)
        self.assertEqual(self.noter().penalite_signalements, 0)

    def test_route_sans_passage_jamais_bonne(self):
        self.passages(5, 5, 60)  # trop peu de passages
        self.assertEqual(self.noter().classe, ClasseEtat.INCONNU)

    def test_assez_de_passages_rapides_donnent_bon(self):
        self.passages(20, 5, 55)  # 55 / 60 > 0,8
        note = self.noter()
        self.assertEqual((note.note, note.classe), (100, ClasseEtat.BON))

    def test_lenteur_de_nuit_donne_une_penalite(self):
        self.passages(20, 5, 18)  # 18 / 60 = 0,3 < 0,4 -> 40
        note = self.noter()
        self.assertEqual((note.penalite_vitesse, note.note, note.classe), (40, 60, ClasseEtat.DEGRADE))
        self.assertIn('VITESSE', note.causes)

    def test_lenteur_aux_heures_pleines_ignoree(self):
        self.passages(20, 5, 10, creuse=False)
        self.assertEqual(self.noter().penalite_vitesse, 0)

    def test_historique_hebdomadaire(self):
        self.passages(20, 5, 55)
        self.noter()
        self.assertEqual(self.troncon.historique.count(), 1)


class EndpointsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.degrade = creer_troncon('111:1')
        NoteTroncon.objects.create(troncon=self.degrade, note=50, classe=ClasseEtat.DEGRADE, confiance=0.5,
                                   causes=['NID_DE_POULE'], nb_conducteurs=6)
        bon = creer_troncon('111:2')
        NoteTroncon.objects.create(troncon=bon, note=90, classe=ClasseEtat.BON, nb_conducteurs=6)
        self.cellule = h3.int_to_str(self.degrade.cellule_h3_res8)

    def test_carte_ne_renvoie_que_les_troncons_degrades(self):
        reponse = self.client.get(reverse('etat_routes:carte'), {'cells': self.cellule})
        self.assertEqual(reponse.status_code, 200, reponse.content)
        self.assertEqual([r['segment_id'] for r in reponse.json()], ['111:1'])
        self.assertEqual(reponse.json()[0]['class'], 'DEGRADE')

    def test_carte_valide_les_cellules(self):
        self.assertEqual(self.client.get(reverse('etat_routes:carte'), {'cells': 'abc'}).status_code, 400)
        self.assertEqual(self.client.get(reverse('etat_routes:carte')).status_code, 400)

    def test_classement_reserve_a_la_permission_dediee(self):
        staff = creer_utilisateur('staff@easyway.local', is_staff=True)
        jetons = connecter(self.client, staff.telephone)
        url = reverse('etat_routes:classement')
        self.assertEqual(self.client.get(url, **jetons).status_code, 403)
        staff.user_permissions.add(Permission.objects.get(codename='exporter_classement'))
        reponse = self.client.get(url, **jetons)
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual([r['segment_id'] for r in reponse.json()['results']], ['111:1'])
        csv = self.client.get(url, {'format': 'csv'}, **jetons)
        self.assertTrue(csv['Content-Type'].startswith('text/csv'))
        self.assertIn('Boulevard X', csv.content.decode())
        geojson = self.client.get(url, {'format': 'geojson'}, **jetons).json()
        self.assertEqual(geojson['features'][0]['geometry']['type'], 'LineString')

    def test_classement_anonymat_5_conducteurs(self):
        NoteTroncon.objects.filter(troncon=self.degrade).update(nb_conducteurs=4)
        staff = creer_utilisateur('staff@easyway.local', is_staff=True, is_superuser=True)
        reponse = self.client.get(reverse('etat_routes:classement'), **connecter(self.client, staff.telephone))
        self.assertEqual(reponse.json()['count'], 0)

    def test_reglages(self):
        staff = creer_utilisateur('staff@easyway.local', is_staff=True)
        jetons = connecter(self.client, staff.telephone)
        url = reverse('etat_routes:reglages')
        self.assertEqual(self.client.get(url, **jetons).json()['values']['seuil_bon'], 70)
        reponse = self.client.patch(url, {'values': {'seuil_bon': 75}}, content_type='application/json', **jetons)
        self.assertEqual(reponse.json()['values']['seuil_bon'], 75)
        mauvais = self.client.patch(url, {'values': {'inconnu': 1}}, content_type='application/json', **jetons)
        self.assertEqual(mauvais.status_code, 400)


class RepliAttributsValhallaTests(SimpleTestCase):
    def test_attribut_refuse_reessaye_avec_les_attributs_de_base(self):
        from unittest.mock import Mock

        from trips.services import client_meili

        refus = Mock(status_code=400)
        ok = Mock(status_code=200)
        ok.json.return_value = {'edges': [{'id': 1}], 'matched_points': []}
        with patch('trips.services.client_meili.requests.post', side_effect=[refus, ok]) as post, \
                patch('trips.services.client_meili.disjoncteur'):
            edges, _ = client_meili.tracer([{'lat': 4, 'lon': 9}], attributs=troncons.ATTRIBUTS_TRONCON)
        self.assertEqual(edges, [{'id': 1}])
        self.assertEqual(post.call_args.kwargs['json']['filters']['attributes'], client_meili.ATTRIBUTS_DEFAUT)
