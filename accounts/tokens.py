from django.contrib.auth.tokens import PasswordResetTokenGenerator


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    """Jeton a usage unique pour 'Verifier son adresse email' (UC-01, «include»).

    Distinct du generateur de reinitialisation de mot de passe : le hash inclut
    `email_verifie` plutot que le mot de passe, si bien qu'un jeton emis reste
    valable meme si l'utilisateur change son mot de passe entre-temps, mais
    devient automatiquement invalide une fois l'adresse verifiee.
    """

    def _make_hash_value(self, user, timestamp):
        return f"{user.pk}{user.email}{user.email_verifie}{timestamp}"


email_verification_token = EmailVerificationTokenGenerator()
