from dataclasses import dataclass

from django.conf import settings
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token


class JetonGoogleInvalide(Exception):
    """Leve lorsque le jeton d'identite transmis par l'app ne peut pas etre
    verifie aupres de Google (signature, audience, ou expiration)."""


@dataclass
class IdentiteGoogle:
    identifiant: str
    email: str
    email_verifie: bool
    nom_complet: str
    url_avatar: str | None


def verifier_jeton_google(jeton: str) -> IdentiteGoogle:
    """Equivalent de ClientGoogleOAuth.verifierJeton(jeton) : Identite (Fig. 11).

    Delegue la verification cryptographique a la librairie officielle google-auth
    (recupere les cles publiques de Google, controle signature/audience/emetteur).
    """

    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise JetonGoogleInvalide("GOOGLE_OAUTH_CLIENT_ID n'est pas configure.")

    try:
        charge_utile = id_token.verify_oauth2_token(
            jeton, google_requests.Request(), audience=settings.GOOGLE_OAUTH_CLIENT_ID
        )
    except (ValueError, GoogleAuthError) as exc:
        raise JetonGoogleInvalide(str(exc)) from exc

    if charge_utile.get('iss') not in ('accounts.google.com', 'https://accounts.google.com'):
        raise JetonGoogleInvalide('Emetteur du jeton inattendu.')

    return IdentiteGoogle(
        identifiant=charge_utile['sub'],
        email=charge_utile['email'],
        email_verifie=charge_utile.get('email_verified', False),
        nom_complet=charge_utile.get('name', ''),
        url_avatar=charge_utile.get('picture'),
    )
