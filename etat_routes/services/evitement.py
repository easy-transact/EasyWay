"""
Etat des routes V3 : "Eviter les routes degradees" (reglage utilisateur).

La spec proposait d'ecrire une vitesse reduite dans traffic.tar ; ce fichier
porte le trafic EN DIRECT, efface au bout de 15 min et ecrase par les mesures
du moment -- une route defoncee redeviendrait "normale" des qu'un vehicule y
passe vite. A la place, comme pour les signalements (retards_signalements.py) :
on classe les itineraires candidats en ajoutant un temps de penalite aux
metres DEGRADE/MAUVAIS (comme si on y roulait FACTEUR_VITESSE fois moins vite).
Aucune route n'est interdite ; la duree affichee reste la duree reelle.
"""

FACTEUR_VITESSE = {'bad_m': 0.5, 'degraded_m': 0.8}
EGALITE_S = 60


def penalite_s(candidat):
    etat = candidat.get('etat_route')
    if not etat:
        return 0
    duree = candidat.get('duree_avec_trafic') or candidat['duree']
    vitesse = max(candidat['distance'] / max(duree, 1), 1.0)  # m/s
    return round(sum(etat.get(cle, 0) / vitesse * (1 / f - 1) for cle, f in FACTEUR_VITESSE.items()))


def classer(candidats):
    """Reclasse par duree + penalite ; ajoute etat_route.avoid_penalty_s."""
    for candidat in candidats:
        penalite = penalite_s(candidat)
        if candidat.get('etat_route'):
            candidat['etat_route'] = {**candidat['etat_route'], 'avoid_penalty_s': penalite}
        candidat['_cle_evitement'] = (candidat.get('duree_avec_trafic') or candidat['duree']) + penalite
    candidats.sort(key=lambda c: c['_cle_evitement'])
    for index, candidat in enumerate(candidats):
        del candidat['_cle_evitement']
        candidat['est_recommande'] = index == 0
        candidat['libelle'] = 'Itineraire recommande' if index == 0 else f'Itineraire {index + 1}'
    return candidats
