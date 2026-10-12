"""
Acces au portail agence (/api/org/) : l'organisation est TOUJOURS celle du
membre connecte (request.membre.organisation), jamais un identifiant envoye
par le client -- c'est ce qui empeche une agence de lire celles des autres.
Chaque vue filtre ses querysets dessus (cf. views.VueOrganisation).
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import RANG_ROLE, StatutOrganisation


class MembreOrganisationActive(BasePermission):
    """Membre actif d'une organisation ACTIVE, avec le role demande par la
    vue : `role_lecture` pour GET/HEAD/OPTIONS, `role_ecriture` sinon (cf.
    VueOrganisation). Pose request.membre pour la vue."""

    message = 'You are not a member of an active organisation.'
    code = 'not_an_organisation_member'

    def has_permission(self, request, view):
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

