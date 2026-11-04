"""
Signalements officiels des autorites (mairie, police) depuis le portail
organisation -- Phase 1 du plan "Comptes autorites". Reserve aux organisations
de type MAIRIE / POLICE ; un signalement officiel est pose via
community.ServiceIncident.signaler_officiel (marque confirme, avec periode).
"""

from django.contrib.gis.geos import Point
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response

from community.models import StatutIncident
from community.serializers import IncidentSerializer, SignalementOfficielCreationSerializer
from community.services import ServiceIncident
from community import temps_reel

from . import services
from .models import RoleOrganisation, TypeOrganisation
from .views import VueOrganisation

# Types d'organisation autorises a poser des signalements officiels.
TYPES_AUTORITE = {TypeOrganisation.MAIRIE, TypeOrganisation.POLICE}
# Statuts visibles dans le portail : en cours + programmes a venir.
STATUTS_VISIBLES = [StatutIncident.ACTIF, StatutIncident.EN_ATTENTE, StatutIncident.PROGRAMME]


class _VueAutorite(VueOrganisation):
    """Refuse l'acces si l'organisation n'est pas une autorite (mairie/police)."""

    role_ecriture = RoleOrganisation.GESTIONNAIRE

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if self.organisation.type not in TYPES_AUTORITE:
            self.permission_denied(request, message='Reserved to town hall / police accounts.', code='not_an_authority')


@extend_schema_view(
    get=extend_schema(tags=['Organisation Authority'], summary='Signalements officiels de l\'autorite',
                      responses={200: IncidentSerializer(many=True)}),
    post=extend_schema(tags=['Organisation Authority'], summary='Poser un signalement officiel (gestionnaire)',
                       request=SignalementOfficielCreationSerializer, responses={201: IncidentSerializer}),
)
class SignalementsOfficielsView(_VueAutorite):
    def get(self, request):
        signalements = (
            self.organisation.signalements_officiels
            .filter(statut__in=STATUTS_VISIBLES, expire_le__gt=timezone.now())
            .order_by('-debut_le')
        )
        return Response(IncidentSerializer(signalements, many=True).data)

    def post(self, request):
        serializer = SignalementOfficielCreationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        incident = ServiceIncident().signaler_officiel(
            membre=request.user, organisation=self.organisation, type_incident=d['type'],
            position=Point(d['lon'], d['lat'], srid=4326), debut_le=d['debut_le'], fin_le=d['expire_le'],
            cap=d.get('cap'), sous_type=d.get('sous_type', ''),
        )
        services.journaliser(self.organisation, request.user, 'signalement_officiel.cree', incident,
                             type=incident.type, sous_type=incident.sous_type or '')
        return Response(IncidentSerializer(incident).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    delete=extend_schema(tags=['Organisation Authority'], summary='Lever un signalement officiel (gestionnaire)',
                         responses={204: None}),
)
class SignalementOfficielDetailView(_VueAutorite):
    def delete(self, request, id):
        incident = get_object_or_404(
            self.organisation.signalements_officiels.filter(statut__in=STATUTS_VISIBLES), id=id,
        )
        cellule = incident.cellule_h3_res8
        incident.retirer(motif='Levé par l\'autorité')
        services.journaliser(self.organisation, request.user, 'signalement_officiel.leve', incident)
        temps_reel.publier_retrait(incident.id, cellule, temps_reel.RAISON_RETIRE_AUTEUR)
        return Response(status=status.HTTP_204_NO_CONTENT)
