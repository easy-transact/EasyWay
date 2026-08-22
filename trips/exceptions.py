class TransitionInvalide(Exception):
    """Transition de statut de Trajet non autorisee par la machine a etats
    (ex. TERMINE -> ACTIF)."""
