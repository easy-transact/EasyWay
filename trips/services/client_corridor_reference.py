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
(corridor.manoeuvres_reference) n'est reutilise que DANS LE SENS aller
(ancrage_depart -> ancrage_arrivee), decoupe a la portion parcourue --
inverser une liste de manoeuvres ne produit pas des instructions valides
pour le sens retour ("tourner a gauche" ne devient pas "tourner a droite"
en inversant l'ordre). Le sens retour recoit une seule manoeuvre synthetique
(distance/duree correctes, road_class correct via libelles_voies, pas de
narration tour par tour)."""

import bisect

from django.contrib.gis.geos import Point

from trips.polyline import decoder_polyline6, encoder_polyline6

from .client_routage import ClientRoutage
from .client_valhalla import ClientValhalla, selectionner_variantes
from .geo import distance_haversine_m

# En-deca de cette distance, un connecteur Valhalla degenere (quasi meme
# point) -- omis plutot que de risquer un appel Valhalla sur une requete a
# distance ~0.
DISTANCE_MIN_CONNECTEUR_M = 50

# Raccordement connecteur/corridor (cf. ClientCorridorReference._contact_entree) :
# un point du connecteur a moins de DISTANCE_CONTACT_M de la ligne est "sur"
# le corridor ; on ne recoupe que si le contact est a plus de
# ECART_MIN_RACCORDEMENT_M du point projete (sinon simple approche finale).
DISTANCE_CONTACT_M = 25
ECART_MIN_RACCORDEMENT_M = 100
METRES_PAR_DEGRE = 111_320

# Types de manoeuvre Valhalla : depart (1-3) et arrivee (4-6).
TYPES_ARRIVEE = {4, 5, 6}


def _sans_arrivees_intermediaires(manoeuvres):
    """Le connecteur d'entree (et le corridor) se terminent chacun par une
    manoeuvre "Vous etes arrive" : seule la derniere du trajet est gardee."""
    return [
        m for i, m in enumerate(manoeuvres)
        if m.get('type') not in TYPES_ARRIVEE or i == len(manoeuvres) - 1
    ]


def _longueur_m(coords):
    return sum(
        distance_haversine_m((a[1], a[0]), (b[1], b[0])) for a, b in zip(coords, coords[1:])
    )


def _tronquer(coords, manoeuvres, km, secondes, debut=0, fin=None):
    """Garde coords[debut:fin + 1] d'un connecteur. Distance/duree au prorata
    de la longueur gardee ; manoeuvres filtrees par begin/end_shape_index
    (toutes gardees si Valhalla ne les fournit pas)."""
    dernier = len(coords) - 1
    fin = dernier if fin is None else fin
    gardees = coords[debut:fin + 1]
    total = _longueur_m(coords)
    ratio = _longueur_m(gardees) / total if total else 0
    if all('begin_shape_index' in m for m in manoeuvres):
        # Une manoeuvre qui commence au point de coupe d'entree part sur le
        # corridor : ecartee. En fin de connecteur, l'arrivee est gardee.
        manoeuvres = [
            m for m in manoeuvres
            if (m['begin_shape_index'] < fin or fin == dernier)
            and m.get('end_shape_index', m['begin_shape_index']) >= debut
        ]
    return gardees, manoeuvres, km * ratio, secondes * ratio


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


def _mettre_a_echelle(trip, facteur):
    """Copie du trip avec ses durees (resume et manoeuvres) multipliees par facteur."""
    return {
        **trip,
        'summary': {**trip['summary'], 'time': round(trip['summary']['time'] * facteur)},
        'legs': [
            {**leg, 'maneuvers': [{**m, 'time': m.get('time', 0) * facteur} for m in leg['maneuvers']]}
            for leg in trip['legs']
        ],
    }


class ClientCorridorReference(ClientRoutage):
    def __init__(self, corridor, client_valhalla=None):
        self.corridor = corridor
        self.client_valhalla = client_valhalla or ClientValhalla()

    def calculer_itineraires(self, depart, arrivee, options, cap_origine=None, alternatives=True, etapes=None):
        try:
            trips = self._calculer(depart, arrivee, options, cap_origine)
        except Exception:
            # Un corridor de reference ne doit jamais rendre le routage moins
            # fiable qu'aujourd'hui -- toute erreur (connecteur Valhalla en
            # panne, cas GEOS degenere...) retombe sur un calcul Valhalla
            # normal de bout en bout, comme si aucun corridor n'avait matche.
            return self.client_valhalla.calculer_itineraires(
                depart, arrivee, options, cap_origine=cap_origine, alternatives=alternatives, etapes=etapes
            )
        if alternatives:
            trips = selectionner_variantes(
                trips, self._variantes_valhalla(trips[0], depart, arrivee, options, cap_origine)
            )
        return trips

    def _variantes_valhalla(self, trip_corridor, depart, arrivee, options, cap_origine=None):
        """Le corridor reste la route recommandee ; les autres options
        viennent de Valhalla (celle qui suit le corridor est ecartee par
        selectionner_variantes, trace quasi identique). Les durees Valhalla
        sont remises a l'echelle du corridor (duree du trajet corridor /
        duree Valhalla de la meilleure route, qui le suit) : sans ca, une
        variante Valhalla paraitrait plus rapide que le corridor alors que
        Valhalla sous-estime simplement toutes les durees de la region."""
        trips = self.client_valhalla.calculer_itineraires(depart, arrivee, options, cap_origine=cap_origine)
        if not trips or trips[0].get('degrade') or not trips[0]['summary']['time']:
            return []
        facteur = trip_corridor['summary']['time'] / trips[0]['summary']['time']
        return [_mettre_a_echelle(trip, facteur) for trip in trips]

    def replier(self, depart, arrivee, etapes=None):
        return self.client_valhalla.replier(depart, arrivee, etapes)

    def _calculer(self, depart, arrivee, options, cap_origine):
        ligne = self.corridor.geometrie
        # Fractions (0-1) le long du corridor, dans l'ordre de parcours :
        # f_entree cote depart, f_sortie cote arrivee ; sens -1 = corridor
        # parcouru a l'envers (Bafoussam -> Douala pour un corridor importe
        # Douala -> Bafoussam).
        f_entree = ligne.project_normalized(Point(depart[1], depart[0], srid=4326))
        f_sortie = ligne.project_normalized(Point(arrivee[1], arrivee[0], srid=4326))
        sens = 1 if f_entree <= f_sortie else -1

        # Connecteurs reels : du point demande jusqu'au point du corridor le
        # plus proche, et du corridor jusqu'au point demande.
        point = ligne.interpolate_normalized(f_entree)
        coords_entree, manoeuvres_entree, km_entree, s_entree = self._connecteur(
            depart, (point.y, point.x), options, cap_origine
        )
        point = ligne.interpolate_normalized(f_sortie)
        coords_sortie, manoeuvres_sortie, km_sortie, s_sortie = self._connecteur(
            (point.y, point.x), arrivee, options
        )

        # Le point le plus proche n'est pas forcement celui ou la route rejoint
        # le corridor : depuis Deido, le connecteur passait le rond-point
        # (deja sur le corridor) puis descendait 1 km vers Bonanjo jusqu'au
        # point projete, et le corridor le faisait remonter -- boucle visible
        # sur la carte. On raccorde donc au premier contact reel.
        coupe, f_contact = self._contact_entree(coords_entree, f_entree, sens)
        if coupe is not None:
            coords_entree, manoeuvres_entree, km_entree, s_entree = _tronquer(
                coords_entree, manoeuvres_entree, km_entree, s_entree, fin=coupe
            )
            f_entree = f_contact
        coupe, f_contact = self._contact_sortie(coords_sortie, f_sortie, sens)
        if coupe is not None:
            coords_sortie, manoeuvres_sortie, km_sortie, s_sortie = _tronquer(
                coords_sortie, manoeuvres_sortie, km_sortie, s_sortie, debut=coupe
            )
            f_sortie = f_contact
        if sens * (f_sortie - f_entree) <= 0:
            # Plus rien a parcourir sur le corridor : calcul Valhalla normal
            # (cf. calculer_itineraires, qui rattrape l'exception).
            raise ValueError('Portion de corridor vide apres raccordement.')

        coords_corridor, manoeuvres_corridor, distance_corridor_m, duree_corridor_s = (
            self._construire_segment_corridor(f_entree, f_sortie, sens)
        )

        tous_points = _concatener_sans_doublon(coords_entree, coords_corridor, coords_sortie)
        toutes_manoeuvres = _sans_arrivees_intermediaires(manoeuvres_entree + manoeuvres_corridor + manoeuvres_sortie)

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

    def _contact_entree(self, coords, f_entree, sens):
        """Premier point du connecteur d'entree pose sur le corridor. S'il est
        EN AVAL de f_entree (le connecteur rejoint le corridor puis le
        remonte a contresens jusqu'au point projete), retourne (indice,
        fraction) ou couper ; sinon (None, None)."""
        for i in range(len(coords) - 1):
            f = self._fraction_si_sur_corridor(coords[i], coords[i + 1])
            if f is not None:
                return (i, f) if sens * (f - f_entree) > self._tolerance_fraction() else (None, None)
        return None, None

    def _contact_sortie(self, coords, f_sortie, sens):
        """Symetrique de _contact_entree : dernier point du connecteur de
        sortie encore sur le corridor, s'il est EN AMONT de f_sortie (le
        corridor depasse la sortie et le connecteur revient en arriere)."""
        for i in range(len(coords) - 1, 0, -1):
            f = self._fraction_si_sur_corridor(coords[i], coords[i - 1])
            if f is not None:
                return (i, f) if sens * (f_sortie - f) > self._tolerance_fraction() else (None, None)
        return None, None

    def _fraction_si_sur_corridor(self, point, voisin):
        """Fraction du point sur le corridor s'il est, avec son voisin sur le
        connecteur, a moins de DISTANCE_CONTACT_M de la ligne (deux points :
        un simple croisement de route ne compte pas comme un raccordement)."""
        ligne = self.corridor.geometrie
        position = Point(point[0], point[1], srid=4326)
        if ligne.distance(position) * METRES_PAR_DEGRE > DISTANCE_CONTACT_M:
            return None
        if ligne.distance(Point(voisin[0], voisin[1], srid=4326)) * METRES_PAR_DEGRE > DISTANCE_CONTACT_M:
            return None
        return ligne.project_normalized(position)

    def _tolerance_fraction(self):
        return ECART_MIN_RACCORDEMENT_M / max(self.corridor.distance_m, 1)

    def _construire_segment_corridor(self, f_entree, f_sortie, sens):
        """Portion du corridor entre f_entree et f_sortie, dans le sens de
        parcours. Manoeuvres reelles (capturees a l'import) dans le sens
        aller, decoupees a la portion ; sens retour : une manoeuvre
        synthetique (cf. limitation en tete de module)."""
        corridor = self.corridor
        f_debut, f_fin = sorted((f_entree, f_sortie))
        ligne = corridor.geometrie
        sommets = list(ligne.coords)
        fractions = corridor.fractions_sommets
        indice_debut = bisect.bisect_left(fractions, f_debut)
        indice_fin = bisect.bisect_right(fractions, f_fin)
        coords = (
            [ligne.interpolate_normalized(f_debut).coords]
            + sommets[indice_debut:indice_fin]
            + [ligne.interpolate_normalized(f_fin).coords]
        )
        if sens < 0:
            coords = list(reversed(coords))

        fraction_utilisee = f_fin - f_debut
        distance_m = corridor.distance_m * fraction_utilisee
        duree_s = corridor.duree_s * fraction_utilisee
        manoeuvres = self._manoeuvres_reference_entre(f_debut, f_fin) if sens > 0 else None
        if not manoeuvres:
            manoeuvres = [{
                'type': 0,
                'instruction': '',
                'length': distance_m / 1000,
                'time': duree_s,
                'street_names': list(corridor.libelles_voies),
            }]
        return coords, manoeuvres, distance_m, duree_s

    def _manoeuvres_reference_entre(self, f_debut, f_fin):
        """Manoeuvres reelles du corridor (sens aller) limitees a la portion
        [f_debut, f_fin], reperees par distance cumulee ; la premiere et la
        derniere sont raccourcies au prorata. [] sans manoeuvres capturees."""
        if not self.corridor.manoeuvres_reference:
            return []
        manoeuvres = self._manoeuvres_mises_a_echelle()
        total_km = sum(m.get('length', 0) for m in manoeuvres)
        debut_km, fin_km = f_debut * total_km, f_fin * total_km
        resultat, cumul = [], 0.0
        for m in manoeuvres:
            longueur = m.get('length', 0)
            a, b = cumul, cumul + longueur
            cumul = b
            if longueur <= 0:
                # Depart/arrivee du corridor (longueur nulle) : seulement si
                # la portion commence/finit vraiment a cette extremite.
                if (a <= debut_km + 1e-6 and debut_km <= 1e-6) or (b >= fin_km - 1e-6 and fin_km >= total_km - 1e-6):
                    resultat.append(m)
                continue
            if b <= debut_km or a >= fin_km:
                continue
            part = (min(b, fin_km) - max(a, debut_km)) / longueur
            resultat.append({**m, 'length': longueur * part, 'time': m.get('time', 0) * part})
        return resultat

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
