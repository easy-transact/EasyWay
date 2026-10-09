from datetime import timedelta

from django.contrib.gis.geos import LineString, Point
from django.contrib.gis.measure import D
from django.db import transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Greatest, Least
from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Parametres
from accounts.pagination import StaffPagination
from ads_admin.services import journaliser
from accounts.serializers import MessageSerializer

from .exceptions import TransitionInvalide
from .geocodage import lancer_geocodage_trajet, libelle_generique
from .services.geo import distance_haversine_m
from .models import StatutTrajet, Trajet, ZoneVitesse
from .polyline import decoder_polyline6
from .serializers import (
    CalculItineraireSerializer,
    ItineraireCandidatSerializer,
    LimiteVitesseSerializer,
    NoterTrajetSerializer,
    TelemetriePositionsSerializer,
    TrajetCreationSerializer,
    TrajetMiseAJourSerializer,
    TrajetModerationSerializer,
    TrajetSerializer,
    ZoneVitesseCreationSerializer,
    ZoneVitesseModificationSerializer,
    ZoneVitesseSerializer,
    ZonesVitesseSurTrajetSerializer,
    ZoneVitesseSurTrajetSerializer,
)
from .services.producteur_evenements import FLUX_POSITIONS, ProducteurRedisStreams
from .services.service_itineraire import ServiceItineraire
from .services.zones_vitesse import zones_sur_trajet

DUREE_PERIODE = {
    'week': timedelta(days=7),
    'month': timedelta(days=30),
}


@extend_schema(
    tags=['Routes'],
    summary="Calculer des candidats d'itineraire",
    description=(
        'view -> ServiceItineraire -> ClientValhalla. Ne persiste rien -- le client '
        "renvoie l'itineraire choisi tel quel a POST /api/trips/ pour le faire persister. "
        "'avoid' (optionnel) exclut reellement les points donnes du graphe de routage "
        "(ex. position d'un incident) -- Valhalla replanifie autour, ce n'est pas un "
        "simple reclassement des candidats existants. 'origin_heading' (optionnel) evite "
        "qu'un recalcul en cours de route demarre par un demi-tour immediat sur une voie "
        "a sens unique ou une chaussee separee. 'alternatives' (defaut true) : false "
        'demande un seul itineraire, reellement honore -- pas de recherche de variantes '
        'cote Valhalla, jamais un simple troncage de la reponse.'
    ),
    request=CalculItineraireSerializer,
    responses={200: ItineraireCandidatSerializer(many=True)},
)
class CalculItineraireView(APIView):
    """POST /api/routes/calculate/ : view -> ServiceItineraire -> ClientValhalla.
    Ne persiste rien -- le client renvoie l'itineraire choisi a POST /api/trips/."""

    def post(self, request):
        serializer = CalculItineraireSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data

        candidats = ServiceItineraire().calculer(
            depart=(donnees['origine_lat'], donnees['origine_lon']),
            arrivee=(donnees['destination_lat'], donnees['destination_lon']),
            utilisateur=request.user,
            eviter=[(p['lat'], p['lon']) for p in donnees['eviter']],
            cap_origine=donnees['cap_origine'],
            alternatives=donnees['alternatives'],
            etapes=[(p['lat'], p['lon']) for p in donnees['etapes']],
        )
        return Response(ItineraireCandidatSerializer(candidats, many=True).data)


@extend_schema_view(
    get=extend_schema(
        operation_id='trajets_lister',
        tags=['Trips'],
        summary="Lister les trajets de l'utilisateur connecte",
        description=(
            "period=week|month|all respecte la retention du plan (Droits."
            "retention_historique_jours) -- tronque toujours 'all', jamais l'inverse. "
            "'truncated_at' est non-null quand la retention du plan a effectivement exclu des trajets."
        ),
        parameters=[
            OpenApiParameter(
                'period', OpenApiTypes.STR, enum=['week', 'month', 'all'], default='all',
            ),
        ],
        responses={
            200: inline_serializer(
                name='TrajetListeReponse',
                fields={
                    'results': TrajetSerializer(many=True),
                    'truncated_at': drf_serializers.DateTimeField(allow_null=True),
                },
            ),
            400: MessageSerializer,
        },
    ),
    post=extend_schema(
        tags=['Trips'],
        summary='Creer un trajet a partir d\'un itineraire choisi',
        description="Demarre immediatement le trajet (PLANIFIE -> ACTIF via la machine a etats).",
        request=TrajetCreationSerializer,
        responses={201: TrajetSerializer},
    ),
)
class TrajetListeCreationView(APIView):
    """GET ?period=week|month|all respecte la retention du plan (Droits.
    retention_historique_jours) -- tronque toujours 'all', jamais l'inverse."""

    def get(self, request):
        periode = request.query_params.get('period', 'all')
        if periode not in (*DUREE_PERIODE, 'all'):
            return Response({'detail': "period must be 'week', 'month' or 'all'."}, status=400)

        queryset = request.user.trajets.order_by('-demarre_le')

        retention_jours = request.user.droits.retention_historique_jours
        limite_retention = timezone.now() - timedelta(days=retention_jours) if retention_jours else None

        depuis = timezone.now() - DUREE_PERIODE[periode] if periode in DUREE_PERIODE else None
        tronque_le = None
        if limite_retention is not None and (depuis is None or limite_retention > depuis):
            # La retention du plan est la contrainte la plus stricte -- mais ne
            # vaut la peine d'etre signalee que si elle exclut reellement des
            # trajets, pas juste parce qu'un plafond existe en theorie.
            if queryset.filter(demarre_le__lt=limite_retention).exists():
                depuis = limite_retention
                tronque_le = limite_retention

        if depuis is not None:
            queryset = queryset.filter(demarre_le__gte=depuis)

        return Response({
            'results': TrajetSerializer(queryset, many=True).data,
            'truncated_at': tronque_le.isoformat() if tronque_le else None,
        })

    def post(self, request):
        serializer = TrajetCreationSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        trajet = serializer.save()
        if libelle_generique(trajet.libelle_origine) or libelle_generique(trajet.libelle_destination):
            # "Votre position" -> nom reel du lieu, en arriere-plan : la reponse
            # n'attend pas Nominatim.
            transaction.on_commit(lambda: lancer_geocodage_trajet(trajet.id))
        return Response(TrajetSerializer(trajet).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        operation_id='trajets_detail', tags=['Trips'], summary="Detail d'un trajet",
        responses={200: TrajetSerializer},
    ),
    patch=extend_schema(
        tags=['Trips'],
        summary="Mettre a jour un trajet (statut, mesures reelles)",
        description='changer_statut() applique la machine a etats du trajet ; une transition illegale renvoie 400.',
        request=TrajetMiseAJourSerializer,
        responses={200: TrajetSerializer, 400: MessageSerializer},
    ),
    delete=extend_schema(tags=['Trips'], summary='Supprimer un trajet', responses={204: None}),
)
class TrajetDetailView(APIView):
    def _objet(self, request, id):
        return get_object_or_404(Trajet, id=id, utilisateur=request.user)

    def get(self, request, id):
        return Response(TrajetSerializer(self._objet(request, id)).data)

    def patch(self, request, id):
        trajet = self._objet(request, id)
        serializer = TrajetMiseAJourSerializer(trajet, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            serializer.save()
        except TransitionInvalide as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(TrajetSerializer(trajet).data)

    def delete(self, request, id):
        self._objet(request, id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    tags=['Trips'],
    summary='Noter un trajet termine',
    description='Refuse avec 400 si le trajet n\'est pas au statut TERMINE.',
    request=NoterTrajetSerializer,
    responses={200: TrajetSerializer, 400: MessageSerializer},
)
class NoterTrajetView(APIView):
    def post(self, request, id):
        trajet = get_object_or_404(Trajet, id=id, utilisateur=request.user)
        if trajet.statut != StatutTrajet.TERMINE:
            return Response(
                {'detail': 'Only a completed trip can be rated.'}, status=status.HTTP_400_BAD_REQUEST
            )
        serializer = NoterTrajetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        trajet.noter(**serializer.validated_data)
        return Response(TrajetSerializer(trajet).data)


@extend_schema(
    tags=['Telemetry'],
    summary='Ingerer un lot de positions GPS',
    description=(
        'Valide et publie sur le flux Redis Streams (ProducteurEvenements), '
        "retourne 202 immediatement -- aucune ecriture en base sur ce chemin de "
        "requete : les positions brutes ne sont jamais persistees, seul leur "
        "agregat 5 minutes (EchantillonVitesse) l'est plus tard, cote consommateur. "
        "Suppression silencieuse (202, rien publie) si l'utilisateur a active "
        'mode_invisible. Le trajet doit appartenir a l\'appelant et etre ACTIF.'
    ),
    request=TelemetriePositionsSerializer,
    responses={202: None, 400: MessageSerializer},
)
class TelemetriePositionsView(APIView):
    def post(self, request):
        if request.user.mode_invisible:
            return Response(status=status.HTTP_202_ACCEPTED)

        serializer = TelemetriePositionsSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        trajet = serializer.validated_data['trajet']

        producteur = ProducteurRedisStreams()
        for position in serializer.validated_data['positions']:
            producteur.publier(FLUX_POSITIONS, {
                'trajet_id': str(trajet.id),
                'lat': position['lat'],
                'lon': position['lon'],
                'vitesse_kmh': position.get('vitesse_kmh'),
                'cap': position.get('cap'),
                'horodatage': position['horodatage'].isoformat(),
            })  # jamais d'identifiant utilisateur publie -- trajet_id suffit au
            # regroupement cote consommateur (cf. cahier des charges, confidentialite)

        _cumuler_telemetrie(trajet, serializer.validated_data['positions'])
        return Response(status=status.HTTP_202_ACCEPTED)


def _cumuler_telemetrie(trajet, positions):
    """Met a jour le resume de telemetrie du trajet (agregats seulement) en une
    requete atomique -- deux lots concurrents ne s'ecrasent pas."""
    positions = sorted(positions, key=lambda p: p['horodatage'])
    vitesses = [p['vitesse_kmh'] for p in positions if p.get('vitesse_kmh') is not None]
    distance = sum(
        distance_haversine_m((a['lat'], a['lon']), (b['lat'], b['lon'])) for a, b in zip(positions, positions[1:])
    )
    maj = {
        'telemetrie_lots': F('telemetrie_lots') + 1,
        'telemetrie_positions': F('telemetrie_positions') + len(positions),
        'telemetrie_nb_vitesses': F('telemetrie_nb_vitesses') + len(vitesses),
        'telemetrie_somme_vitesses': F('telemetrie_somme_vitesses') + sum(vitesses),
        'telemetrie_distance_m': F('telemetrie_distance_m') + round(distance),
        # NULL ignore par GREATEST/LEAST cote PostgreSQL : premier lot inclus.
        'telemetrie_premiere_le': Least('telemetrie_premiere_le', Value(positions[0]['horodatage'])),
        'telemetrie_derniere_le': Greatest('telemetrie_derniere_le', Value(positions[-1]['horodatage'])),
    }
    if vitesses:
        maj['telemetrie_vitesse_max'] = Greatest('telemetrie_vitesse_max', Value(float(max(vitesses))))
    Trajet.objects.filter(pk=trajet.pk).update(**maj)


# Couloir etroit (30m) autour de ZoneVitesse.geometrie : cette geometrie est
# deja calee sur le graphe routier reel via ServiceItineraire au moment de la
# creation (cf. ZoneVitesseListCreateView.post), meme raisonnement que le
# BUFFER_M_DEFAUT du matching Incident (community/views.py) -- pas la peine
# d'un couloir large pour une ligne deja precise.
BUFFER_M_ZONE_VITESSE = 30
# Limite de repli hors de toute ZoneVitesse definie par le staff -- valeur
# communiquee par l'app mobile (limite urbaine usuelle au Cameroun), pas une
# donnee officielle mesuree route par route.
LIMITE_VITESSE_DEFAUT_KMH = 60


@extend_schema(
    tags=['Speed Zones'],
    summary='Limite de vitesse applicable a une position',
    description=(
        "Cherche la ZoneVitesse active la plus proche (< 30m, meme couloir que le "
        "matching des Incident sur le graphe routier) couvrant lat/lon ; si plusieurs "
        "zones actives se chevauchent a cet endroit, la plus restrictive gagne. A "
        f"defaut de zone, renvoie la limite de repli ({LIMITE_VITESSE_DEFAUT_KMH} km/h, "
        "source='default'). La detection de depassement (position + vitesse instantanee) "
        "reste cote client -- l'app a deja la vitesse GPS en temps reel, un aller-retour "
        "reseau n'apporterait que de la latence sur une alerte qui doit etre immediate."
    ),
    parameters=[
        OpenApiParameter('lat', OpenApiTypes.FLOAT, required=True),
        OpenApiParameter('lon', OpenApiTypes.FLOAT, required=True),
    ],
    responses={200: LimiteVitesseSerializer, 400: MessageSerializer},
)
class LimiteVitesseView(APIView):
    def get(self, request):
        lat = request.query_params.get('lat')
        lon = request.query_params.get('lon')
        if lat is None or lon is None:
            return Response({'detail': "'lat' and 'lon' are required."}, status=400)
        try:
            lat, lon = float(lat), float(lon)
        except ValueError:
            return Response({'detail': "'lat'/'lon' must be numbers."}, status=400)

        position = Point(lon, lat, srid=4326)
        zone = (
            ZoneVitesse.objects.filter(
                actif=True,
                geometrie__isnull=False,
                geometrie__distance_lte=(position, D(m=BUFFER_M_ZONE_VITESSE)),
            )
            .order_by('vitesse_max_kmh')  # la plus restrictive d'abord en cas de chevauchement
            .first()
        )
        if zone is not None:
            return Response(LimiteVitesseSerializer({
                'speed_limit_kmh': zone.vitesse_max_kmh,
                'zone_id': zone.id,
                'zone_name': zone.nom,
                'source': 'zone',
            }).data)
        return Response(LimiteVitesseSerializer({
            'speed_limit_kmh': LIMITE_VITESSE_DEFAUT_KMH,
            'zone_id': None,
            'zone_name': None,
            'source': 'default',
        }).data)


@extend_schema(
    tags=['Speed Zones'],
    summary="Zones de vitesse le long d'un trajet",
    description=(
        "Remplace le sondage de /api/speed-limit/ pendant la conduite : un appel par "
        "itineraire (au depart et a chaque recalcul) avec geometry = routes/calculate -> "
        "geometry. Renvoie chaque zone active traversee, avec start_m/end_m en metres "
        "depuis le debut du trajet, triees par start_m. Des zones peuvent se chevaucher : "
        "la plus restrictive gagne, meme regle que /api/speed-limit/. Hors de toute zone, "
        "la limite reste celle du road_class des manoeuvres. buffer_m (defaut "
        f"{BUFFER_M_ZONE_VITESSE}, max 100) : distance maximale zone/trajet."
    ),
    request=ZonesVitesseSurTrajetSerializer,
    responses={200: ZoneVitesseSurTrajetSerializer(many=True), 400: MessageSerializer},
)
class ZonesVitesseSurTrajetView(APIView):
    def post(self, request):
        serializer = ZonesVitesseSurTrajetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resultats = zones_sur_trajet(
            serializer.validated_data['geometry'], serializer.validated_data['buffer_m'],
        )
        return Response(ZoneVitesseSurTrajetSerializer(resultats, many=True).data)


def _libelle_zone(zone):
    return f'{zone.nom} · {zone.vitesse_max_kmh} km/h' if zone.nom else str(zone)


def _tracer_zone(utilisateur, depart, arrivee):
    """Trace routier reel entre deux points (lat, lon) : LineString, None si
    Valhalla renvoie moins de 2 points, False si aucun itineraire."""
    # ServiceItineraire lit utilisateur.parametres -- absent pour un compte
    # cree via createsuperuser (jamais instancie hors de l'inscription
    # normale, cf. accounts/serializers.py:InscriptionSerializer). Le staff
    # n'a par ailleurs pas a se soucier de configurer ses Parametres avant
    # de pouvoir creer une zone.
    Parametres.objects.get_or_create(utilisateur=utilisateur)
    candidats = ServiceItineraire().calculer(
        depart=depart, arrivee=arrivee, utilisateur=utilisateur, alternatives=False,
    )
    if not candidats:
        return False
    points = decoder_polyline6(candidats[0]['geometrie'])
    return LineString(points, srid=4326) if len(points) >= 2 else None


@extend_schema_view(
    get=extend_schema(
        tags=['Staff Speed Zones'],
        summary='Lister les zones de vitesse',
        parameters=[
            OpenApiParameter('active', OpenApiTypes.BOOL),
            OpenApiParameter('page', OpenApiTypes.INT),
            OpenApiParameter('page_size', OpenApiTypes.INT),
        ],
        responses={200: ZoneVitesseSerializer(many=True)},
    ),
    post=extend_schema(
        tags=['Staff Speed Zones'],
        summary='Creer une zone de vitesse',
        description=(
            'Reserve au staff (is_staff). Calcule le trace routier reel entre '
            'origin et destination via ServiceItineraire (meme service que '
            'POST /api/routes/calculate/) plutot que de stocker une ligne droite.'
        ),
        request=ZoneVitesseCreationSerializer,
        responses={201: ZoneVitesseSerializer, 400: MessageSerializer},
    ),
)
class ZoneVitesseListCreateView(APIView):
    permission_classes = [IsAdminUser]
    pagination_class = StaffPagination

    def get(self, request):
        zones = ZoneVitesse.objects.all().order_by('-cree_le')
        actif = request.query_params.get('active')
        if actif is not None:
            zones = zones.filter(actif=actif.lower() == 'true')

        paginateur = self.pagination_class()
        page = paginateur.paginate_queryset(zones, request)
        return paginateur.get_paginated_response(ZoneVitesseSerializer(page, many=True).data)

    def post(self, request):
        serializer = ZoneVitesseCreationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data

        geometrie_ligne = _tracer_zone(
            request.user, (donnees['origin_lat'], donnees['origin_lon']),
            (donnees['destination_lat'], donnees['destination_lon']),
        )
        if geometrie_ligne is False:
            return Response(
                {'detail': 'No route could be computed between these two points.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        zone = ZoneVitesse.objects.create(
            nom=donnees.get('nom', ''),
            point_depart=Point(donnees['origin_lon'], donnees['origin_lat'], srid=4326),
            point_arrivee=Point(donnees['destination_lon'], donnees['destination_lat'], srid=4326),
            geometrie=geometrie_ligne,
            vitesse_max_kmh=donnees['vitesse_max_kmh'],
            cree_par=request.user,
        )
        journaliser(request.user, 'speedzones.create', zone, libelle=_libelle_zone(zone))
        return Response(ZoneVitesseSerializer(zone).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        tags=['Staff Speed Zones'],
        summary="Detail d'une zone de vitesse",
        responses={200: ZoneVitesseSerializer},
    ),
    patch=extend_schema(
        tags=['Staff Speed Zones'],
        summary='Modifier une zone de vitesse',
        description='nom/vitesse/actif seulement -- recreer la zone pour deplacer ses points.',
        request=ZoneVitesseModificationSerializer,
        responses={200: ZoneVitesseSerializer},
    ),
    delete=extend_schema(
        tags=['Staff Speed Zones'],
        summary='Supprimer une zone de vitesse',
        responses={204: None},
    ),
)
class ZoneVitesseDetailView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, id):
        zone = get_object_or_404(ZoneVitesse, id=id)
        return Response(ZoneVitesseSerializer(zone).data)

    def patch(self, request, id):
        zone = get_object_or_404(ZoneVitesse, id=id)
        serializer = ZoneVitesseModificationSerializer(zone, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        points = serializer.validated_data.pop('points', None)
        if points:
            # Points deplaces (glisser-deposer) : on recalcule le trace reel.
            geometrie_ligne = _tracer_zone(
                request.user, (points['origin_lat'], points['origin_lon']),
                (points['destination_lat'], points['destination_lon']),
            )
            if geometrie_ligne is False:
                return Response(
                    {'detail': 'No route could be computed between these two points.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            zone.point_depart = Point(points['origin_lon'], points['origin_lat'], srid=4326)
            zone.point_arrivee = Point(points['destination_lon'], points['destination_lat'], srid=4326)
            zone.geometrie = geometrie_ligne
        serializer.save()
        journaliser(
            request.user, 'speedzones.update', zone, libelle=_libelle_zone(zone),
            apres={'champs': sorted(request.data.keys())},
        )
        return Response(ZoneVitesseSerializer(zone).data)

    def delete(self, request, id):
        zone = get_object_or_404(ZoneVitesse, id=id)
        journaliser(request.user, 'speedzones.delete', zone, libelle=_libelle_zone(zone))
        zone.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    tags=['Staff Trips'],
    summary='Lister les voyages du jour',
    description=(
        'Reserve au staff (is_staff). Voyages demarres a la date donnee '
        "(defaut aujourd'hui) -- Trajet n'a pas de date de creation separee, "
        "demarre_le est la seule reference temporelle utilisable pour "
        '"les voyages de la journee". incidents_on_route est calcule cote '
        'serveur par topologie (meme mecanisme que /api/incidents/along-route/), '
        'pas le compteur incidents_evites auto-declare par le client.'
    ),
    parameters=[
        OpenApiParameter('date', OpenApiTypes.DATE, description='Un seul jour (defaut aujourd\'hui, heure locale).'),
        OpenApiParameter('date_from', OpenApiTypes.DATE, description='Debut de periode (inclus), prioritaire sur date.'),
        OpenApiParameter('date_to', OpenApiTypes.DATE, description='Fin de periode (incluse).'),
        OpenApiParameter('status', OpenApiTypes.STR, description='PLANIFIE/ACTIF/TERMINE/ANNULE.'),
        OpenApiParameter('search', OpenApiTypes.STR, description='Voyageur (nom/telephone) ou libelles.'),
        OpenApiParameter('user', OpenApiTypes.UUID, description='Trajets d\'un utilisateur, toutes dates.'),
        OpenApiParameter('with_geometry', OpenApiTypes.BOOL, description='Ajoute un trace simplifie (carte).'),
        OpenApiParameter('page', OpenApiTypes.INT),
        OpenApiParameter('page_size', OpenApiTypes.INT),
    ],
    responses={200: TrajetModerationSerializer(many=True)},
)
class TrajetModerationListView(APIView):
    permission_classes = [IsAdminUser]
    pagination_class = StaffPagination

    def get(self, request):
        params = request.query_params
        trajets = Trajet.objects.select_related('utilisateur').order_by(F('demarre_le').desc(nulls_last=True))

        if params.get('user'):
            # Page d'un utilisateur : tout son historique, y compris les trajets
            # jamais demarres (demarre_le null), sauf periode explicite.
            trajets = trajets.filter(utilisateur_id=params['user'])
        debut = parse_date(params.get('date_from') or '')
        fin = parse_date(params.get('date_to') or '')
        if debut or fin:
            if debut:
                trajets = trajets.filter(demarre_le__date__gte=debut)
            if fin:
                trajets = trajets.filter(demarre_le__date__lte=fin)
        elif not params.get('user'):
            date_cible = parse_date(params.get('date') or '') or timezone.localdate()
            trajets = trajets.filter(demarre_le__date=date_cible)

        if params.get('status'):
            trajets = trajets.filter(statut=params['status'])
        recherche = params.get('search', '').strip()
        if recherche:
            trajets = trajets.filter(
                Q(utilisateur__nom_complet__icontains=recherche)
                | Q(utilisateur__telephone__icontains=recherche)
                | Q(libelle_origine__icontains=recherche)
                | Q(libelle_destination__icontains=recherche)
            )

        paginateur = self.pagination_class()
        page = paginateur.paginate_queryset(trajets, request)
        donnees = TrajetModerationSerializer(page, many=True).data
        if params.get('with_geometry') in ('1', 'true'):
            for ligne, trajet in zip(donnees, page):
                ligne['geometry'] = _trace_leaflet(trajet.geometrie, max_points=200)
        return paginateur.get_paginated_response(donnees)


def _trace_leaflet(ligne, max_points=None):
    """LineString -> [[lat, lon], ...] (ordre Leaflet), sous-echantillonne a
    max_points en gardant toujours le premier et le dernier point."""
    if not ligne:
        return []
    coords = [[lat, lon] for lon, lat in ligne.coords]
    if max_points and len(coords) > max_points:
        pas = len(coords) / (max_points - 1)
        coords = [coords[int(i * pas)] for i in range(max_points - 1)] + [coords[-1]]
    return coords


@extend_schema(
    tags=['Staff Trips'],
    summary="Detail d'un trajet (moderation)",
    description=(
        'Reserve au staff (is_staff). Trajet complet : voyageur, positions de depart/'
        "arrivee, trace, etapes, itineraires proposes (dont celui choisi), telemetrie "
        'recue et signalements actifs sur le trajet.'
    ),
    responses={200: TrajetModerationSerializer},
)
class TrajetModerationDetailView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, id):
        trajet = get_object_or_404(Trajet.objects.select_related('utilisateur'), id=id)
        donnees = TrajetModerationSerializer(trajet).data
        utilisateur = trajet.utilisateur
        donnees.update({
            'traveler': {
                'id': str(utilisateur.id),
                'name': utilisateur.nom_complet,
                'phone': utilisateur.telephone,
                'vehicle_type': utilisateur.type_vehicule,
                'plan': utilisateur.formule,
            },
            'origin': {'lat': trajet.position_origine.y, 'lon': trajet.position_origine.x},
            'destination': {'lat': trajet.position_destination.y, 'lon': trajet.position_destination.x},
            'geometry': _trace_leaflet(trajet.geometrie),
            'waypoints': [{'lat': e.position.y, 'lon': e.position.x} for e in trajet.etapes.all()],
            'routes': [
                {
                    'label': i.libelle,
                    'distance_m': i.distance,
                    'duration_s': i.duree,
                    'duration_with_traffic_s': i.duree_avec_trafic,
                    'traffic_level': i.niveau_trafic,
                    'recommended': i.est_recommande,
                    'chosen': i.identifiant == trajet.itineraire_choisi,
                }
                for i in trajet.itineraires.all()
            ],
            'declared_incidents_avoided': trajet.incidents_evites,
            'comment': trajet.commentaire,
            'duration_gap_pct': trajet.ecart_duree_pourcent(),
        })
        return Response(donnees)
