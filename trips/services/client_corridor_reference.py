"""ClientCorridorReference : implemente ClientRoutage en cousant un
CorridorReference (trace de confiance importe, cf. trips/models.py) avec de
vrais connecteurs Valhalla a chaque bout -- depart reel -> corridor ->
arrivee reelle. Cf. ServiceItineraire.calculer, qui l'utilise a la place de
ClientValhalla quand corridors.trouver_corridor() trouve une correspondance.

Tout est assemble en un seul leg (pas trois) : encoder_polyline6() demarre
son delta a (0,0) a chaque appel independant (cf. polyline.py) -- concatener
directement les shapes de 3 legs obtenus par 3 appels Valhalla/calculs
distincts produirait une geometrie corrompue (chaque shape suppose a tort
continuer le delta du precedent). On decode donc chaque morceau en points
bruts, on les concatene, et on encode une seule fois. Un seul leg evite
aussi un effet de bord : _normaliser_trip marque la derniere manoeuvre de
tout leg non-final comme arrivee_etape_index (arret intermediaire, cf.
waypoints) -- trois legs internes auraient fabrique de faux arrets la ou
l'utilisateur n'en a demande aucun.

Limitation acceptee en v1 : le turn-by-turn reel capture a l'import
(corridor.manoeuvres_reference) n'est reutilise que pour un usage complet
DANS LE SENS aller (ancrage_depart -> ancrage_arrivee) -- inverser une liste
de manoeuvres ne produit pas des instructions valides pour le sens retour
("tourner a gauche" ne devient pas "tourner a droite" en inversant l'ordre).
Le sens retour et tout usage partiel du corridor recoivent une seule
manoeuvre synthetique (distance/duree correctes, road_class correct via
libelles_voies, pas de narration tour par tour)."""

import bisect

from django.contrib.gis.geos import Point

from trips.polyline import decoder_polyline6, encoder_polyline6

from .client_routage import ClientRoutage
from .client_valhalla import ClientValhalla
from .geo import distance_haversine_m

# cf. docstring de module : un trajet dont les deux extremites tombent a
# moins de cette fraction des bornes du corridor (0=ancrage_depart,
# 1=ancrage_arrivee) est traite comme un usage complet dans le sens aller.
# 2% d'un corridor de 250km ~= 5km, coherent avec le rayon d'ancrage par
# defaut (25km, cf. CorridorReference.rayon_ancrage_m) -- pas mesure sur
# donnees reelles au-dela de cette coherence d'ordre de grandeur.
TOLERANCE_CORRIDOR_COMPLET = 0.02

# En-deca de cette distance, un connecteur Valhalla degenere (quasi meme
# point) -- omis plutot que de risquer un appel Valhalla sur une requete a
# distance ~0.
DISTANCE_MIN_CONNECTEUR_M = 50


def _concatener_sans_doublon(*listes_points):
    """Concatene plusieurs listes de points (lon, lat) consecutives en
    evitant de dupliquer le point de jonction quand il est strictement
    identique (les connecteurs sont calcules pour aboutir exactement au
    point d'entree/sortie du corridor)."""
    resultat = []
    for points in listes_points:
        if resultat and points and resultat[-1] == points[0]:
            resultat.extend(points[1:])
        else:
            resultat.extend(points)
    return resultat


class ClientCorridorReference(ClientRoutage):
    def __init__(self, corridor, client_valhalla=None):
        self.corridor = corridor
        self.client_valhalla = client_valhalla or ClientValhalla()

    def calculer_itineraires(self, depart, arrivee, options, cap_origine=None, alternatives=True, etapes=None):
        try:
            return self._calculer(depart, arrivee, options, cap_origine)
        except Exception:
            # Un corridor de reference ne doit jamais rendre le routage moins
            # fiable qu'aujourd'hui -- toute erreur (connecteur Valhalla en
            # panne, cas GEOS degenere...) retombe sur un calcul Valhalla
            # normal de bout en bout, comme si aucun corridor n'avait matche.
            return self.client_valhalla.calculer_itineraires(
                depart, arrivee, options, cap_origine=cap_origine, alternatives=alternatives, etapes=etapes
            )

    def replier(self, depart, arrivee, etapes=None):
        return self.client_valhalla.replier(depart, arrivee, etapes)

    def _calculer(self, depart, arrivee, options, cap_origine):
        ligne = self.corridor.geometrie
        f_depart = ligne.project_normalized(Point(depart[1], depart[0], srid=4326))
        f_arrivee = ligne.project_normalized(Point(arrivee[1], arrivee[0], srid=4326))

        if f_depart <= f_arrivee:
            f_entree, f_sortie, inverser = f_depart, f_arrivee, False
        else:
            f_entree, f_sortie, inverser = f_arrivee, f_depart, True

        point_entree = ligne.interpolate_normalized(f_entree)
        point_sortie = ligne.interpolate_normalized(f_sortie)

        coords_corridor, manoeuvres_corridor, distance_corridor_m, duree_corridor_s = (
            self._construire_segment_corridor(point_entree, point_sortie, f_entree, f_sortie, inverser)
        )

        # Connecteurs reels : du point demande jusqu'au corridor, et du
        # corridor jusqu'au point demande -- selon le sens de parcours reel
        # (inverser=True : on parcourt le corridor de sortie vers entree,
        # donc le depart reel se connecte a point_sortie).
        point_connecteur_depart = point_sortie if inverser else point_entree
        point_connecteur_arrivee = point_entree if inverser else point_sortie

        coords_entree, manoeuvres_entree, km_entree, s_entree = self._connecteur(
            depart, (point_connecteur_depart.y, point_connecteur_depart.x), options, cap_origine
        )
        coords_sortie, manoeuvres_sortie, km_sortie, s_sortie = self._connecteur(
            (point_connecteur_arrivee.y, point_connecteur_arrivee.x), arrivee, options
        )

        tous_points = _concatener_sans_doublon(coords_entree, coords_corridor, coords_sortie)
        toutes_manoeuvres = manoeuvres_entree + manoeuvres_corridor + manoeuvres_sortie

        longueur_km = km_entree + distance_corridor_m / 1000 + km_sortie
        duree_totale_s = s_entree + duree_corridor_s + s_sortie

        return [{
            'summary': {'length': round(longueur_km, 2), 'time': round(duree_totale_s)},
            # Un seul leg (cf. docstring de module) : concatener_direct des
            # legs Valhalla d'un meme appel est valide (Valhalla chaine
            # lui-meme ses deltas entre legs), mais ici les trois morceaux
            # viennent d'appels/calculs independants.
            'legs': [{'shape': encoder_polyline6(tous_points), 'maneuvers': toutes_manoeuvres}],
            'degrade': False,
        }]

    def _connecteur(self, point_a, point_b, options, cap_origine=None):
        if distance_haversine_m(point_a, point_b) < DISTANCE_MIN_CONNECTEUR_M:
            return [], [], 0, 0
        trips = self.client_valhalla.calculer_itineraires(
            point_a, point_b, options, cap_origine=cap_origine, alternatives=False
        )
        trip = trips[0]
        coords = decoder_polyline6(''.join(leg['shape'] for leg in trip['legs']))
        manoeuvres = [m for leg in trip['legs'] for m in leg['maneuvers']]
        return coords, manoeuvres, trip['summary']['length'], trip['summary']['time']

    def _construire_segment_corridor(self, point_entree, point_sortie, f_entree, f_sortie, inverser):
        corridor = self.corridor
        fraction_utilisee = f_sortie - f_entree
        est_aller_complet = (
            not inverser
            and f_entree <= TOLERANCE_CORRIDOR_COMPLET
            and f_sortie >= 1 - TOLERANCE_CORRIDOR_COMPLET
        )

        if est_aller_complet:
            coords = list(corridor.geometrie.coords)
            manoeuvres = self._manoeuvres_mises_a_echelle()
            distance_m, duree_s = corridor.distance_m, corridor.duree_s
        else:
            # Usage partiel (ou sens retour) : sous-portion des sommets
            # originaux (garde la resolution de la source) + une seule
            # manoeuvre synthetique -- cf. limitation en tete de module.
            sommets = list(corridor.geometrie.coords)
            fractions = corridor.fractions_sommets
            indice_debut = bisect.bisect_left(fractions, f_entree)
            indice_fin = bisect.bisect_right(fractions, f_sortie)
            coords = [point_entree.coords] + sommets[indice_debut:indice_fin] + [point_sortie.coords]

            distance_m = corridor.distance_m * fraction_utilisee
            duree_s = corridor.duree_s * fraction_utilisee
            manoeuvres = [{
                'type': 0,
                'instruction': '',
                'length': distance_m / 1000,
                'time': duree_s,
                'street_names': list(corridor.libelles_voies),
            }]

        if inverser:
            coords = list(reversed(coords))

        return coords, manoeuvres, distance_m, duree_s

    def _manoeuvres_mises_a_echelle(self):
        """Les manoeuvres capturees a l'import sommaient a la duree Valhalla
        ORIGINALE (plus rapide, sous-estimee) -- remises a l'echelle pour que
        leur somme corresponde a corridor.duree_s (la duree de confiance),
        plutot que de laisser les ETA par manoeuvre incoherents avec la
        duree globale corrigee."""
        manoeuvres = self.corridor.manoeuvres_reference
        if not manoeuvres:
            return [{
                'type': 0, 'instruction': '', 'length': self.corridor.distance_m / 1000,
                'time': self.corridor.duree_s, 'street_names': list(self.corridor.libelles_voies),
            }]

        duree_capturee = sum(m.get('time', 0) for m in manoeuvres)
        facteur = (self.corridor.duree_s / duree_capturee) if duree_capturee else 1
        return [{**m, 'time': m.get('time', 0) * facteur} for m in manoeuvres]
