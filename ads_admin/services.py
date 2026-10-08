"""Outils partages du back-office : notifications de l'equipe staff et journal
d'audit. Appeles depuis les vues de moderation des autres apps et depuis
signals.py -- jamais depuis l'application mobile."""

import uuid

from django.db.models import F, Q
from django.utils import timezone

from .models import EntreeAudit, NiveauNotification, NotificationStaff

# Fenetre de regroupement : un deuxieme lieu propose dans ce delai met a jour
# la notification existante ("2 nouveaux lieux proposes") au lieu d'en creer
# une nouvelle -- tant que personne ne l'a lue.
FENETRE_REGROUPEMENT = timezone.timedelta(minutes=15)

# Incident "probablement faux" : au moins SEUIL_INFIRMATIONS votes "n'existe
# pas", representant au moins 60 % des votes (infirmations >= 1,5 x
# confirmations). Valeurs pas mesurees sur donnees reelles.
SEUIL_INFIRMATIONS = 3

# Comportement suspect : au moins SEUIL_RETRAITS_ABUS signalements du meme
# auteur retires par la moderation sur FENETRE_ABUS.
SEUIL_RETRAITS_ABUS = 3
FENETRE_ABUS = timezone.timedelta(days=7)


def filtre_incidents_suspects():
    return Q(infirmations__gte=SEUIL_INFIRMATIONS) & Q(infirmations__gte=F('confirmations') * 1.5)


def notifier(type, titre, texte='', lien='', niveau=NiveauNotification.INFO, cle=None, titre_groupe=None, une_seule_fois=False):
    """Cree une notification staff, ou met a jour celle de meme `cle` si elle
    est recente et encore non lue (titre_groupe(n) donne alors le titre pour n
    evenements). une_seule_fois : ne rien faire si une notification de cette
    cle existe deja, lue ou non (ex. un incident signale comme faux)."""
    maintenant = timezone.now()
    if cle:
        existantes = NotificationStaff.objects.filter(cle_regroupement=cle)
        if une_seule_fois and existantes.exists():
            return None
        recente = (
            existantes.filter(mis_a_jour_le__gte=maintenant - FENETRE_REGROUPEMENT, lue_par__isnull=True)
            .order_by('-mis_a_jour_le')
            .first()
        )
        if recente and titre_groupe:
            recente.compteur += 1
            recente.titre = titre_groupe(recente.compteur)
            recente.texte = texte
            recente.mis_a_jour_le = maintenant
            recente.save(update_fields=['compteur', 'titre', 'texte', 'mis_a_jour_le'])
            return recente
    return NotificationStaff.objects.create(
        type=type, niveau=niveau, titre=titre, texte=texte, lien=lien,
        cle_regroupement=cle, mis_a_jour_le=maintenant,
    )


def journaliser(acteur, action, cible=None, type_cible=None, libelle=None, avant=None, apres=None):
    """Ajoute une ligne au journal d'audit. `action` suit la forme
    "<domaine>.<verbe>" (ex. "places.approve"). Le libelle de la cible est
    copie dans valeur_nouvelle : il reste lisible meme apres suppression."""
    nouvelle = dict(apres or {})
    nouvelle['libelle'] = libelle if libelle is not None else (str(cible) if cible is not None else '')
    return EntreeAudit.objects.create(
        acteur=acteur if getattr(acteur, 'is_authenticated', False) else None,
        action=action,
        type_cible=type_cible or (cible._meta.model_name if cible is not None else 'systeme'),
        identifiant_cible=getattr(cible, 'pk', None) or uuid.uuid4(),
        valeur_precedente=avant,
        valeur_nouvelle=nouvelle,
    )
