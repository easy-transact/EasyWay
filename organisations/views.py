"""
Deux espaces d'API (cf. analyse "Espace Agences EasyWay") :
  /api/staff/organisations/...  back-office EasyWay (is_staff)
  /api/org/...                  portail agence (membre d'une organisation ACTIVE)
"""

from datetime import timedelta

from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.pagination import StaffPagination
from accounts.serializers import MessageSerializer, UtilisateurSerializer
from accounts.views import _decoder_uid, _jetons_pour

from . import services
from .models import (
    Chauffeur,
    MembreOrganisation,
    Organisation,
    RoleOrganisation,
    StatutFiche,
    StatutOrganisation,
    StatutVehicule,
    Vehicule,
)
from .permissions import MembreOrganisationActive
from .serializers import (
    AccepterInvitationSerializer,
    ChauffeurSerializer,
    InvitationSerializer,
    JournalSerializer,
    MembreSerializer,
    OrganisationCreationSerializer,
    OrganisationProfilSerializer,
    OrganisationSerializer,
    VehiculeSerializer,
)


def _paginer(vue, request, queryset, serializer_class, **kwargs):
    paginateur = StaffPagination()
    page = paginateur.paginate_queryset(queryset, request, view=vue)
    return paginateur.get_paginated_response(serializer_class(page, many=True, **kwargs).data)


def _invitation_ou_400(fonction, *args, **kwargs):
    try:
        return fonction(*args, **kwargs), None
    except services.InvitationImpossible as exc:
        return None, Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Back-office EasyWay (staff)
# ---------------------------------------------------------------------------

class OrganisationStaffDetailSerializer(OrganisationSerializer):
    members = MembreSerializer(source='membres', many=True, read_only=True)
    drivers_count = drf_serializers.IntegerField(read_only=True)
    vehicles_count = drf_serializers.IntegerField(read_only=True)

    class Meta(OrganisationSerializer.Meta):
        fields = OrganisationSerializer.Meta.fields + ['members', 'drivers_count', 'vehicles_count']


def _organisations_avec_compteurs():
    return Organisation.objects.annotate(
        drivers_count=Count('chauffeurs', filter=~Q(chauffeurs__statut=StatutFiche.ARCHIVE), distinct=True),
        vehicles_count=Count('vehicules', filter=~Q(vehicules__statut=StatutVehicule.ARCHIVE), distinct=True),
    )


@extend_schema_view(
    get=extend_schema(
        tags=['Staff Organisations'],
        summary='Lister les organisations (agences, flottes)',
        parameters=[
            OpenApiParameter('search', OpenApiTypes.STR, description='Raison sociale, nom commercial, ville, email.'),
            OpenApiParameter('type', OpenApiTypes.STR, enum=['VOYAGE', 'FLOTTE']),
            OpenApiParameter('status', OpenApiTypes.STR, enum=StatutOrganisation.values),
            OpenApiParameter('page', OpenApiTypes.INT),
            OpenApiParameter('page_size', OpenApiTypes.INT),
        ],
        responses={200: OrganisationSerializer(many=True)},
    ),
    post=extend_schema(
        tags=['Staff Organisations'],
        summary='Creer une organisation et inviter son proprietaire',
        description=(
            "Cree l'organisation (EN_ATTENTE) et son proprietaire. Telephone ou email deja "
            "connus : le compte existant est rattache et l'organisation passe ACTIVE tout de "
            "suite. Sinon un compte est cree et recoit par email un lien d'activation (72 h). "
            "waitlist_id : inscription de la liste d'attente convertie (marquee contactee)."
        ),
        request=OrganisationCreationSerializer,
        responses={201: OrganisationStaffDetailSerializer, 400: MessageSerializer},
    ),
)
class OrganisationsStaffView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        organisations = Organisation.objects.order_by('-cree_le')
        recherche = request.query_params.get('search')
        if recherche:
            organisations = organisations.filter(
                Q(raison_sociale__icontains=recherche) | Q(nom_commercial__icontains=recherche)
                | Q(ville__icontains=recherche) | Q(email__icontains=recherche)
            )
        if request.query_params.get('type'):
            organisations = organisations.filter(type=request.query_params['type'])
        if request.query_params.get('status'):
            organisations = organisations.filter(statut=request.query_params['status'])
        return _paginer(self, request, organisations, OrganisationSerializer)

    def post(self, request):
        serializer = OrganisationCreationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = dict(serializer.validated_data)
        proprietaire = donnees.pop('owner')

        with transaction.atomic():
            organisation = Organisation.objects.create(cree_par=request.user, **donnees)
            membre, erreur = _invitation_ou_400(
                services.inviter, organisation, request.user, proprietaire['full_name'],
                proprietaire['phone'], proprietaire['email'], RoleOrganisation.PROPRIETAIRE,
            )
            if erreur is not None:
                transaction.set_rollback(True)
                return erreur
            if organisation.inscription_liste_attente is not None:
                organisation.inscription_liste_attente.contacte = True
                organisation.inscription_liste_attente.save(update_fields=['contacte'])
            services.journaliser(organisation, request.user, 'organisation.creee', organisation)

        organisation = _organisations_avec_compteurs().get(pk=organisation.pk)
        return Response(OrganisationStaffDetailSerializer(organisation).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        tags=['Staff Organisations'], summary="Detail d'une organisation",
        responses={200: OrganisationStaffDetailSerializer},
    ),
    patch=extend_schema(
        tags=['Staff Organisations'], summary='Modifier une organisation (profil, formule)',
        description='Le statut change par suspend/reactivate, pas ici.',
        request=OrganisationSerializer, responses={200: OrganisationStaffDetailSerializer},
    ),
)
class OrganisationStaffDetailView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, id):
        return Response(OrganisationStaffDetailSerializer(get_object_or_404(_organisations_avec_compteurs(), id=id)).data)

    def patch(self, request, id):
        organisation = get_object_or_404(Organisation, id=id)
        serializer = OrganisationSerializer(organisation, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        services.journaliser(organisation, request.user, 'organisation.modifiee', organisation,
                             champs=sorted(request.data.keys()))
        return Response(OrganisationStaffDetailSerializer(_organisations_avec_compteurs().get(pk=id)).data)


@extend_schema(
    tags=['Staff Organisations'], summary='Suspendre une organisation',
    description="Bloque tout /api/org/ pour ses membres, sans rien effacer.",
    request=None, responses={200: OrganisationSerializer},
)
class OrganisationSuspendreView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, id):
        organisation = get_object_or_404(Organisation, id=id)
        organisation.statut = StatutOrganisation.SUSPENDUE
        organisation.save(update_fields=['statut', 'modifie_le'])
        services.journaliser(organisation, request.user, 'organisation.suspendue', organisation)
        return Response(OrganisationSerializer(organisation).data)


@extend_schema(
    tags=['Staff Organisations'], summary='Reactiver une organisation',
    description="ACTIVE si le proprietaire a active son compte, EN_ATTENTE sinon.",
    request=None, responses={200: OrganisationSerializer},
)
class OrganisationReactiverView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, id):
        organisation = get_object_or_404(Organisation, id=id)
        proprietaire_pret = organisation.membres.filter(
            role=RoleOrganisation.PROPRIETAIRE, accepte_le__isnull=False,
        ).exists()
        organisation.statut = StatutOrganisation.ACTIVE if proprietaire_pret else StatutOrganisation.EN_ATTENTE
        organisation.save(update_fields=['statut', 'modifie_le'])
        services.journaliser(organisation, request.user, 'organisation.reactivee', organisation)
        return Response(OrganisationSerializer(organisation).data)


@extend_schema(
    tags=['Staff Organisations'], summary="Renvoyer l'invitation d'un membre",
    request=None, responses={200: MessageSerializer, 400: MessageSerializer},
)
class MembreRenvoyerInvitationStaffView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, id, membre_id):
        membre = get_object_or_404(MembreOrganisation, id=membre_id, organisation_id=id)
        _, erreur = _invitation_ou_400(services.renvoyer_invitation, membre, request.user)
        return erreur or Response({'detail': 'Invitation sent.'})


@extend_schema(
    tags=['Staff Organisations'], summary="Journal d'une organisation",
    parameters=[OpenApiParameter('page', OpenApiTypes.INT), OpenApiParameter('page_size', OpenApiTypes.INT)],
    responses={200: JournalSerializer(many=True)},
)
class JournalStaffView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, id):
        organisation = get_object_or_404(Organisation, id=id)
        journal = organisation.journal.select_related('acteur').order_by('-cree_le')
        return _paginer(self, request, journal, JournalSerializer)


# ---------------------------------------------------------------------------
# Portail agence
# ---------------------------------------------------------------------------

class VueOrganisation(APIView):
    """Base des vues /api/org/ : request.membre est pose par la permission,
    et toute donnee lue ou ecrite est filtree sur request.membre.organisation."""

    permission_classes = [IsAuthenticated, MembreOrganisationActive]
    role_lecture = RoleOrganisation.LECTEUR
    role_ecriture = RoleOrganisation.ADMIN

    @property
    def organisation(self):
        return self.request.membre.organisation


@extend_schema(
    tags=['Organisation'], summary="Mon organisation et mon role",
    description="Appele au chargement du portail : le role decide des menus affiches.",
    responses={200: inline_serializer(name='MonOrganisation', fields={
        'organisation': OrganisationSerializer(), 'member_id': drf_serializers.UUIDField(),
        'role': drf_serializers.ChoiceField(choices=RoleOrganisation.choices),
    })},
)
class MonOrganisationView(VueOrganisation):
    def get(self, request):
        return Response({
            'organisation': OrganisationSerializer(self.organisation).data,
            'member_id': str(request.membre.id) if request.membre.id else None,
            'staff_access': getattr(request.membre, 'est_staff', False),
            'role': request.membre.role,
            'user': {'name': request.user.nom_complet, 'phone': request.user.telephone},
        })


@extend_schema_view(
    get=extend_schema(tags=['Organisation'], summary="Profil de l'agence", responses={200: OrganisationSerializer}),
    patch=extend_schema(
        tags=['Organisation'], summary="Modifier le profil de l'agence (admin)",
        description='Type, raison sociale et formule restent reserves au staff EasyWay.',
        request=OrganisationProfilSerializer, responses={200: OrganisationSerializer},
    ),
)
class ProfilOrganisationView(VueOrganisation):
    def get(self, request):
        return Response(OrganisationSerializer(self.organisation).data)

    def patch(self, request):
        serializer = OrganisationProfilSerializer(self.organisation, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        services.journaliser(self.organisation, request.user, 'organisation.modifiee', self.organisation,
                             champs=sorted(request.data.keys()))
        return Response(OrganisationSerializer(self.organisation).data)


@extend_schema_view(
    get=extend_schema(tags=['Organisation Members'], summary="Membres de l'agence",
                      responses={200: MembreSerializer(many=True)}),
    post=extend_schema(
        tags=['Organisation Members'], summary='Inviter un membre (admin)',
        description=(
            "Telephone = identifiant de connexion, email = destinataire du lien d'activation (72 h). "
            "Compte EasyWay existant : rattache directement, sans nouveau mot de passe."
        ),
        request=InvitationSerializer, responses={201: MembreSerializer, 400: MessageSerializer},
    ),
)
class MembresView(VueOrganisation):
    throttle_scope = 'invitation'

    def get_throttles(self):
        return [ScopedRateThrottle()] if self.request.method == 'POST' else []

    def get(self, request):
        membres = self.organisation.membres.select_related('utilisateur', 'invite_par').order_by('invite_le')
        return Response(MembreSerializer(membres, many=True).data)

    def post(self, request):
        serializer = InvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        membre, erreur = _invitation_ou_400(
            services.inviter, self.organisation, request.user, d['full_name'], d['phone'], d['email'], d['role'],
        )
        return erreur or Response(MembreSerializer(membre).data, status=status.HTTP_201_CREATED)


def _refuser_si_proprietaire(membre):
    if membre.role == RoleOrganisation.PROPRIETAIRE:
        return Response(
            {'detail': 'The owner cannot be changed or removed. Transfer ownership first.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None


@extend_schema_view(
    patch=extend_schema(
        tags=['Organisation Members'], summary="Changer le role ou desactiver un membre (admin)",
        request=MembreSerializer, responses={200: MembreSerializer, 400: MessageSerializer},
    ),
    delete=extend_schema(
        tags=['Organisation Members'], summary="Retirer un membre de l'agence (admin)",
        description="Le compte EasyWay de la personne est conserve ; seul son rattachement est supprime.",
        responses={204: None, 400: MessageSerializer},
    ),
)
class MembreDetailView(VueOrganisation):
    def patch(self, request, id):
        membre = get_object_or_404(self.organisation.membres, id=id)
        refus = _refuser_si_proprietaire(membre)
        if refus:
            return refus
        serializer = MembreSerializer(membre, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        services.journaliser(self.organisation, request.user, 'membre.modifie', membre,
                             role=membre.role, actif=membre.actif)
        return Response(MembreSerializer(membre).data)

    def delete(self, request, id):
        membre = get_object_or_404(self.organisation.membres, id=id)
        refus = _refuser_si_proprietaire(membre)
        if refus:
            return refus
        services.journaliser(self.organisation, request.user, 'membre.retire', membre,
                             telephone=membre.utilisateur.telephone)
        membre.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    tags=['Organisation Members'], summary="Renvoyer l'invitation (admin)",
    request=None, responses={200: MessageSerializer, 400: MessageSerializer},
)
class MembreRenvoyerInvitationView(VueOrganisation):
    def post(self, request, id):
        membre = get_object_or_404(self.organisation.membres, id=id)
        _, erreur = _invitation_ou_400(services.renvoyer_invitation, membre, request.user)
        return erreur or Response({'detail': 'Invitation sent.'})


@extend_schema(
    tags=['Organisation Members'], summary='Transferer la propriete (proprietaire)',
    description="Le membre cible devient proprietaire, l'ancien proprietaire devient admin.",
    request=None, responses={200: MembreSerializer, 400: MessageSerializer},
)
class TransfererProprieteView(VueOrganisation):
    role_ecriture = RoleOrganisation.PROPRIETAIRE

    def post(self, request, id):
        cible = get_object_or_404(self.organisation.membres, id=id)
        if cible.pk == request.membre.pk:
            return Response({'detail': 'You are already the owner.'}, status=status.HTTP_400_BAD_REQUEST)
        if not cible.actif or cible.accepte_le is None:
            return Response(
                {'detail': 'The new owner must be an active member who has activated their account.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            request.membre.role = RoleOrganisation.ADMIN
            request.membre.save(update_fields=['role'])
            cible.role = RoleOrganisation.PROPRIETAIRE
            cible.save(update_fields=['role'])
            services.journaliser(self.organisation, request.user, 'organisation.propriete_transferee', cible)
        return Response(MembreSerializer(cible).data)


@extend_schema(
    tags=['Organisation Members'], summary="Activer son compte depuis le lien d'invitation",
    description=(
        "Public (lien recu par email). Choisit le mot de passe, active le membre ; "
        "l'organisation passe ACTIVE quand c'est le proprietaire. Renvoie les jetons "
        "de connexion comme /api/auth/login/ : le portail connecte directement."
    ),
    request=AccepterInvitationSerializer,
    responses={200: inline_serializer(name='InvitationAcceptee', fields={
        'user': UtilisateurSerializer(), 'tokens': drf_serializers.DictField(),
        'organisation': OrganisationSerializer(),
    }), 400: MessageSerializer},
)
class AccepterInvitationView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'mot-de-passe'

    def post(self, request):
        serializer = AccepterInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data
        utilisateur = _decoder_uid(d['uid'])
        membre = getattr(utilisateur, 'membre_organisation', None) if utilisateur else None
        if (
            membre is None or membre.accepte_le is not None
            or not default_token_generator.check_token(utilisateur, d['token'])
        ):
            return Response({'detail': 'Invalid or expired link.'}, status=status.HTTP_400_BAD_REQUEST)

        membre = services.accepter_invitation(utilisateur, d['new_password'])
        return Response({
            'user': UtilisateurSerializer(utilisateur, context={'request': request}).data,
            'tokens': _jetons_pour(utilisateur),
            'organisation': OrganisationSerializer(membre.organisation).data,
        })


# --- Chauffeurs & vehicules (phase 2) --------------------------------------

def _filtrer_fiches(queryset, request, champs_recherche):
    statut = request.query_params.get('status')
    queryset = queryset.filter(statut=statut) if statut else queryset.exclude(statut='ARCHIVE')
    recherche = request.query_params.get('search')
    if recherche:
        filtre = Q()
        for champ in champs_recherche:
            filtre |= Q(**{f'{champ}__icontains': recherche})
        queryset = queryset.filter(filtre)
    return queryset


PARAMETRES_LISTE = [
    OpenApiParameter('search', OpenApiTypes.STR),
    OpenApiParameter('status', OpenApiTypes.STR, description='Par defaut : tout sauf ARCHIVE.'),
    OpenApiParameter('page', OpenApiTypes.INT),
    OpenApiParameter('page_size', OpenApiTypes.INT),
]


@extend_schema_view(
    get=extend_schema(tags=['Organisation Drivers'], summary='Chauffeurs', parameters=PARAMETRES_LISTE,
                      responses={200: ChauffeurSerializer(many=True)}),
    post=extend_schema(tags=['Organisation Drivers'], summary='Ajouter un chauffeur (gestionnaire)',
                       request=ChauffeurSerializer, responses={201: ChauffeurSerializer}),
)
class ChauffeursView(VueOrganisation):
    role_ecriture = RoleOrganisation.GESTIONNAIRE

    def get(self, request):
        chauffeurs = _filtrer_fiches(
            self.organisation.chauffeurs.select_related('utilisateur').prefetch_related('vehicules').order_by('nom_complet'),
            request, ['nom_complet', 'telephone', 'numero_permis'],
        )
        return _paginer(self, request, chauffeurs, ChauffeurSerializer)

    def post(self, request):
        serializer = ChauffeurSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        chauffeur = serializer.save(organisation=self.organisation)
        services.journaliser(self.organisation, request.user, 'chauffeur.cree', chauffeur)
        return Response(ChauffeurSerializer(chauffeur).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(tags=['Organisation Drivers'], summary='Fiche chauffeur', responses={200: ChauffeurSerializer}),
    patch=extend_schema(tags=['Organisation Drivers'], summary='Modifier un chauffeur (gestionnaire)',
                        request=ChauffeurSerializer, responses={200: ChauffeurSerializer}),
    delete=extend_schema(
        tags=['Organisation Drivers'], summary='Archiver un chauffeur (gestionnaire)',
        description='Archive (statut ARCHIVE) et retire le chauffeur de ses vehicules ; la fiche reste au journal.',
        responses={204: None},
    ),
)
class ChauffeurDetailView(VueOrganisation):
    role_ecriture = RoleOrganisation.GESTIONNAIRE

    def _objet(self, id):
        return get_object_or_404(self.organisation.chauffeurs.exclude(statut=StatutFiche.ARCHIVE), id=id)

    def get(self, request, id):
        return Response(ChauffeurSerializer(self._objet(id)).data)

    def patch(self, request, id):
        chauffeur = self._objet(id)
        serializer = ChauffeurSerializer(chauffeur, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        services.journaliser(self.organisation, request.user, 'chauffeur.modifie', chauffeur,
                             champs=sorted(request.data.keys()))
        return Response(ChauffeurSerializer(chauffeur).data)

    def delete(self, request, id):
        chauffeur = self._objet(id)
        with transaction.atomic():
            chauffeur.vehicules.update(chauffeur=None)
            chauffeur.statut = StatutFiche.ARCHIVE
            chauffeur.save(update_fields=['statut', 'modifie_le'])
            services.journaliser(self.organisation, request.user, 'chauffeur.archive', chauffeur)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema_view(
    get=extend_schema(tags=['Organisation Vehicles'], summary='Vehicules', parameters=PARAMETRES_LISTE,
                      responses={200: VehiculeSerializer(many=True)}),
    post=extend_schema(
        tags=['Organisation Vehicles'], summary='Ajouter un vehicule (gestionnaire)',
        description="plate est normalisee (majuscules, sans espaces ni tirets) et unique dans l'agence.",
        request=VehiculeSerializer, responses={201: VehiculeSerializer},
    ),
)
class VehiculesView(VueOrganisation):
    role_ecriture = RoleOrganisation.GESTIONNAIRE

    def get(self, request):
        vehicules = _filtrer_fiches(
            self.organisation.vehicules.select_related('chauffeur').order_by('immatriculation'),
            request, ['immatriculation', 'marque', 'modele'],
        )
        return _paginer(self, request, vehicules, VehiculeSerializer)

    def post(self, request):
        serializer = VehiculeSerializer(data=request.data, organisation=self.organisation)
        serializer.is_valid(raise_exception=True)
        vehicule = serializer.save(organisation=self.organisation)
        services.journaliser(self.organisation, request.user, 'vehicule.cree', vehicule)
        return Response(VehiculeSerializer(vehicule).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(tags=['Organisation Vehicles'], summary='Fiche vehicule', responses={200: VehiculeSerializer}),
    patch=extend_schema(tags=['Organisation Vehicles'], summary='Modifier un vehicule (gestionnaire)',
                        request=VehiculeSerializer, responses={200: VehiculeSerializer}),
    delete=extend_schema(tags=['Organisation Vehicles'], summary='Archiver un vehicule (gestionnaire)',
                         responses={204: None}),
)
class VehiculeDetailView(VueOrganisation):
    role_ecriture = RoleOrganisation.GESTIONNAIRE

    def _objet(self, id):
        return get_object_or_404(self.organisation.vehicules.exclude(statut=StatutVehicule.ARCHIVE), id=id)

    def get(self, request, id):
        return Response(VehiculeSerializer(self._objet(id)).data)

    def patch(self, request, id):
        vehicule = self._objet(id)
        serializer = VehiculeSerializer(vehicule, data=request.data, partial=True, organisation=self.organisation)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        services.journaliser(self.organisation, request.user, 'vehicule.modifie', vehicule,
                             champs=sorted(request.data.keys()))
        return Response(VehiculeSerializer(vehicule).data)

    def delete(self, request, id):
        vehicule = self._objet(id)
        vehicule.statut = StatutVehicule.ARCHIVE
        vehicule.chauffeur = None
        vehicule.save(update_fields=['statut', 'chauffeur', 'modifie_le'])
        services.journaliser(self.organisation, request.user, 'vehicule.archive', vehicule)
        return Response(status=status.HTTP_204_NO_CONTENT)


ECHEANCES_JOURS_DEFAUT = 30
ECHEANCES_JOURS_MAX = 180


@extend_schema(
    tags=['Organisation'], summary="Tableau de bord de l'agence",
    description=(
        "Compteurs + echeances (permis, assurance, visite technique) deja depassees ou dans "
        f"les `days` prochains jours (defaut {ECHEANCES_JOURS_DEFAUT}, max {ECHEANCES_JOURS_MAX}), "
        "les plus proches d'abord."
    ),
    parameters=[OpenApiParameter('days', OpenApiTypes.INT)],
    responses={200: inline_serializer(name='TableauDeBordOrganisation', fields={
        'members': drf_serializers.IntegerField(),
        'drivers': drf_serializers.DictField(child=drf_serializers.IntegerField()),
        'vehicles': drf_serializers.DictField(child=drf_serializers.IntegerField()),
        'deadlines': inline_serializer(name='Echeance', many=True, fields={
            'kind': drf_serializers.ChoiceField(choices=['LICENSE', 'INSURANCE', 'INSPECTION']),
            'target_type': drf_serializers.ChoiceField(choices=['driver', 'vehicle']),
            'target_id': drf_serializers.UUIDField(),
            'label': drf_serializers.CharField(),
            'expires_on': drf_serializers.DateField(),
            'expired': drf_serializers.BooleanField(),
        }),
    })},
)
class TableauDeBordView(VueOrganisation):
    def get(self, request):
        try:
            jours = min(max(int(request.query_params.get('days', ECHEANCES_JOURS_DEFAUT)), 0), ECHEANCES_JOURS_MAX)
        except ValueError:
            return Response({'detail': "'days' must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
        aujourd_hui = timezone.localdate()
        limite = aujourd_hui + timedelta(days=jours)

        chauffeurs = self.organisation.chauffeurs.exclude(statut=StatutFiche.ARCHIVE)
        vehicules = self.organisation.vehicules.exclude(statut=StatutVehicule.ARCHIVE)

        echeances = []

        def ajouter(kind, target_type, objet, label, date):
            if date is not None and date <= limite:
                echeances.append({
                    'kind': kind, 'target_type': target_type, 'target_id': str(objet.id), 'label': label,
                    'expires_on': date.isoformat(), 'expired': date < aujourd_hui,
                })

        for c in chauffeurs.filter(statut=StatutFiche.ACTIF, permis_expire_le__lte=limite):
            ajouter('LICENSE', 'driver', c, c.nom_complet, c.permis_expire_le)
        for v in vehicules.filter(Q(assurance_expire_le__lte=limite) | Q(visite_technique_expire_le__lte=limite)):
            ajouter('INSURANCE', 'vehicle', v, v.immatriculation, v.assurance_expire_le)
            ajouter('INSPECTION', 'vehicle', v, v.immatriculation, v.visite_technique_expire_le)
        echeances.sort(key=lambda e: e['expires_on'])

        return Response({
            'members': self.organisation.membres.filter(actif=True).count(),
            'drivers': {s: chauffeurs.filter(statut=s).count() for s in (StatutFiche.ACTIF, StatutFiche.INACTIF)},
            'vehicles': {
                s: vehicules.filter(statut=s).count()
                for s in (StatutVehicule.ACTIF, StatutVehicule.MAINTENANCE, StatutVehicule.INACTIF)
            },
            'deadlines': echeances,
            # Suivi de flotte (phase 3) : en route, trajets, exces, signalements.
            'fleet': _resume_flotte(self.organisation),
        })


def _resume_flotte(organisation):
    from .vues_flotte import resume_flotte_tableau_de_bord  # import circulaire vues_flotte -> views

    return resume_flotte_tableau_de_bord(organisation)


@extend_schema(
    tags=['Organisation'], summary="Journal des actions de l'agence (admin)",
    parameters=[OpenApiParameter('page', OpenApiTypes.INT), OpenApiParameter('page_size', OpenApiTypes.INT)],
    responses={200: JournalSerializer(many=True)},
)
class JournalView(VueOrganisation):
    role_lecture = RoleOrganisation.ADMIN

    def get(self, request):
        journal = self.organisation.journal.select_related('acteur').order_by('-cree_le')
        return _paginer(self, request, journal, JournalSerializer)
