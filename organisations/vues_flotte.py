"""
Suivi de flotte (phase 3) : ce que l'agence voit de ses vehicules -- position
en direct, trajets, signalements de ses chauffeurs, exces de vitesse,
notifications -- et la liste des vehicules proposee au chauffeur dans
l'application mobile. Uniquement les trajets declares avec un vehicule de
l'agence (cf. flotte.py), jamais les trajets personnels.
"""

from django.db.models import Count, Exists, OuterRef
from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from community.models import Incident
from trips.models import StatutTrajet, Trajet
from trips.serializers import TrajetModerationSerializer

from .flotte import DELAI_POSITION_EN_DIRECT, vehicules_disponibles
from .models import ExcesVitesse, NotificationOrganisation
from .views import VueOrganisation, _paginer

# --- Serializers ---------------------------------------------------------------


def _vehicule(trajet_ou_exces):
    v = trajet_ou_exces.vehicule
    return {'id': str(v.id), 'plate': v.immatriculation, 'type': v.type} if v else None


def _chauffeur(trajet_ou_exces):
    c = trajet_ou_exces.chauffeur
    return {'id': str(c.id), 'name': c.nom_complet, 'phone': c.telephone} if c else None


class TrajetFlotteSerializer(TrajetModerationSerializer):
    vehicle = serializers.SerializerMethodField()
    driver = serializers.SerializerMethodField()
    speeding_count = serializers.IntegerField(source='nb_exces', read_only=True, default=None)
    reports_count = serializers.IntegerField(source='nb_signalements', read_only=True, default=None)

    class Meta(TrajetModerationSerializer.Meta):
        fields = [
            'id', 'vehicle', 'driver', 'origin_label', 'destination_label', 'status', 'started_at', 'ended_at',
            'planned_distance_m', 'planned_duration_s', 'actual_distance_m', 'actual_duration_s', 'rating',
            'telemetry', 'speeding_count', 'reports_count',
        ]
        read_only_fields = fields

    def get_vehicle(self, trajet) -> dict:
        return _vehicule(trajet)

    def get_driver(self, trajet) -> dict:
        return _chauffeur(trajet)


class ExcesVitesseSerializer(serializers.ModelSerializer):
    trip_id = serializers.UUIDField(source='trajet_id', read_only=True)
    vehicle = serializers.SerializerMethodField()
    driver = serializers.SerializerMethodField()
    limit_kmh = serializers.IntegerField(source='limite_kmh')
    max_speed_kmh = serializers.FloatField(source='vitesse_max_kmh')
    over_kmh = serializers.FloatField(source='depassement_kmh')
    zone_name = serializers.CharField(source='nom_zone')
    lat = serializers.SerializerMethodField()
    lon = serializers.SerializerMethodField()
    positions = serializers.IntegerField(source='nb_positions')
    started_at = serializers.DateTimeField(source='debut_le')
    ended_at = serializers.DateTimeField(source='fin_le')
    duration_s = serializers.SerializerMethodField()

    class Meta:
        model = ExcesVitesse
        fields = [
            'id', 'trip_id', 'vehicle', 'driver', 'limit_kmh', 'max_speed_kmh', 'over_kmh', 'zone_name',
            'lat', 'lon', 'positions', 'started_at', 'ended_at', 'duration_s',
        ]
        read_only_fields = fields

    def get_vehicle(self, exces) -> dict:
        return _vehicule(exces)

    def get_driver(self, exces) -> dict:
        return _chauffeur(exces)

    def get_lat(self, exces) -> float:
        return exces.position.y

    def get_lon(self, exces) -> float:
        return exces.position.x

    def get_duration_s(self, exces) -> int:
        return round((exces.fin_le - exces.debut_le).total_seconds())


class SignalementFlotteSerializer(serializers.ModelSerializer):
    type_label = serializers.CharField(source='get_type_display')
    subtype = serializers.CharField(source='sous_type')
    street_name = serializers.CharField(source='nom_voie')
    city = serializers.CharField(source='ville')
    status = serializers.CharField(source='statut')
    disputes = serializers.IntegerField(source='infirmations')
    lat = serializers.SerializerMethodField()
    lon = serializers.SerializerMethodField()
    trip_id = serializers.UUIDField(source='trajet_id')
    vehicle = serializers.SerializerMethodField()
    driver = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(source='cree_le')

    class Meta:
        model = Incident
        fields = [
            'id', 'type', 'type_label', 'subtype', 'street_name', 'city', 'status', 'confirmations', 'disputes',
            'lat', 'lon', 'trip_id', 'vehicle', 'driver', 'created_at',
        ]
        read_only_fields = fields

    def get_lat(self, incident) -> float:
        return incident.position.y

    def get_lon(self, incident) -> float:
        return incident.position.x

    def get_vehicle(self, incident) -> dict:
        return _vehicule(incident.trajet)

    def get_driver(self, incident) -> dict:
        return _chauffeur(incident.trajet)


class NotificationOrganisationSerializer(serializers.ModelSerializer):
    title = serializers.CharField(source='titre')
    text = serializers.CharField(source='texte')
    link = serializers.CharField(source='lien')
    created_at = serializers.DateTimeField(source='cree_le')
    read = serializers.BooleanField(source='lue_par_moi', default=False)

    class Meta:
        model = NotificationOrganisation
        fields = ['id', 'type', 'title', 'text', 'link', 'created_at', 'read']
        read_only_fields = fields


# --- Filtres communs -----------------------------------------------------------


def _filtrer(queryset, request, champ_date, prefixe=''):
    """?vehicle=&driver=&date_from=&date_to= sur trajets, exces ou signalements
    (prefixe = chemin vers le trajet, ex. 'trajet__')."""
    params = request.query_params
    if params.get('vehicle'):
        queryset = queryset.filter(**{f'{prefixe}vehicule_id': params['vehicle']})
    if params.get('driver'):
        queryset = queryset.filter(**{f'{prefixe}chauffeur_id': params['driver']})
    debut = parse_date(params.get('date_from') or '')
    fin = parse_date(params.get('date_to') or '')
    if debut:
        queryset = queryset.filter(**{f'{champ_date}__date__gte': debut})
    if fin:
        queryset = queryset.filter(**{f'{champ_date}__date__lte': fin})
    return queryset


FILTRES = [
    OpenApiParameter('vehicle', OpenApiTypes.UUID),
    OpenApiParameter('driver', OpenApiTypes.UUID),
    OpenApiParameter('date_from', OpenApiTypes.DATE),
    OpenApiParameter('date_to', OpenApiTypes.DATE),
    OpenApiParameter('page', OpenApiTypes.INT),
    OpenApiParameter('page_size', OpenApiTypes.INT),
]


def _trajets_annotes(organisation):
    return (
        Trajet.objects.filter(organisation=organisation)
        .select_related('vehicule', 'chauffeur', 'utilisateur')
        .annotate(
            nb_exces=Count('exces_vitesse', distinct=True),
            nb_signalements=Count('signalements', distinct=True),
        )
    )


# --- Portail agence ------------------------------------------------------------


@extend_schema(
    tags=['Organisation Fleet'], summary='Vehicules en route (carte en direct)',
    description=(
        "Trajets ACTIF de l'agence dont la derniere position date de moins de 15 min. "
        'A rafraichir toutes les 10-15 s.'
    ),
)
class EnDirectView(VueOrganisation):
    def get(self, request):
        trajets = (
            Trajet.objects.filter(
                organisation=self.organisation, statut=StatutTrajet.ACTIF,
                derniere_position_le__gte=timezone.now() - DELAI_POSITION_EN_DIRECT,
            )
            .select_related('vehicule', 'chauffeur')
            .order_by('vehicule__immatriculation')
        )
        return Response([
            {
                'trip_id': str(t.id), 'vehicle': _vehicule(t), 'driver': _chauffeur(t),
                'lat': t.derniere_position.y, 'lon': t.derniere_position.x,
                'speed_kmh': t.derniere_vitesse_kmh, 'heading': t.dernier_cap,
                'updated_at': t.derniere_position_le,
                'origin_label': t.libelle_origine, 'destination_label': t.libelle_destination,
                'started_at': t.demarre_le,
            }
            for t in trajets
            if t.derniere_position is not None
        ])


@extend_schema(
    tags=['Organisation Fleet'], summary="Trajets faits avec les vehicules de l'agence",
    parameters=FILTRES + [OpenApiParameter('status', OpenApiTypes.STR)],
    responses={200: TrajetFlotteSerializer(many=True)},
)
class TrajetsFlotteView(VueOrganisation):
    def get(self, request):
        trajets = _filtrer(_trajets_annotes(self.organisation), request, 'demarre_le')
        if request.query_params.get('status'):
            trajets = trajets.filter(statut=request.query_params['status'])
        return _paginer(self, request, trajets.order_by('-demarre_le'), TrajetFlotteSerializer)


@extend_schema(
    tags=['Organisation Fleet'], summary="Detail d'un trajet de l'agence",
    description='Trace, derniere position, exces de vitesse et signalements du chauffeur pendant ce trajet.',
)
class TrajetFlotteDetailView(VueOrganisation):
    def get(self, request, id):
        trajet = get_object_or_404(_trajets_annotes(self.organisation), id=id)
        donnees = TrajetFlotteSerializer(trajet).data
        donnees.update({
            'origin': {'lat': trajet.position_origine.y, 'lon': trajet.position_origine.x},
            'destination': {'lat': trajet.position_destination.y, 'lon': trajet.position_destination.x},
            'geometry': [[lat, lon] for lon, lat in trajet.geometrie.coords] if trajet.geometrie else [],
            'last_position': (
                {
                    'lat': trajet.derniere_position.y, 'lon': trajet.derniere_position.x,
                    'speed_kmh': trajet.derniere_vitesse_kmh, 'at': trajet.derniere_position_le,
                }
                if trajet.derniere_position else None
            ),
            'speeding': ExcesVitesseSerializer(
                trajet.exces_vitesse.select_related('vehicule', 'chauffeur').order_by('debut_le'), many=True
            ).data,
            'reports': SignalementFlotteSerializer(
                trajet.signalements.select_related('trajet__vehicule', 'trajet__chauffeur').order_by('cree_le'),
                many=True,
            ).data,
        })
        return Response(donnees)


@extend_schema(
    tags=['Organisation Fleet'], summary="Signalements faits par les chauffeurs pendant un trajet de l'agence",
    parameters=FILTRES, responses={200: SignalementFlotteSerializer(many=True)},
)
class SignalementsFlotteView(VueOrganisation):
    def get(self, request):
        signalements = Incident.objects.filter(trajet__organisation=self.organisation).select_related(
            'trajet__vehicule', 'trajet__chauffeur'
        )
        signalements = _filtrer(signalements, request, 'cree_le', prefixe='trajet__')
        return _paginer(self, request, signalements.order_by('-cree_le'), SignalementFlotteSerializer)


@extend_schema(
    tags=['Organisation Fleet'], summary='Exces de vitesse',
    description="Episodes de depassement (limite de zone ou 60 km/h par defaut, + tolerance de l'agence).",
    parameters=FILTRES, responses={200: ExcesVitesseSerializer(many=True)},
)
class ExcesVitesseView(VueOrganisation):
    def get(self, request):
        exces = ExcesVitesse.objects.filter(organisation=self.organisation).select_related('vehicule', 'chauffeur')
        exces = _filtrer(exces, request, 'debut_le')
        return _paginer(self, request, exces.order_by('-debut_le'), ExcesVitesseSerializer)


def _notifications(vue):
    lues = NotificationOrganisation.lue_par.through.objects.filter(
        notificationorganisation_id=OuterRef('pk'), utilisateur_id=vue.request.user.pk
    )
    return vue.organisation.notifications.annotate(lue_par_moi=Exists(lues))


@extend_schema(
    tags=['Organisation Fleet'], summary="Notifications de l'agence",
    parameters=[OpenApiParameter('unread', OpenApiTypes.BOOL), OpenApiParameter('page', OpenApiTypes.INT)],
    responses={200: NotificationOrganisationSerializer(many=True)},
)
class NotificationsOrganisationView(VueOrganisation):
    def get(self, request):
        notifications = _notifications(self).order_by('-cree_le')
        if request.query_params.get('unread') in ('1', 'true'):
            notifications = notifications.filter(lue_par_moi=False)
        reponse = _paginer(self, request, notifications, NotificationOrganisationSerializer)
        reponse.data['unread_count'] = _notifications(self).filter(lue_par_moi=False).count()
        return reponse


@extend_schema(tags=['Organisation Fleet'], summary='Marquer une notification comme lue', request=None)
class NotificationOrganisationLueView(VueOrganisation):
    role_ecriture = VueOrganisation.role_lecture

    def post(self, request, id):
        get_object_or_404(self.organisation.notifications, id=id).lue_par.add(request.user)
        return Response(status=204)


@extend_schema(tags=['Organisation Fleet'], summary='Tout marquer comme lu', request=None)
class NotificationsOrganisationToutLuView(VueOrganisation):
    role_ecriture = VueOrganisation.role_lecture

    def post(self, request):
        Lien = NotificationOrganisation.lue_par.through
        non_lues = _notifications(self).filter(lue_par_moi=False).values_list('pk', flat=True)
        Lien.objects.bulk_create(
            [Lien(notificationorganisation_id=pk, utilisateur_id=request.user.pk) for pk in non_lues],
            ignore_conflicts=True,
        )
        return Response(status=204)


@extend_schema(
    tags=['Organisation Fleet'], summary='Compteurs du menu et de la cloche (portail agence)',
    description='Appele toutes les 30 s : notifications non lues, derniere notification, vehicules en route, exces du jour.',
)
class BadgesOrganisationView(VueOrganisation):
    def get(self, request):
        derniere = _notifications(self).order_by('-cree_le').first()
        return Response({
            'unread_notifications': _notifications(self).filter(lue_par_moi=False).count(),
            'latest_notification': NotificationOrganisationSerializer(derniere).data if derniere else None,
            'vehicles_on_road': Trajet.objects.filter(
                organisation=self.organisation, statut=StatutTrajet.ACTIF,
                derniere_position_le__gte=timezone.now() - DELAI_POSITION_EN_DIRECT,
            ).count(),
            'speeding_today': ExcesVitesse.objects.filter(
                organisation=self.organisation, debut_le__date=timezone.localdate()
            ).count(),
        })


# --- Application mobile -----------------------------------------------------------


@extend_schema(
    tags=['Trips'], summary='Vehicules que je peux declarer conduire',
    description=(
        "Vehicules en service des agences ou l'appelant a une fiche chauffeur active "
        "(fiche saisie par l'agence avec son numero de telephone). `assigned` = vehicule "
        "qui lui est attribue, a preselectionner. Liste vide = conducteur sans agence : "
        "ne pas afficher le choix du vehicule. L'id choisi part dans POST /api/trips/ "
        '(vehicle_id).'
    ),
)
class MesVehiculesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response([
            {
                'id': str(v.id), 'plate': v.immatriculation, 'type': v.type, 'make': v.marque, 'model': v.modele,
                'organisation': {'id': str(v.organisation_id), 'name': str(v.organisation)},
                'driver_id': str(fiche.id), 'assigned': attribue,
            }
            for v, fiche, attribue in vehicules_disponibles(request.user)
        ])


def resume_flotte_tableau_de_bord(organisation, jours=7):
    """Ajouts phase 3 au tableau de bord de l'agence (cf. views.TableauDeBordView)."""
    depuis = timezone.now() - timezone.timedelta(days=jours)
    trajets = Trajet.objects.filter(organisation=organisation)
    return {
        'on_road': trajets.filter(
            statut=StatutTrajet.ACTIF, derniere_position_le__gte=timezone.now() - DELAI_POSITION_EN_DIRECT
        ).count(),
        'trips_today': trajets.filter(demarre_le__date=timezone.localdate()).count(),
        'trips_7d': trajets.filter(demarre_le__gte=depuis).count(),
        'speeding_7d': ExcesVitesse.objects.filter(organisation=organisation, debut_le__gte=depuis).count(),
        'reports_7d': Incident.objects.filter(trajet__organisation=organisation, cree_le__gte=depuis).count(),
        'top_speeding_drivers': [
            {'driver_id': str(ligne['chauffeur_id']), 'name': ligne['chauffeur__nom_complet'], 'count': ligne['total']}
            for ligne in
            ExcesVitesse.objects.filter(organisation=organisation, debut_le__gte=depuis, chauffeur__isnull=False)
            .values('chauffeur_id', 'chauffeur__nom_complet')
            .annotate(total=Count('pk'))
            .order_by('-total')[:5]
        ],
        'drivers_with_app': organisation.chauffeurs.filter(utilisateur__isnull=False).exclude(statut='ARCHIVE').count(),
    }


