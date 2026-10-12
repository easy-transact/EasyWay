"""
Acces au portail agence (/api/org/) : l'organisation est TOUJOURS celle du
membre connecte (request.membre.organisation), jamais un identifiant envoye
par le client -- c'est ce qui empeche une agence de lire celles des autres.
Chaque vue filtre ses querysets dessus (cf. views.VueOrganisation).
"""

import uuid

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import RANG_ROLE, Organisation, RoleOrganisation, StatutOrganisation

EN_TETE_ORGANISATION = 'X-EW-Organisation'


def _uuid_ou_none(valeur):
    try:
        return uuid.UUID(str(valeur))
    except ValueError:
        return None


class AccesStaff:
    """Remplace MembreOrganisation dans request.membre quand un membre du staff
    EasyWay agit sur une agence (cf. MembreOrganisationActive)."""

    id = None
    actif = True
    role = RoleOrganisation.ADMIN
    est_staff = True

    def __init__(self, organisation, utilisateur):
        self.organisation = organisation
        self.organisation_id = organisation.id
        self.utilisateur = utilisateur

    @property
    def rang(self):
        return RANG_ROLE[self.role]


class MembreOrganisationActive(BasePermission):
    """Membre actif d'une organisation ACTIVE, avec le role demande par la
    vue : `role_lecture` pour GET/HEAD/OPTIONS, `role_ecriture` sinon (cf.
    VueOrganisation). Pose request.membre pour la vue."""

    message = 'You are not a member of an active organisation.'
    code = 'not_an_organisation_member'

    def has_permission(self, request, view):
        # Staff EasyWay (back-office) qui consulte une agence : en-tete
        # X-EW-Organisation. Droits d'un ADMIN (pas ceux du proprietaire), y
        # compris sur une agence suspendue ou en attente ; toute action reste
        # journalisee a son nom.
        organisation_id = request.headers.get(EN_TETE_ORGANISATION)
        if organisation_id and request.user.is_authenticated and request.user.is_staff:
            organisation = Organisation.objects.filter(pk=_uuid_ou_none(organisation_id)).first()
            if organisation is None:
                self.message = 'Unknown organisation.'
                self.code = 'unknown_organisation'
                return False
            role_requis = view.role_lecture if request.method in SAFE_METHODS else view.role_ecriture
            if RANG_ROLE[RoleOrganisation.ADMIN] < RANG_ROLE[role_requis]:
                self.message = f'This action requires the {role_requis} role or higher.'
                self.code = 'insufficient_role'
                return False
            request.membre = AccesStaff(organisation, request.user)
            return True

        membre = getattr(request.user, 'membre_organisation', None)
        if membre is None or not membre.actif:
            return False
        if membre.organisation.statut == StatutOrganisation.SUSPENDUE:
            self.message = 'This organisation is suspended. Contact EasyWay.'
            self.code = 'organisation_suspended'
            return False
        if membre.organisation.statut != StatutOrganisation.ACTIVE:
            self.message = 'This organisation is not active yet.'
            self.code = 'organisation_not_active'
            return False

        role_requis = view.role_lecture if request.method in SAFE_METHODS else view.role_ecriture
        if membre.rang < RANG_ROLE[role_requis]:
            self.message = f'This action requires the {role_requis} role or higher.'
            self.code = 'insufficient_role'
            return False

        request.membre = membre
        return True

