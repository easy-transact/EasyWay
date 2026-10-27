"""Valeurs de depart de la spec (section 4), a caler sur les premieres
semaines de donnees : surchargeables depuis le back-office (ReglagesEtatRoutes)."""

import copy

DEFAUTS = {
    # Signalements : sous-type -> poids et demi-vie (jours). Penalite plafonnee.
    'signalements': {
        'NID_DE_POULE': {'poids': 25, 'demi_vie_j': 60},
        'INONDATION': {'poids': 20, 'demi_vie_j': 14},
        'TRAVAUX': {'poids': 10, 'demi_vie_j': 30},
        '1_VOIE_REDUITE': {'poids': 10, 'demi_vie_j': 30},
        'ROUTE_GLISSANTE': {'poids': 10, 'demi_vie_j': 3},
    },
    'plafond_signalements': 50,
    # facteur_votes = 1 + bonus x min(confirmations, max) ; 0 si plus de
    # contestations que de confirmations.
    'bonus_par_confirmation': 0.25,
    'confirmations_max': 4,
    # Zone inondable : signalements INONDATION a au moins N dates differentes.
    'inondation_dates_min': 3,
    # Vitesse : 85e centile aux heures creuses / vitesse attendue.
    'plafond_vitesse': 40,
    'ratio_sans_penalite': 0.8,
    'ratio_penalite_max': 0.4,
    'heure_creuse_debut': 22,
    'heure_creuse_fin': 5,
    'vitesse_attendue_kmh': {'URBAIN': 60, 'NATIONALE': 90, 'AUTOROUTE': 110},
    'passages_min': 20,
    'conducteurs_min_vitesse': 5,
    'fenetre_vitesse_j': 60,
    # Classes.
    'seuil_bon': 70,
    'seuil_degrade': 40,
    'conducteurs_min_alerte': 2,
    'conducteurs_confiance_pleine': 20,
    # Classement des autorites : anonymat.
    'conducteurs_min_classement': 5,
}


def _fusionner(base, surcharge):
    resultat = copy.deepcopy(base)
    for cle, valeur in (surcharge or {}).items():
        if isinstance(valeur, dict) and isinstance(resultat.get(cle), dict):
            resultat[cle] = _fusionner(resultat[cle], valeur)
        else:
            resultat[cle] = valeur
    return resultat


def reglages():
    from ..models import ReglagesEtatRoutes

    ligne = ReglagesEtatRoutes.objects.order_by('id').first()
    return _fusionner(DEFAUTS, ligne.valeurs if ligne else {})
