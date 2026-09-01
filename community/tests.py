from decimal import Decimal
from unittest.mock import patch

from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Parametres, Utilisateur
from accounts.tests import connecter
from places.utils import normaliser
from trips.polyline import encoder_polyline6
from trips.services import client_locate
from trips.services.disjoncteur import DisjoncteurOuvert

from .cache_incidents import cle_cache_cellule, ecriture_recente, invalider_cache_cellule
from .models import Incident, StatutIncident, TypeIncident, Vote
from .services import PositionHorsRoute, QuotaDepasse, ServiceIncident
from .tasks import expirer_incidents

MOT_DE_PASSE = 'CorrectHorse9!'
DOUALA_LAT, DOUALA_LON = 4.0483, 9.7043


def creer_utilisateur(email='user@easyway.local', score_reputation=Decimal('0'), **extra):
    telephone = extra.pop('telephone', None)
    if not telephone:
        telephone = email
    utilisateur = Utilisateur.objects.create_user(
        telephone=telephone, email=email, password=MOT_DE_PASSE, nom_complet='Test User',
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


def patcher_nominatim_incident(test_case, libelle=None, ville=None):
    patcheur = patch('community.services.ClientNominatim')
    classe_simulee = patcheur.start()
    classe_simulee.return_value.inverser.return_value = (
        {'label': libelle, 'city': ville or '', 'source': 'nominatim'} if libelle else None
    )
    test_case.addCleanup(patcheur.stop)


def patcher_locate_incident(test_case, distance_m=0, destination_only=False, lat=None, lon=None, effet_de_bord=None):
    """Par defaut simule une position sur la route (distance 0m, calee sur le
    meme point que celui teste) pour ne pas perturber les tests qui ne
    portent pas sur cette verification -- cf. patcher_nominatim_incident
    ci-dessus, meme logique. distance_m=None simule "aucune route connue
    dans la zone" (localiser() renvoie None). lat/lon : force le point
    correle renvoye (sinon identique au point soumis -- pas de correction)."""
    patcheur = patch('community.services.client_locate.localiser')
    fonction_simulee = patcheur.start()
    if effet_de_bord is not None:
        fonction_simulee.side_effect = effet_de_bord
    elif distance_m is None:
        fonction_simulee.return_value = None
    else:
        def _repondre(lat_in, lon_in):
            return {
                'distance_m': distance_m,
                'lat': lat_in if lat is None else lat,
                'lon': lon_in if lon is None else lon,
                'destination_only': destination_only,
                'use': 'driveway' if destination_only else 'road',
            }
        fonction_simulee.side_effect = _repondre
    test_case.addCleanup(patcheur.stop)
    return fonction_simulee


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
        patcher_locate_incident(self)
        cache.clear()

    def _point(self, decalage_lat=0, decalage_lon=0):
        return Point(DOUALA_LON + decalage_lon, DOUALA_LAT + decalage_lat, srid=4326)

    def test_signalement_toujours_cree_en_attente_quelle_que_soit_la_reputation(self):
        # Plus de raccourci "auteur repute -> ACTIF direct" : tout signalement
        # part EN_ATTENTE et n'est promu que par confirmer() une fois corrobore
        # (cf. Incident.seuil_validation).
        incident, doublon = ServiceIncident().signaler(
            creer_utilisateur(score_reputation=Decimal('50')), TypeIncident.EMBOUTEILLAGE, self._point()
        )
        self.assertFalse(doublon)
        self.assertEqual(incident.statut, StatutIncident.EN_ATTENTE)


    @override_settings(QUOTA_SIGNALEMENTS_ACTIF=True)
    def test_quota_horaire_depasse(self):
        utilisateur = creer_utilisateur()
        for i in range(10):
            creer_incident(utilisateur, type_incident=TypeIncident.DANGER, lat=DOUALA_LAT + i * 0.05)
        with self.assertRaises(QuotaDepasse):
            ServiceIncident().signaler(utilisateur, TypeIncident.EMBOUTEILLAGE, self._point())

    @override_settings(QUOTA_SIGNALEMENTS_ACTIF=False)
    def test_quota_desactive_ne_bloque_pas(self):
        # QUOTA_SIGNALEMENTS_ACTIF=False (defaut actuel, cf. settings.py) --
        # desactive temporairement pour ne pas bloquer les tests manuels.
        utilisateur = creer_utilisateur()
        for i in range(15):
            creer_incident(utilisateur, type_incident=TypeIncident.DANGER, lat=DOUALA_LAT + i * 0.05)
        incident, _ = ServiceIncident().signaler(utilisateur, TypeIncident.EMBOUTEILLAGE, self._point())
        self.assertIsNotNone(incident.id)

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

    def test_candidat_statut_actif_mais_deja_expire_ne_dedoublonne_pas(self):
        # Reproduit un cas trouve en verification live : statut='ACTIF' en
        # base ne veut rien dire si expire_le est deja passe -- la tache
        # periodique qui le repasserait a EXPIRE n'a pas forcement encore
        # tourne (jusqu'a 60s de retard, ou plus si elle est en panne).
        # Corroborer un tel candidat le laisserait invisible partout (son
        # expire_le resterait dans le passe, +10 minutes ne suffisant pas a
        # le faire revenir apres maintenant).
        perime = creer_incident(
            creer_utilisateur('a@easyway.local'), type_incident=TypeIncident.EMBOUTEILLAGE,
            expire_le=timezone.now() - timezone.timedelta(minutes=5),
        )
        incident, doublon = ServiceIncident().signaler(
            creer_utilisateur('b@easyway.local'), TypeIncident.EMBOUTEILLAGE, self._point(0.0005, 0.0005)
        )
        self.assertFalse(doublon)
        self.assertNotEqual(incident.id, perime.id)
        self.assertEqual(Incident.objects.count(), 2)

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
        self.assertEqual(incident.ville, '')
        self.assertEqual(incident.ville_normalisee, '')

    def test_ville_depuis_nominatim_normalisee_sans_accents(self):
        patcher_nominatim_incident(self, libelle='Avenue Test', ville='Yaoundé')
        incident, _ = ServiceIncident().signaler(creer_utilisateur(), TypeIncident.EMBOUTEILLAGE, self._point())
        self.assertEqual(incident.ville, 'Yaoundé')
        self.assertEqual(incident.ville_normalisee, 'yaounde')

    def test_position_loin_de_la_route_rejetee(self):
        patcher_locate_incident(self, distance_m=51)
        with self.assertRaises(PositionHorsRoute):
            ServiceIncident().signaler(creer_utilisateur(), TypeIncident.EMBOUTEILLAGE, self._point())
        self.assertEqual(Incident.objects.count(), 0)

    def test_position_juste_sous_le_seuil_acceptee(self):
        patcher_locate_incident(self, distance_m=49)
        incident, _ = ServiceIncident().signaler(creer_utilisateur(), TypeIncident.EMBOUTEILLAGE, self._point())
        self.assertIsNotNone(incident.id)

    def test_locate_ne_trouve_aucune_route_pas_derreur(self):
        patcher_locate_incident(self, distance_m=None)
        incident, _ = ServiceIncident().signaler(creer_utilisateur(), TypeIncident.EMBOUTEILLAGE, self._point())
        self.assertIsNotNone(incident.id)

    def test_allee_privee_rejetee_meme_a_distance_nulle(self):
        # Reproduit le cas trouve en verification live : un point cale sur
        # une allee privee/un parking (destination_only=True) doit etre
        # rejete, meme si la distance a cette arete est nulle/faible --
        # la distance seule ne dit rien sur la nature publique de la route.
        patcher_locate_incident(self, distance_m=0, destination_only=True)
        with self.assertRaises(PositionHorsRoute):
            ServiceIncident().signaler(creer_utilisateur(), TypeIncident.EMBOUTEILLAGE, self._point())
        self.assertEqual(Incident.objects.count(), 0)

    def test_position_calee_sur_larete_routiere_trouvee(self):
        # Le point stocke n'est pas force le point brut soumis : cale sur
        # correlated_lat/lon pour que le marqueur tombe exactement sur la
        # route, pas a quelques metres a cote (cf. capture d'ecran frontend).
        lat_corrige, lon_corrige = DOUALA_LAT + 0.0001, DOUALA_LON + 0.0001
        patcher_locate_incident(self, distance_m=15, lat=lat_corrige, lon=lon_corrige)
        incident, _ = ServiceIncident().signaler(creer_utilisateur(), TypeIncident.EMBOUTEILLAGE, self._point())
        self.assertAlmostEqual(incident.position.y, lat_corrige, places=6)
        self.assertAlmostEqual(incident.position.x, lon_corrige, places=6)

    def test_position_gardee_brute_si_verification_ignoree(self):
        # Valhalla indisponible : la position n'est pas alteree.
        patcher_locate_incident(self, effet_de_bord=client_locate.ErreurLocate('panne'))
        point = self._point()
        incident, _ = ServiceIncident().signaler(creer_utilisateur(), TypeIncident.EMBOUTEILLAGE, point)
        self.assertEqual(incident.position.y, point.y)
        self.assertEqual(incident.position.x, point.x)

    def test_valhalla_indisponible_ne_bloque_pas_le_signalement(self):
        patcher_locate_incident(self, effet_de_bord=client_locate.ErreurLocate('panne'))
        incident, _ = ServiceIncident().signaler(creer_utilisateur(), TypeIncident.EMBOUTEILLAGE, self._point())
        self.assertIsNotNone(incident.id)

    def test_disjoncteur_ouvert_ne_bloque_pas_le_signalement(self):
        patcher_locate_incident(self, effet_de_bord=DisjoncteurOuvert())
        incident, _ = ServiceIncident().signaler(creer_utilisateur(), TypeIncident.EMBOUTEILLAGE, self._point())
        self.assertIsNotNone(incident.id)


class IncidentCreationApiTests(TestCase):
    def setUp(self):
        patcher_nominatim_incident(self)
        patcher_locate_incident(self)
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

    def test_position_hors_route_rejetee_en_400(self):
        patcher_locate_incident(self, distance_m=200)
        reponse = self.client.post(
            reverse('community:incidents'), self._payload(), content_type='application/json',
            HTTP_IDEMPOTENCY_KEY='cle-hors-route', **self.jetons,
        )
        self.assertEqual(reponse.status_code, 400)
        self.assertEqual(Incident.objects.count(), 0)


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

    def test_trop_de_cellules_rejete(self):
        from community.views import MAX_CELLULES
        cellules = ','.join(f'fake{i}' for i in range(MAX_CELLULES + 1))
        reponse = self.client.get(reverse('community:incidents-proches'), {'cells': cellules})
        self.assertEqual(reponse.status_code, 400)

    def test_pile_a_la_limite_de_cellules_accepte(self):
        from community.views import MAX_CELLULES
        import h3
        cellules = ','.join(h3.latlng_to_cell(0.0, float(i), 8) for i in range(MAX_CELLULES))
        reponse = self.client.get(reverse('community:incidents-proches'), {'cells': cellules})
        self.assertEqual(reponse.status_code, 200)

    def test_lat_sans_lon_rejete(self):
        import h3
        cellule_hex = h3.latlng_to_cell(DOUALA_LAT, DOUALA_LON, 8)
        reponse = self.client.get(reverse('community:incidents-proches'), {'cells': cellule_hex, 'lat': '4.05'})
        self.assertEqual(reponse.status_code, 400)

    def test_plafond_de_resultats_applique(self):
        from community.views import MAX_RESULTATS
        import h3
        utilisateur = creer_utilisateur()
        for _ in range(MAX_RESULTATS + 5):
            creer_incident(utilisateur)
        cellule_hex = h3.latlng_to_cell(DOUALA_LAT, DOUALA_LON, 8)
        reponse = self.client.get(reverse('community:incidents-proches'), {'cells': cellule_hex})
        self.assertEqual(len(reponse.json()), MAX_RESULTATS)

    def test_tri_par_gravite_sans_position_de_reference(self):
        utilisateur = creer_utilisateur()
        import h3
        faible = creer_incident(utilisateur, severite=1)
        forte = creer_incident(utilisateur, severite=5)
        cellule_hex = h3.latlng_to_cell(DOUALA_LAT, DOUALA_LON, 8)

        reponse = self.client.get(reverse('community:incidents-proches'), {'cells': cellule_hex})
        corps = reponse.json()
        self.assertEqual([corps[0]['id'], corps[1]['id']], [str(forte.id), str(faible.id)])

    def test_tri_par_proximite_quand_lat_lon_fournis(self):
        import h3
        utilisateur = creer_utilisateur()
        proche = creer_incident(utilisateur, lat=DOUALA_LAT, lon=DOUALA_LON)
        loin = creer_incident(utilisateur, lat=DOUALA_LAT + 0.02, lon=DOUALA_LON + 0.02)  # ~2-3km
        cellules = ','.join({
            h3.int_to_str(proche.cellule_h3_res8),
            h3.int_to_str(loin.cellule_h3_res8),
        })

        reponse = self.client.get(
            reverse('community:incidents-proches'),
            {'cells': cellules, 'lat': str(DOUALA_LAT), 'lon': str(DOUALA_LON)},
        )
        corps = reponse.json()
        self.assertEqual(corps[0]['id'], str(proche.id))

        # Inverse la position de reference -- l'ordre doit s'inverser aussi,
        # preuve que le tri suit reellement la distance et non un ordre fixe.
        reponse_inverse = self.client.get(
            reverse('community:incidents-proches'),
            {'cells': cellules, 'lat': str(DOUALA_LAT + 0.02), 'lon': str(DOUALA_LON + 0.02)},
        )
        corps_inverse = reponse_inverse.json()
        self.assertEqual(corps_inverse[0]['id'], str(loin.id))


class IncidentsProchesParRayonApiTests(TestCase):
    """GET /api/incidents/nearby/?lat=&lon=&radius_km= : mode alternatif a
    cells=, requete geographique directe (pas de cache par cellule)."""

    def setUp(self):
        cache.clear()
        self.utilisateur = creer_utilisateur()

    def test_radius_km_sans_lat_lon_rejete(self):
        reponse = self.client.get(reverse('community:incidents-proches'), {'radius_km': '5'})
        self.assertEqual(reponse.status_code, 400)

    def test_retourne_incident_dans_le_rayon(self):
        proche = creer_incident(self.utilisateur, lat=DOUALA_LAT + 0.01, lon=DOUALA_LON)  # ~1.1km
        reponse = self.client.get(
            reverse('community:incidents-proches'),
            {'lat': str(DOUALA_LAT), 'lon': str(DOUALA_LON), 'radius_km': '5'},
        )
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual([i['id'] for i in reponse.json()], [str(proche.id)])

    def test_exclut_incident_hors_du_rayon(self):
        creer_incident(self.utilisateur, lat=DOUALA_LAT + 0.2, lon=DOUALA_LON)  # ~22km
        reponse = self.client.get(
            reverse('community:incidents-proches'),
            {'lat': str(DOUALA_LAT), 'lon': str(DOUALA_LON), 'radius_km': '5'},
        )
        self.assertEqual(reponse.json(), [])

    def test_rayon_par_defaut_dix_km(self):
        from community.views import RAYON_KM_DEFAUT
        self.assertEqual(RAYON_KM_DEFAUT, 10)
        dans_les_dix_km = creer_incident(self.utilisateur, lat=DOUALA_LAT + 0.08, lon=DOUALA_LON)  # ~8.9km
        reponse = self.client.get(
            reverse('community:incidents-proches'), {'lat': str(DOUALA_LAT), 'lon': str(DOUALA_LON)},
        )
        self.assertEqual([i['id'] for i in reponse.json()], [str(dans_les_dix_km.id)])

    def test_rayon_hors_bornes_rejete(self):
        from community.views import RAYON_KM_MAX
        reponse = self.client.get(
            reverse('community:incidents-proches'),
            {'lat': str(DOUALA_LAT), 'lon': str(DOUALA_LON), 'radius_km': str(RAYON_KM_MAX + 1)},
        )
        self.assertEqual(reponse.status_code, 400)

    def test_rayon_nul_rejete(self):
        reponse = self.client.get(
            reverse('community:incidents-proches'),
            {'lat': str(DOUALA_LAT), 'lon': str(DOUALA_LON), 'radius_km': '0'},
        )
        self.assertEqual(reponse.status_code, 400)

    def test_rayon_non_numerique_rejete(self):
        reponse = self.client.get(
            reverse('community:incidents-proches'),
            {'lat': str(DOUALA_LAT), 'lon': str(DOUALA_LON), 'radius_km': 'loin'},
        )
        self.assertEqual(reponse.status_code, 400)

    def test_tri_par_proximite(self):
        proche = creer_incident(self.utilisateur, lat=DOUALA_LAT + 0.01, lon=DOUALA_LON)
        loin = creer_incident(self.utilisateur, lat=DOUALA_LAT + 0.05, lon=DOUALA_LON)
        reponse = self.client.get(
            reverse('community:incidents-proches'),
            {'lat': str(DOUALA_LAT), 'lon': str(DOUALA_LON), 'radius_km': '10'},
        )
        corps = reponse.json()
        self.assertEqual([corps[0]['id'], corps[1]['id']], [str(proche.id), str(loin.id)])

    def test_plafond_de_resultats_applique(self):
        from community.views import MAX_RESULTATS
        for _ in range(MAX_RESULTATS + 5):
            creer_incident(self.utilisateur, lat=DOUALA_LAT, lon=DOUALA_LON)
        reponse = self.client.get(
            reverse('community:incidents-proches'),
            {'lat': str(DOUALA_LAT), 'lon': str(DOUALA_LON), 'radius_km': '10'},
        )
        self.assertEqual(len(reponse.json()), MAX_RESULTATS)

    def test_cells_prioritaire_sur_radius_km(self):
        import h3
        dans_la_cellule = creer_incident(self.utilisateur, lat=DOUALA_LAT, lon=DOUALA_LON)
        creer_incident(self.utilisateur, lat=DOUALA_LAT + 0.02, lon=DOUALA_LON + 0.02)  # hors cellule, dans le rayon
        cellule_hex = h3.int_to_str(dans_la_cellule.cellule_h3_res8)

        reponse = self.client.get(
            reverse('community:incidents-proches'), {'cells': cellule_hex, 'radius_km': '50'},
        )
        # cells prioritaire : radius_km est ignore silencieusement.
        self.assertEqual([i['id'] for i in reponse.json()], [str(dans_la_cellule.id)])


class IncidentsSurTrajetApiTests(TestCase):
    """POST /api/incidents/along-route/ : geometrie en corps de requete,
    couloir autour du trace -- cf. discussion frontend (URL H3 ingerable
    sur un trajet long)."""

    def setUp(self):
        # Segment rectiligne d'environ 800m -- assez pour placer un incident
        # "sur" le trace et un autre nettement hors du couloir par defaut (300m).
        self.geometrie = encoder_polyline6([
            (DOUALA_LON, DOUALA_LAT),
            (DOUALA_LON - 0.007, DOUALA_LAT),
        ])
        self.utilisateur = creer_utilisateur()

    def test_geometrie_tronquee_fait_planter_le_decodeur_rejetee(self):
        # decoder_polyline6 est un decodeur permissif (pas un validateur) :
        # la plupart des chaines "au hasard" produisent quand meme des points
        # (faux mais bien formes). Ce qui declenche vraiment une exception,
        # c'est un varint coupe en plein milieu -- '~' (0x7e-0x3f=0x3f >= 0x20)
        # pose le bit de continuation puis la chaine s'arrete, IndexError sur
        # le caractere suivant qui n'existe pas.
        reponse = self.client.post(
            reverse('community:incidents-sur-trajet'), {'geometry': '~'},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 400)

    def test_geometrie_un_seul_point_rejetee(self):
        un_point = encoder_polyline6([(DOUALA_LON, DOUALA_LAT)])
        reponse = self.client.post(
            reverse('community:incidents-sur-trajet'), {'geometry': un_point},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 400)

    def test_incident_sur_le_trace_est_retourne(self):
        incident = creer_incident(self.utilisateur, lat=DOUALA_LAT, lon=DOUALA_LON)
        reponse = self.client.post(
            reverse('community:incidents-sur-trajet'), {'geometry': self.geometrie},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 200)
        corps = reponse.json()
        self.assertEqual([i['id'] for i in corps], [str(incident.id)])

    def test_incident_loin_du_trace_est_exclu(self):
        creer_incident(self.utilisateur, lat=DOUALA_LAT + 0.05, lon=DOUALA_LON + 0.05)  # ~7km, hors couloir
        reponse = self.client.post(
            reverse('community:incidents-sur-trajet'), {'geometry': self.geometrie},
            content_type='application/json',
        )
        self.assertEqual(reponse.json(), [])

    def test_buffer_personnalise_elargit_le_couloir(self):
        # A ~600m perpendiculaire du segment -- hors du buffer par defaut (300m),
        # dans un buffer elargi (800m).
        loin_lateral = creer_incident(self.utilisateur, lat=DOUALA_LAT + 0.0055, lon=DOUALA_LON - 0.0035)

        etroit = self.client.post(
            reverse('community:incidents-sur-trajet'), {'geometry': self.geometrie},
            content_type='application/json',
        )
        self.assertEqual(etroit.json(), [])

        large = self.client.post(
            reverse('community:incidents-sur-trajet'),
            {'geometry': self.geometrie, 'buffer_m': 800},
            content_type='application/json',
        )
        self.assertEqual([i['id'] for i in large.json()], [str(loin_lateral.id)])

    def test_buffer_hors_bornes_rejete(self):
        reponse = self.client.post(
            reverse('community:incidents-sur-trajet'),
            {'geometry': self.geometrie, 'buffer_m': 5000},
            content_type='application/json',
        )
        self.assertEqual(reponse.status_code, 400)

    def test_tri_par_gravite(self):
        faible = creer_incident(self.utilisateur, lat=DOUALA_LAT, lon=DOUALA_LON, severite=1)
        forte = creer_incident(self.utilisateur, lat=DOUALA_LAT, lon=DOUALA_LON - 0.005, severite=5)
        reponse = self.client.post(
            reverse('community:incidents-sur-trajet'), {'geometry': self.geometrie},
            content_type='application/json',
        )
        corps = reponse.json()
        self.assertEqual([corps[0]['id'], corps[1]['id']], [str(forte.id), str(faible.id)])


class IncidentsParVilleApiTests(TestCase):
    """GET /api/incidents/city/?name=<ville> : filtre sur Incident.ville_normalisee
    (denormalise a la creation depuis Nominatim, cf. ServiceIncident._geocoder_inverse)."""

    def setUp(self):
        self.utilisateur = creer_utilisateur()

    def _incident_a(self, ville, **extra):
        return creer_incident(self.utilisateur, ville=ville, ville_normalisee=normaliser(ville), **extra)

    def test_sans_name_rejete(self):
        reponse = self.client.get(reverse('community:incidents-par-ville'))
        self.assertEqual(reponse.status_code, 400)

    def test_retourne_les_incidents_de_la_ville(self):
        yaounde = self._incident_a('Yaoundé')
        self._incident_a('Douala')
        reponse = self.client.get(reverse('community:incidents-par-ville'), {'name': 'Yaoundé'})
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual([i['id'] for i in reponse.json()], [str(yaounde.id)])

    def test_insensible_a_la_casse_et_aux_accents(self):
        yaounde = self._incident_a('Yaoundé')
        reponse = self.client.get(reverse('community:incidents-par-ville'), {'name': 'yaounde'})
        self.assertEqual([i['id'] for i in reponse.json()], [str(yaounde.id)])

    def test_ville_avec_granularite_administrative_matchee_par_sous_chaine(self):
        # Reproduit un cas reel observe en verification live : Nominatim
        # renvoie parfois "Douala I" (arrondissement) au lieu du nom usuel --
        # cf. NormalisationVilleNominatimTests dans places/tests.py. Le
        # filtre par sous-chaine (pas exact) absorbe cette variation.
        incident = self._incident_a('Douala I')
        reponse = self.client.get(reverse('community:incidents-par-ville'), {'name': 'Douala'})
        self.assertEqual([i['id'] for i in reponse.json()], [str(incident.id)])

    def test_incident_sans_ville_absent_de_toute_recherche(self):
        creer_incident(self.utilisateur)  # ville='' (Nominatim indisponible au signalement)
        reponse = self.client.get(reverse('community:incidents-par-ville'), {'name': 'Yaounde'})
        self.assertEqual(reponse.json(), [])

    def test_incident_expire_exclu(self):
        self._incident_a('Yaoundé', statut=StatutIncident.EXPIRE, expire_le=timezone.now() - timezone.timedelta(minutes=1))
        reponse = self.client.get(reverse('community:incidents-par-ville'), {'name': 'Yaoundé'})
        self.assertEqual(reponse.json(), [])

    def test_tri_par_gravite(self):
        faible = self._incident_a('Yaoundé', severite=1)
        forte = self._incident_a('Yaoundé', lon=DOUALA_LON - 0.01, severite=5)
        reponse = self.client.get(reverse('community:incidents-par-ville'), {'name': 'Yaoundé'})
        corps = reponse.json()
        self.assertEqual([corps[0]['id'], corps[1]['id']], [str(forte.id), str(faible.id)])


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

    def test_lecture_pendant_verrou_ecriture_ne_recache_pas(self):
        # Simule une lecture concurrente amorcee juste avant une ecriture : meme
        # en cache miss (ex. apres invalider_cache_cellule), elle ne doit pas
        # re-peupler durablement le cache pendant la fenetre de verrou -- sinon
        # une valeur potentiellement perimee (snapshot pre-ecriture) resterait
        # servie jusqu'a l'expiration de son propre TTL au lieu du prochain
        # miss legitime.
        invalider_cache_cellule(self.incident.cellule_h3_res8)
        self.assertTrue(ecriture_recente(self.incident.cellule_h3_res8))

        reponse = self.client.get(reverse('community:incidents-proches'), {'cells': self.cellule_hex})
        self.assertEqual(reponse.status_code, 200)
        self.assertIsNone(cache.get(cle_cache_cellule(self.incident.cellule_h3_res8)))


class IncidentDetailApiTests(TestCase):
    def setUp(self):
        self.auteur = creer_utilisateur('auteur@easyway.local')
        self.incident = creer_incident(self.auteur, confirmations=3, infirmations=1)

    def test_detail_inclut_impact_estime(self):
        reponse = self.client.get(reverse('community:incident-detail', kwargs={'id': self.incident.id}))
        self.assertEqual(reponse.json()['estimated_impact'], 2)

    def test_incident_expire_introuvable(self):
        # Meme regle que /nearby/, /along-route/, /city/ : un incident dont la
        # periode de validite est passee ne doit reapparaitre nulle part,
        # meme via son id direct -- cf. discussion frontend.
        self.incident.expire_le = timezone.now() - timezone.timedelta(minutes=1)
        self.incident.save(update_fields=['expire_le'])
        reponse = self.client.get(reverse('community:incident-detail', kwargs={'id': self.incident.id}))
        self.assertEqual(reponse.status_code, 404)

    def test_incident_retire_introuvable(self):
        self.incident.statut = StatutIncident.RETIRE
        self.incident.save(update_fields=['statut'])
        reponse = self.client.get(reverse('community:incident-detail', kwargs={'id': self.incident.id}))
        self.assertEqual(reponse.status_code, 404)

    def test_incident_en_attente_non_expire_visible(self):
        self.incident.statut = StatutIncident.EN_ATTENTE
        self.incident.save(update_fields=['statut'])
        reponse = self.client.get(reverse('community:incident-detail', kwargs={'id': self.incident.id}))
        self.assertEqual(reponse.status_code, 200)

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

    def test_trois_confirmations_promeuvent_en_attente_vers_actif(self):
        # SEUIL_CONFIANCE_VALIDATION = 3 : il faut 3 votants de reputation
        # neutre (poids 1.0 chacun, nouveau compte) pour atteindre le seuil.
        incident = creer_incident(self.auteur, statut=StatutIncident.EN_ATTENTE)
        self._voter(incident, 'confirm')
        incident.refresh_from_db()
        self.assertEqual(incident.statut, StatutIncident.EN_ATTENTE)

        votant2 = creer_utilisateur('votant2@easyway.local')
        self._voter(incident, 'confirm', jetons=connecter(self.client, votant2.email))
        incident.refresh_from_db()
        self.assertEqual(incident.statut, StatutIncident.EN_ATTENTE)

        votant3 = creer_utilisateur('votant3@easyway.local')
        self._voter(incident, 'confirm', jetons=connecter(self.client, votant3.email))
        incident.refresh_from_db()
        self.assertEqual(incident.statut, StatutIncident.ACTIF)

    def test_seuil_validation_reduit_pour_auteur_repute(self):
        # Auteur deja repute (>= SEUIL_REPUTATION_PALIER_REDUCTION) : 2
        # confirmations suffisent au lieu de 3 pour ses signalements suivants.
        auteur_repute = creer_utilisateur('repute@easyway.local', score_reputation=Decimal('3'))
        incident = creer_incident(auteur_repute, statut=StatutIncident.EN_ATTENTE)
        self._voter(incident, 'confirm')
        incident.refresh_from_db()
        self.assertEqual(incident.statut, StatutIncident.EN_ATTENTE)

        votant2 = creer_utilisateur('votant2@easyway.local')
        self._voter(incident, 'confirm', jetons=connecter(self.client, votant2.email))
        incident.refresh_from_db()
        self.assertEqual(incident.statut, StatutIncident.ACTIF)

    def test_validation_credite_lauteur_de_points_de_reputation(self):
        incident = creer_incident(self.auteur, statut=StatutIncident.EN_ATTENTE)
        self._voter(incident, 'confirm')
        for i in (2, 3):
            votant = creer_utilisateur(f'votant{i}@easyway.local')
            self._voter(incident, 'confirm', jetons=connecter(self.client, votant.email))
        self.auteur.refresh_from_db()
        self.assertEqual(self.auteur.score_reputation, Decimal('0.5'))

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
