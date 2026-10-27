"""
Calcul nocturne des notes (spec section 4) :

    note = max(0, 100 - P_signalements - P_vitesse)      (P_chocs : V2)

Regle asymetrique : DEGRADE/MAUVAIS des que des elements le montrent (au
moins conducteurs_min_alerte personnes differentes) ; BON seulement si la
note le dit ET que la vitesse a ete mesuree assez souvent ; sinon INCONNU.
Une route sans donnees n'est jamais "bonne".
"""

import math
from collections import defaultdict
from datetime import timedelta

from django.db.models import Max, Q
from django.utils import timezone

from community.models import Incident, SensVote, StatutIncident, Vote

from ..models import ClasseEtat, HistoriqueNote, NoteTroncon, PassageTroncon, SignalementTroncon, Troncon
from .reglages import reglages
from .troncons import classe_attendue, empreinte_conducteur, rattacher_signalement

RETENTION_SIGNALEMENTS_J = 120  # = community.tasks.RETENTION_INCIDENTS_TERMINES_JOURS
STATUTS_IGNORES = (StatutIncident.RETIRE, StatutIncident.FUSIONNE)  # faux ou compte dans un autre


def _signalements_utiles(reg):
    return Incident.objects.filter(
        sous_type__in=list(reg['signalements']),
        cree_le__gte=timezone.now() - timedelta(days=RETENTION_SIGNALEMENTS_J),
    ).exclude(statut__in=STATUTS_IGNORES)


def rattacher_nouveaux_signalements(reg, limite=500):
    """Recale sur leur tranche les signalements pas encore rattaches."""
    a_faire = _signalements_utiles(reg).filter(troncon_route__isnull=True).order_by('-cree_le')[:limite]
    return sum(1 for incident in a_faire if rattacher_signalement(incident) is not None)


def facteur_votes(confirmations, infirmations, reg):
    if infirmations > confirmations:
        return 0.0
    return 1 + reg['bonus_par_confirmation'] * min(confirmations, reg['confirmations_max'])


def penalite_vitesse(vitesses, conducteurs, classe_route, reg):
    """(penalite, seuil_atteint) a partir des vitesses aux heures creuses."""
    if len(vitesses) < reg['passages_min'] or len(conducteurs) < reg['conducteurs_min_vitesse']:
        return 0.0, False
    vitesses = sorted(vitesses)
    v85 = vitesses[max(math.ceil(0.85 * len(vitesses)) - 1, 0)]
    attendue = reg['vitesse_attendue_kmh'][classe_attendue(classe_route)]
    ratio = v85 / attendue
    haut, bas, plafond = reg['ratio_sans_penalite'], reg['ratio_penalite_max'], reg['plafond_vitesse']
    if ratio >= haut:
        return 0.0, True
    if ratio <= bas:
        return float(plafond), True
    return plafond * (haut - ratio) / (haut - bas), True


def classer(note, temoins, vitesse_mesuree, reg):
    if note < reg['seuil_bon']:
        if len(temoins) < reg['conducteurs_min_alerte']:
            return ClasseEtat.INCONNU
        return ClasseEtat.MAUVAIS if note < reg['seuil_degrade'] else ClasseEtat.DEGRADE
    return ClasseEtat.BON if vitesse_mesuree else ClasseEtat.INCONNU


def noter_troncon(troncon, reg, maintenant):
    from trips.models import FUSEAU_TRAFIC

    # --- Signalements ------------------------------------------------------
    incidents = [
        lien.incident for lien in
        SignalementTroncon.objects.filter(troncon=troncon, incident__in=_signalements_utiles(reg)).select_related('incident')
    ]
    confirmeurs = defaultdict(set)
    for vote in Vote.objects.filter(incident__in=incidents, sens=SensVote.CONFIRMATION).values('incident_id', 'votant_id'):
        confirmeurs[vote['incident_id']].add(empreinte_conducteur(vote['votant_id']))
    par_cause, temoins, confirmes, dates_inondation = {}, set(), 0, set()
    for incident in incidents:
        regle = reg['signalements'][incident.sous_type]
        facteur = facteur_votes(incident.confirmations, incident.infirmations, reg)
        age_j = (maintenant - incident.cree_le).total_seconds() / 86400
        poids = regle['poids'] * facteur * 0.5 ** (age_j / regle['demi_vie_j'])
        if facteur > 0:
            par_cause[incident.sous_type] = par_cause.get(incident.sous_type, 0) + poids
            temoins |= {empreinte_conducteur(incident.auteur_id)} | confirmeurs[incident.id]
            confirmes += incident.confirmations > 0
            if incident.sous_type == 'INONDATION':
                dates_inondation.add(incident.cree_le.astimezone(FUSEAU_TRAFIC).date())
    p_signalements = min(sum(par_cause.values()), reg['plafond_signalements'])

    # --- Vitesse aux heures creuses ---------------------------------------
    fenetre = PassageTroncon.objects.filter(
        troncon=troncon, horodatage__gte=maintenant - timedelta(days=reg['fenetre_vitesse_j']),
    )
    tous = list(fenetre.values_list('conducteur', 'vitesse_kmh', 'heure_creuse'))
    creuses = [(c, v) for c, v, creuse in tous if creuse]
    if troncon.ralentissement_normal:
        p_vitesse, vitesse_mesuree = 0.0, False
    else:
        p_vitesse, vitesse_mesuree = penalite_vitesse(
            [v for _, v in creuses], {c for c, _ in creuses}, troncon.classe_route, reg,
        )
    if p_vitesse > 0:
        temoins |= {c for c, _ in creuses}
        par_cause['VITESSE'] = p_vitesse

    note = max(0, round(100 - p_signalements - p_vitesse))
    conducteurs = {c for c, _, _ in tous} | temoins
    dates = [i.cree_le for i in incidents] + [fenetre.aggregate(m=Max('horodatage'))['m']]
    derniere = max((d for d in dates if d), default=None)
    ancienne = NoteTroncon.objects.filter(troncon=troncon).values_list('inondable', flat=True).first()
    valeurs = {
        'note': note,
        'classe': classer(note, temoins, vitesse_mesuree, reg),
        'confiance': round(min(1.0, len(conducteurs) / reg['conducteurs_confiance_pleine']), 2),
        'causes': [cause for cause, poids in sorted(par_cause.items(), key=lambda c: -c[1]) if poids > 0],
        # Permanent : une route inondable l'est de saison en saison.
        'inondable': bool(ancienne) or len(dates_inondation) >= reg['inondation_dates_min'],
        'penalite_signalements': round(p_signalements, 1),
        'penalite_vitesse': round(p_vitesse, 1),
        'nb_passages': len(tous),
        'nb_conducteurs': len(conducteurs),
        'nb_signalements_confirmes': confirmes,
        'derniere_observation': derniere,
    }
    note_troncon, _ = NoteTroncon.objects.update_or_create(troncon=troncon, defaults=valeurs)
    return note_troncon


def calculer_notes(maintenant=None):
    """Tache nocturne. Retourne {classe: nombre de tronçons}."""
    from trips.models import FUSEAU_TRAFIC

    maintenant = maintenant or timezone.now()
    reg = reglages()
    rattacher_nouveaux_signalements(reg)
    a_noter = Troncon.objects.filter(
        Q(passages__horodatage__gte=maintenant - timedelta(days=reg['fenetre_vitesse_j']))
        | Q(signalements__isnull=False)
        | Q(note__isnull=False)
    ).distinct()
    lundi = (maintenant.astimezone(FUSEAU_TRAFIC) - timedelta(days=maintenant.astimezone(FUSEAU_TRAFIC).weekday())).date()
    bilan = {}
    for troncon in a_noter.iterator():
        note = noter_troncon(troncon, reg, maintenant)
        HistoriqueNote.objects.update_or_create(
            troncon=troncon, semaine=lundi, defaults={'note': note.note, 'classe': note.classe},
        )
        bilan[note.classe] = bilan.get(note.classe, 0) + 1
    return bilan
