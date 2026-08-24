from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .tokens import email_verification_token


def _uid(utilisateur) -> str:
    return urlsafe_base64_encode(force_bytes(str(utilisateur.pk)))


def envoyer_email_verification(utilisateur):
    jeton = email_verification_token.make_token(utilisateur)
    # reverse() plutot qu'un chemin fige en dur : le lien suit automatiquement
    # un futur renommage de route (deja arrive une fois -- verifier-email/*.../
    # a change de /verifier-email/ vers /verify-email/ lors de la traduction
    # de la surface API en anglais).
    chemin = reverse('accounts:verifier-email', kwargs={'uidb64': _uid(utilisateur), 'token': jeton})
    lien = f"{settings.FRONTEND_URL}{chemin}"
    send_mail(
        subject='Verifiez votre adresse email - Easy Way',
        message=(
            f"Bonjour {utilisateur.nom_complet},\n\n"
            f"Confirmez votre adresse email en suivant ce lien :\n{lien}\n\n"
            "Si vous n'etes pas a l'origine de cette demande, ignorez ce message."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[utilisateur.email],
    )


def envoyer_email_reinitialisation(utilisateur, token_generator):
    jeton = token_generator.make_token(utilisateur)
    # uid/token, pas uid/jeton : ce sont les noms de champs que le frontend
    # doit renvoyer tels quels a POST /api/auth/password/confirm/.
    lien = f"{settings.FRONTEND_URL}/reinitialiser-mot-de-passe?uid={_uid(utilisateur)}&token={jeton}"
    send_mail(
        subject='Reinitialisation de votre mot de passe - Easy Way',
        message=(
            f"Bonjour {utilisateur.nom_complet},\n\n"
            f"Reinitialisez votre mot de passe en suivant ce lien :\n{lien}\n\n"
            "Ce lien expire apres un delai limite. Si vous n'etes pas a l'origine "
            "de cette demande, ignorez ce message."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[utilisateur.email],
    )
