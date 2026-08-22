import unicodedata


def normaliser(texte: str) -> str:
    """Minuscules, sans accents : base commune pour nom_normalise et pour la
    requete de recherche, pour que le trigram matche independamment des accents."""
    sans_accents = unicodedata.normalize('NFKD', texte).encode('ascii', 'ignore').decode('ascii')
    return sans_accents.lower().strip()
