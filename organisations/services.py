"""Invitations et journal : partages par l'API staff (back-office EasyWay)
et l'API agence (portail)."""

from django.db import transaction
from django.utils import timezone

from accounts.models import Parametres, Utilisateur

from .emails import envoyer_ajout_compte_existant, envoyer_invitation
from .models import JournalOrganisation, MembreOrganisation, RoleOrganisation, StatutOrganisation


class InvitationImpossible(Exception):
    """Message destine au client (400)."""


def journaliser(organisation, acteur, action, cible=None, **details):
    JournalOrganisation.objects.create(
        organisation=organisation,
        acteur=acteur if acteur is not None and acteur.is_authenticated else None,
        action=action,
        cible_type=type(cible).__name__ if cible is not None else '',
        cible_id=str(cible.pk) if cible is not None else '',
        details=details,
    )


def inviter(organisation, invitant, nom_complet, telephone, email, role):
    """telephone deja normalise (E.164), email deja normalise. Retourne le
    MembreOrganisation cree.

    Telephone ou email deja connus : le compte existant est rattache (pas de
    nouveau mot de passe, accepte tout de suite). Sinon un compte est cree sans
    mot de passe utilisable et recoit le lien d'activation."""
    par_telephone = Utilisateur.objects.filter(telephone=telephone).first()
    par_email = Utilisateur.objects.filter(email__iexact=email).first() if email else None
    if par_telephone and par_email and par_telephone.pk != par_email.pk:
        raise InvitationImpossible('This phone number and this email belong to two different accounts.')
    existant = par_telephone or par_email

    with transaction.atomic():
        if existant is not None:
            if MembreOrganisation.objects.filter(utilisateur=existant).exists():
                raise InvitationImpossible('This user already belongs to an organisation.')
            membre = MembreOrganisation.objects.create(
                organisation=organisation, utilisateur=existant, role=role,
                invite_par=invitant, accepte_le=timezone.now(),
            )
            if role == RoleOrganisation.PROPRIETAIRE:
                activer_si_proprietaire_pret(organisation)
        else:
            utilisateur = Utilisateur.objects.create_user(
                telephone=telephone, password=None, nom_complet=nom_complet, email=email or None,
            )
            Parametres.objects.create(utilisateur=utilisateur)
            membre = MembreOrganisation.objects.create(
                organisation=organisation, utilisateur=utilisateur, role=role, invite_par=invitant,
            )
        journaliser(organisation, invitant, 'membre.invite', membre, role=role, compte_existant=existant is not None)

    if membre.accepte_le is None:
        transaction.on_commit(lambda: envoyer_invitation(membre))
    else:
        transaction.on_commit(lambda: envoyer_ajout_compte_existant(membre))
    return membre


def renvoyer_invitation(membre, acteur):
    if membre.accepte_le is not None:
        raise InvitationImpossible('This member has already activated their account.')
    if not membre.utilisateur.email:
        raise InvitationImpossible('This member has no email address.')
    envoyer_invitation(membre)
    journaliser(membre.organisation, acteur, 'membre.invitation_renvoyee', membre)


def accepter_invitation(utilisateur, mot_de_passe):
    """Appele une fois le jeton verifie (cf. views.AccepterInvitationView)."""
    membre = utilisateur.membre_organisation
    with transaction.atomic():
        utilisateur.set_password(mot_de_passe)
        utilisateur.save(update_fields=['password'])
        membre.accepte_le = timezone.now()
        membre.save(update_fields=['accepte_le'])
        if membre.role == RoleOrganisation.PROPRIETAIRE:
            activer_si_proprietaire_pret(membre.organisation)
        journaliser(membre.organisation, utilisateur, 'membre.invitation_acceptee', membre)
    return membre


def activer_si_proprietaire_pret(organisation):
    """EN_ATTENTE -> ACTIVE des que le proprietaire a un compte utilisable.
    Ne reactive jamais une organisation SUSPENDUE."""
    if organisation.statut == StatutOrganisation.EN_ATTENTE:
        organisation.statut = StatutOrganisation.ACTIVE
        organisation.save(update_fields=['statut', 'modifie_le'])
