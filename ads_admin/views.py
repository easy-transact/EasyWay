"""Endpoints transverses du back-office : profil staff, tableau de bord,
notifications, journal d'audit et campagnes publicitaires. Tous reserves au
staff (is_staff), sous /api/staff/ (exempte de signature HMAC, cf.
settings.HMAC_CHEMINS_EXEMPTES)."""

from django.db.models import Count, Exists, OuterRef, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import InscriptionListeAttente, Utilisateur
from accounts.pagination import StaffPagination
from community.models import Incident, StatutIncident
from places.models import Lieu, StatutLieu
from trips.models import Trajet

from .models import CampagnePublicitaire, EntreeAudit, NotificationStaff, PreferencesNotificationStaff
from .serializers import (
    EVENEMENT_CLIC,
    CampagneSerializer,
    EntreeAuditSerializer,
    NotificationStaffSerializer,
    PreferencesNotificationSerializer,
)
from .services import filtre_incidents_suspects, journaliser

INCIDENTS_EN_COURS = [StatutIncident.ACTIF, StatutIncident.EN_ATTENTE]

# Filtres du journal d'audit (onglets du back-office) -> prefixes d'action.
DOMAINES_AUDIT = {
    'places': ['places.'],
    'incidents': ['incidents.'],
    'users': ['users.', 'waitlist.'],
    'referentiel': ['speedzones.', 'establishments.', 'infractions.'],
    'ads': ['ads.'],
}


def _preferences(utilisateur):
    preferences, _ = PreferencesNotificationStaff.objects.get_or_create(utilisateur=utilisateur)
    return preferences


def _notifications_visibles(utilisateur):
    desactives = _preferences(utilisateur).types_desactives
    return NotificationStaff.objects.exclude(type__in=desactives)


def _a_traiter():
    return {
        'places_pending': Lieu.objects.filter(statut=StatutLieu.EN_ATTENTE).count(),
        'incidents_suspect': Incident.objects.filter(statut__in=INCIDENTS_EN_COURS)
        .filter(filtre_incidents_suspects())
        .count(),
        'waitlist_new': InscriptionListeAttente.objects.filter(contacte=False).count(),
    }


def _par_jour(queryset, champ, depuis):
    """{date: nombre} des lignes de `queryset` dont `champ` tombe depuis `depuis`."""
    return dict(
        queryset.filter(**{f'{champ}__date__gte': depuis})
        .annotate(jour=TruncDate(champ))
        .values('jour')
        .annotate(n=Count('pk'))
        .values_list('jour', 'n')
    )


def _variation(actuel, precedent):
    """Variation en % (None si la base est nulle : pas de pourcentage honnete)."""
    if not precedent:
        return None
    return round((actuel - precedent) / precedent * 100, 1)


@extend_schema(tags=['Staff'], summary='Profil du membre staff connecte')
class StaffMoiView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        u = request.user
        return Response({
            'id': str(u.id),
            'name': u.nom_complet,
            'phone': u.telephone,
            'email': u.email,
            'city': u.ville,
            'is_admin': u.is_superuser,
        })


@extend_schema(
    tags=['Staff'],
    summary='Compteurs du menu et de la cloche',
    description=(
        "Appele toutes les 30 s par le back-office : notifications non lues, "
        "derniere notification (pour afficher un toast quand elle change) et "
        "files de moderation en attente."
    ),
)
class BadgesView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        visibles = _notifications_visibles(request.user)
        derniere = visibles.order_by('-mis_a_jour_le').first()
        return Response({
            'unread_notifications': visibles.exclude(lue_par=request.user).count(),
            'latest_notification': (
                NotificationStaffSerializer(derniere, context={'request': request}).data if derniere else None
            ),
            **_a_traiter(),
        })


@extend_schema(tags=['Staff'], summary='Statistiques du tableau de bord')
class StatsView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        aujourdhui = timezone.localdate()
        jour = timezone.timedelta(days=1)
        jours_10 = [aujourdhui - jour * (9 - i) for i in range(10)]
        jours_30 = [aujourdhui - jour * (29 - i) for i in range(30)]
        il_y_a_7 = aujourdhui - jour * 7
        il_y_a_14 = aujourdhui - jour * 14

        # Utilisateurs : total a la fin de chacun des 10 derniers jours.
        total_utilisateurs = Utilisateur.objects.count()
        inscrits = _par_jour(Utilisateur.objects.all(), 'date_joined', il_y_a_14)
        serie_utilisateurs, cumul = [], total_utilisateurs
        for j in reversed(jours_10):
            serie_utilisateurs.insert(0, cumul)
            cumul -= inscrits.get(j, 0)
        nouveaux_7j = sum(n for j, n in inscrits.items() if j > il_y_a_7)

        trajets = _par_jour(Trajet.objects.all(), 'demarre_le', il_y_a_14)
        signales = _par_jour(Incident.objects.all(), 'cree_le', aujourdhui - jour * 29)
        retires = _par_jour(Incident.objects.filter(statut=StatutIncident.RETIRE), 'cree_le', aujourdhui - jour * 29)

        def somme(compteurs, debut, fin):
            return sum(n for j, n in compteurs.items() if debut < j <= fin)

        def taux_faux(debut, fin):
            total = somme(signales, debut, fin)
            return round(somme(retires, debut, fin) / total * 100, 1) if total else 0.0

        taux_7j, taux_7j_precedents = taux_faux(il_y_a_7, aujourdhui), taux_faux(il_y_a_14, il_y_a_7)

        par_ville = (
            Incident.objects.filter(statut__in=INCIDENTS_EN_COURS)
            .exclude(ville='')
            .values('ville')
            .annotate(total=Count('pk'))
            .order_by('-total')[:5]
        )
        lieux = dict(Lieu.objects.values('statut').annotate(n=Count('pk')).values_list('statut', 'n'))
        incidents = dict(Incident.objects.values('statut').annotate(n=Count('pk')).values_list('statut', 'n'))

        return Response({
            'to_handle': _a_traiter(),
            'kpis': {
                'users': {
                    'value': total_utilisateurs,
                    'change_pct': _variation(total_utilisateurs, total_utilisateurs - nouveaux_7j),
                    'series': serie_utilisateurs,
                },
                'trips_today': {
                    'value': trajets.get(aujourdhui, 0),
                    'change_pct': _variation(trajets.get(aujourdhui, 0), trajets.get(il_y_a_7, 0)),
                    'series': [trajets.get(j, 0) for j in jours_10],
                },
                'active_incidents': {
                    'value': incidents.get(StatutIncident.ACTIF, 0) + incidents.get(StatutIncident.EN_ATTENTE, 0),
                    'change_pct': _variation(somme(signales, il_y_a_7, aujourdhui), somme(signales, il_y_a_14, il_y_a_7)),
                    'series': [signales.get(j, 0) for j in jours_10],
                },
                'false_report_rate': {
                    'value': taux_7j,
                    'change_pts': round(taux_7j - taux_7j_precedents, 1),
                    'series': [taux_faux(j - jour, j) for j in jours_10],
                },
            },
            'reports_30d': [
                {'date': j.isoformat(), 'reported': signales.get(j, 0), 'removed': retires.get(j, 0)} for j in jours_30
            ],
            'incidents_by_city': [{'city': v['ville'], 'total': v['total']} for v in par_ville],
            'places': {s: lieux.get(s, 0) for s in (StatutLieu.EN_ATTENTE, StatutLieu.APPROUVE, StatutLieu.REJETE)},
            'incidents': {
                'active': incidents.get(StatutIncident.ACTIF, 0) + incidents.get(StatutIncident.EN_ATTENTE, 0),
                'removed': incidents.get(StatutIncident.RETIRE, 0),
                'expired': incidents.get(StatutIncident.EXPIRE, 0),
            },
            'users': {
                'total': total_utilisateurs,
                'banned': Utilisateur.objects.filter(est_banni=True).count(),
            },
        })


@extend_schema(
    tags=['Staff Notifications'],
    summary='Lister les notifications',
    parameters=[
        OpenApiParameter('unread', OpenApiTypes.BOOL),
        OpenApiParameter('type', OpenApiTypes.STR, description='LIEU/INCIDENT/ABUS/ATTENTE/IMPORT/PUB.'),
        OpenApiParameter('page', OpenApiTypes.INT),
        OpenApiParameter('page_size', OpenApiTypes.INT),
    ],
)
class NotificationListView(APIView):
    permission_classes = [IsAdminUser]
    pagination_class = StaffPagination

    def get(self, request):
        lues = NotificationStaff.lue_par.through.objects.filter(
            notificationstaff_id=OuterRef('pk'), utilisateur_id=request.user.pk
        )
        visibles = _notifications_visibles(request.user)
        notifications = visibles.annotate(lue_par_moi=Exists(lues)).order_by('-mis_a_jour_le')
        if request.query_params.get('unread') in ('1', 'true'):
            notifications = notifications.filter(lue_par_moi=False)
        if request.query_params.get('type'):
            notifications = notifications.filter(type=request.query_params['type'])

        paginateur = self.pagination_class()
        page = paginateur.paginate_queryset(notifications, request)
        reponse = paginateur.get_paginated_response(
            NotificationStaffSerializer(page, many=True, context={'request': request}).data
        )
        reponse.data['unread_count'] = visibles.exclude(lue_par=request.user).count()
        return reponse


@extend_schema(tags=['Staff Notifications'], summary='Marquer une notification comme lue', request=None)
class NotificationLueView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, id):
        notification = get_object_or_404(NotificationStaff, id=id)
        notification.lue_par.add(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Staff Notifications'], summary='Tout marquer comme lu', request=None)
class NotificationToutLuView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        Lien = NotificationStaff.lue_par.through
        non_lues = NotificationStaff.objects.exclude(lue_par=request.user).values_list('pk', flat=True)
        Lien.objects.bulk_create(
            [Lien(notificationstaff_id=pk, utilisateur_id=request.user.pk) for pk in non_lues],
            ignore_conflicts=True,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(tags=['Staff Notifications'], summary='Mes preferences de notification'),
    patch=extend_schema(tags=['Staff Notifications'], summary='Modifier mes preferences de notification'),
)
class PreferencesNotificationView(APIView):
    permission_classes = [IsAdminUser]
    serializer_class = PreferencesNotificationSerializer

    def get(self, request):
        return Response(PreferencesNotificationSerializer(_preferences(request.user)).data)

    def patch(self, request):
        serializer = PreferencesNotificationSerializer(_preferences(request.user), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


@extend_schema(
    tags=['Staff Audit'],
    summary="Journal d'audit",
    parameters=[
        OpenApiParameter('domain', OpenApiTypes.STR, description='places/incidents/users/referentiel/ads.'),
        OpenApiParameter('search', OpenApiTypes.STR, description='Membre du staff ou element concerne.'),
        OpenApiParameter('page', OpenApiTypes.INT),
        OpenApiParameter('page_size', OpenApiTypes.INT),
    ],
    responses={200: EntreeAuditSerializer(many=True)},
)
class AuditListView(APIView):
    permission_classes = [IsAdminUser]
    pagination_class = StaffPagination

    def get(self, request):
        entrees = EntreeAudit.objects.select_related('acteur').order_by('-survenue_le')
        prefixes = DOMAINES_AUDIT.get(request.query_params.get('domain', ''))
        if prefixes:
            filtre = Q()
            for prefixe in prefixes:
                filtre |= Q(action__startswith=prefixe)
            entrees = entrees.filter(filtre)
        recherche = request.query_params.get('search', '').strip()
        if recherche:
            entrees = entrees.filter(
                Q(acteur__nom_complet__icontains=recherche) | Q(valeur_nouvelle__libelle__icontains=recherche)
            )

        paginateur = self.pagination_class()
        page = paginateur.paginate_queryset(entrees, request)
        return paginateur.get_paginated_response(EntreeAuditSerializer(page, many=True).data)


def _campagnes_avec_stats():
    aujourdhui = Q(impressions__survenue_le__date=timezone.localdate())
    return CampagnePublicitaire.objects.annotate(
        impressions_jour=Count('impressions', filter=aujourdhui & ~Q(impressions__evenement=EVENEMENT_CLIC)),
        clics_jour=Count('impressions', filter=aujourdhui & Q(impressions__evenement=EVENEMENT_CLIC)),
    )


@extend_schema_view(
    get=extend_schema(tags=['Staff Ads'], summary='Lister les campagnes et leurs chiffres du jour'),
    post=extend_schema(tags=['Staff Ads'], summary='Creer une campagne', request=CampagneSerializer),
)
class CampagneListCreateView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        campagnes = list(_campagnes_avec_stats().order_by('-debute_le'))
        donnees = CampagneSerializer(campagnes, many=True).data
        impressions = sum(c['impressions_today'] for c in donnees)
        clics = sum(c['clicks_today'] for c in donnees)
        return Response({
            'results': donnees,
            'summary': {
                'impressions_today': impressions,
                'clicks_today': clics,
                'ctr_pct': round(clics / impressions * 100, 1) if impressions else None,
                'active_count': sum(1 for c in donnees if c['status'] in ('ACTIVE', 'PLAFOND')),
            },
        })

    def post(self, request):
        serializer = CampagneSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        campagne = serializer.save()
        journaliser(request.user, 'ads.create', campagne)
        return Response(CampagneSerializer(campagne).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    patch=extend_schema(tags=['Staff Ads'], summary='Modifier une campagne', request=CampagneSerializer),
    delete=extend_schema(tags=['Staff Ads'], summary='Supprimer une campagne'),
)
class CampagneDetailView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, id):
        campagne = get_object_or_404(CampagnePublicitaire, id=id)
        serializer = CampagneSerializer(campagne, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        journaliser(request.user, 'ads.update', campagne, apres={'champs': sorted(request.data.keys())})
        return Response(CampagneSerializer(campagne).data)

    def delete(self, request, id):
        campagne = get_object_or_404(CampagnePublicitaire, id=id)
        journaliser(request.user, 'ads.delete', campagne)
        campagne.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
