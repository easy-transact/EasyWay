from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode


def envoyer_invitation(membre):
    """Lien d'activation du portail agence : meme jeton que la reinitialisation
    de mot de passe (default_token_generator) -- a usage unique (il depend du
    mot de passe, qui change a l'acceptation) et valable PASSWORD_RESET_TIMEOUT
    (72 h par defaut dans Django)."""
    utilisateur = membre.utilisateur
    uid = urlsafe_base64_encode(force_bytes(str(utilisateur.pk)))
    jeton = default_token_generator.make_token(utilisateur)
    lien = f'{settings.PORTAIL_AGENCE_URL}/activer?uid={uid}&token={jeton}'
    send_mail(
        subject=f'Invitation a rejoindre {membre.organisation} sur EasyWay',
        message=(
            f'Bonjour {utilisateur.nom_complet},\n\n'
            f'Vous etes invite a rejoindre {membre.organisation} sur le portail agences EasyWay.\n'
            f'Choisissez votre mot de passe en suivant ce lien (valable 72 h) :\n{lien}\n\n'
            f'Vous vous connecterez ensuite avec votre numero de telephone : {utilisateur.telephone}.'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[utilisateur.email],
    )


def envoyer_ajout_compte_existant(membre):
    """L'utilisateur avait deja un compte EasyWay : pas de mot de passe a
    choisir, simple information."""
    utilisateur = membre.utilisateur
    if not utilisateur.email:
        return
    send_mail(
        subject=f'Vous avez rejoint {membre.organisation} sur EasyWay',
        message=(
            f'Bonjour {utilisateur.nom_complet},\n\n'
            f'Votre compte EasyWay a ete ajoute a {membre.organisation}.\n'
            f'Connectez-vous au portail agences avec vos identifiants habituels :\n'
            f'{settings.PORTAIL_AGENCE_URL}'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[utilisateur.email],
    )
