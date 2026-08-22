# Donnees servies par GET /api/config/. Volontairement statiques pour l'instant
# (pas de table Ville en base) : a faire evoluer vers un modele si la liste de
# villes ou la version minimale doivent changer sans redeploiement du code.

VILLES_DISPONIBLES = [
    'Douala', 'Yaounde', 'Bafoussam', 'Bamenda', 'Garoua',
    'Maroua', 'Ngaoundere', 'Bertoua', 'Ebolowa', 'Buea',
]

VERSION_MINIMALE_APP = {
    'ios': '1.0.0',
    'android': '1.0.0',
}
