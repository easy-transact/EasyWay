"""Raccordement au corridor de reference (ClientCorridorReference._calculer) :
pas de boucle au depart, pas de "Vous etes arrive" en plein trajet."""

from types import SimpleNamespace
from unittest.mock import Mock

from django.contrib.gis.geos import LineString, Point
from django.test import SimpleTestCase

from .polyline import decoder_polyline6, encoder_polyline6
from .services.client_corridor_reference import ClientCorridorReference

# Corridor plein nord, lon 9.70, de lat 4.00 a 4.30 (~33 km).
SOMMETS = [(9.70, 4.00 + i * 0.01) for i in range(31)]


def corridor(manoeuvres=None):
    ligne = LineString(SOMMETS, srid=4326)
    return SimpleNamespace(
        geometrie=ligne,
        fractions_sommets=[ligne.project_normalized(Point(c, srid=4326)) for c in SOMMETS],
        distance_m=33_000, duree_s=3300, libelles_voies=['N3'],
        manoeuvres_reference=manoeuvres or [],
    )


def trip(coords, manoeuvres, secondes):
    return {'summary': {'length': 1.0, 'time': secondes},
            'legs': [{'shape': encoder_polyline6(coords), 'maneuvers': manoeuvres}], 'degrade': False}


DEPART = (4.05, 9.71)     # a l'est du corridor
ARRIVEE = (4.25, 9.69)    # a l'ouest

# Connecteur d'entree : rejoint le corridor a lat 4.06 (un "rond-point"),
# puis le DESCEND jusqu'au point projete (lat 4.05) -- le cas de Deido.
ENTREE = [(9.71, 4.05), (9.705, 4.06), (9.70, 4.06), (9.70, 4.058), (9.70, 4.055), (9.70, 4.05)]
MANOEUVRES_ENTREE = [
    {'type': 1, 'instruction': 'Partez vers l ouest', 'begin_shape_index': 0, 'end_shape_index': 2, 'length': 1.2, 'time': 120},
    {'type': 10, 'instruction': 'Descendez vers le sud', 'begin_shape_index': 2, 'end_shape_index': 5, 'length': 1.1, 'time': 100},
    {'type': 4, 'instruction': 'Vous etes arrive', 'begin_shape_index': 5, 'end_shape_index': 5, 'length': 0, 'time': 0},
]
SORTIE = [(9.70, 4.25), (9.69, 4.25)]
MANOEUVRES_SORTIE = [
    {'type': 1, 'instruction': 'Partez vers l ouest', 'begin_shape_index': 0, 'end_shape_index': 1, 'length': 1.1, 'time': 90},
    {'type': 4, 'instruction': 'Vous etes arrive', 'begin_shape_index': 1, 'end_shape_index': 1, 'length': 0, 'time': 0},
]


def valhalla():
    client = Mock()

    def calculer(point_a, point_b, options, cap_origine=None, alternatives=True, etapes=None):
        if point_a == DEPART:
            return [trip(ENTREE, MANOEUVRES_ENTREE, 220)]
        return [trip(SORTIE, MANOEUVRES_SORTIE, 90)]

    client.calculer_itineraires.side_effect = calculer
    return client


class RaccordementCorridorTests(SimpleTestCase):
    def calculer(self, manoeuvres=None):
        client = ClientCorridorReference(corridor(manoeuvres), client_valhalla=valhalla())
        return client.calculer_itineraires(DEPART, ARRIVEE, {'costing': 'auto'}, alternatives=False)[0]

    def test_pas_de_boucle_au_depart(self):
        resultat = self.calculer()
        latitudes = [lat for lon, lat in decoder_polyline6(resultat['legs'][0]['shape'])]
        arrivee_corridor = latitudes.index(4.06)
        apres = latitudes[arrivee_corridor:]
        # Une fois sur le corridor, on ne redescend plus vers le sud.
        self.assertEqual(apres, sorted(apres))
        instructions = [m['instruction'] for m in resultat['legs'][0]['maneuvers']]
        self.assertNotIn('Descendez vers le sud', instructions)

    def test_une_seule_arrivee_a_la_fin(self):
        manoeuvres = self.calculer()['legs'][0]['maneuvers']
        arrivees = [i for i, m in enumerate(manoeuvres) if m['type'] in (4, 5, 6)]
        self.assertEqual(arrivees, [len(manoeuvres) - 1])

    def test_manoeuvres_reelles_decoupees_a_la_portion(self):
        reference = [
            {'type': 1, 'instruction': 'Depart ancrage', 'length': 0, 'time': 0},
            {'type': 8, 'instruction': 'Sud de la ville', 'length': 10, 'time': 1000},
            {'type': 8, 'instruction': 'Nord de la ville', 'length': 23, 'time': 2300},
            {'type': 4, 'instruction': 'Arrivee ancrage', 'length': 0, 'time': 0},
        ]
        manoeuvres = self.calculer(reference)['legs'][0]['maneuvers']
        instructions = [m['instruction'] for m in manoeuvres]
        # Portion lat 4.06 -> 4.25 : le depart de l'ancrage (lat 4.00) et
        # l'arrivee du corridor (lat 4.30) ne sont pas parcourus.
        self.assertNotIn('Depart ancrage', instructions)
        self.assertNotIn('Arrivee ancrage', instructions)
        self.assertIn('Sud de la ville', instructions)
        sud = manoeuvres[instructions.index('Sud de la ville')]
        self.assertAlmostEqual(sud['length'], 10 - 33 * 0.2, delta=0.3)
