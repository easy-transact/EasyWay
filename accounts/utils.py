import phonenumbers

# Region par defaut pour parser un numero saisi sans indicatif pays (ex.
# "677123456") -- EasyWay ne sert que des villes camerounaises (cf.
# config_data.VILLES_DISPONIBLES). Un numero saisi avec un "+" explicite
# (ex. "+33...") est parse selon son propre indicatif, cette region ne sert
# alors que de repli.
REGION_PAR_DEFAUT = 'CM'


class NumeroTelephoneInvalide(Exception):
    """Numero absent, illisible, ou syntaxiquement invalide pour sa region
    (cf. phonenumbers.is_valid_number -- verifie la longueur/le prefixe
    reels de l'operateur, pas juste la forme)."""


def valider_et_normaliser_telephone(numero: str) -> str:
    """Leve NumeroTelephoneInvalide si `numero` n'est pas un numero de
    telephone valide ; sinon retourne sa forme E.164 (ex. '+237677123456').
    Deux numeros ecrits differemment (avec/sans indicatif, espaces, 0 initial)
    mais identiques une fois normalises doivent matcher a l'inscription
    (unicite) comme a la connexion -- Utilisateur.telephone stocke uniquement
    cette forme normalisee, jamais la saisie brute."""
    try:
        analyse = phonenumbers.parse(numero, REGION_PAR_DEFAUT)
    except phonenumbers.NumberParseException as exc:
        raise NumeroTelephoneInvalide(str(exc)) from exc

    if not phonenumbers.is_valid_number(analyse):
        raise NumeroTelephoneInvalide(f"'{numero}' is not a valid phone number.")

    return phonenumbers.format_number(analyse, phonenumbers.PhoneNumberFormat.E164)
