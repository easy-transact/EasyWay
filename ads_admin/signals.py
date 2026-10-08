"""Evenements de l'application qui alimentent la cloche du back-office.
Les actions de moderation elles-memes (retraits a repetition, imports) sont
notifiees depuis leurs vues, cf. community/views.py et infractions/views.py."""

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from accounts.models import InscriptionListeAttente
from community.models import Incident, StatutIncident
from places.models import Lieu

from .models import Impression, NiveauNotification, TypeNotification
from .services import SEUIL_INFIRMATIONS, notifier


@receiver(post_save, sender=Lieu)
def lieu_propose(sender, instance, created, **kwargs):
    if not created or not instance.propose_par_id:
        return
    notifier(
        TypeNotification.LIEU,
        titre='Nouveau lieu proposé',
        texte=f'{instance.nom} · {instance.ville}',
        lien='/places?statut=EN_ATTENTE',
        niveau=NiveauNotification.ATTENTION,
        cle='lieux-proposes',
        titre_groupe=lambda n: f'{n} nouveaux lieux proposés',
    )


@receiver(post_save, sender=InscriptionListeAttente)
def inscription_liste_attente(sender, instance, created, **kwargs):
    if not created:
        return
    notifier(
        TypeNotification.ATTENTE,
        titre="Nouvelle inscription sur la liste d'attente",
        texte=f'{instance.get_profil_display()}' + (f' · {instance.ville}' if instance.ville else ''),
        lien='/liste-attente',
        cle='liste-attente',
        titre_groupe=lambda n: f"{n} nouvelles inscriptions sur la liste d'attente",
    )


@receiver(post_save, sender=Incident)
def incident_conteste(sender, instance, created, update_fields=None, **kwargs):
    # Incident.infirmer() sauvegarde avec update_fields : on ne recalcule qu'a
    # ce moment-la, pas a chaque save() de l'incident.
    if created or not update_fields or 'infirmations' not in update_fields:
        return
    if instance.statut not in (StatutIncident.ACTIF, StatutIncident.EN_ATTENTE):
        return
    if instance.infirmations < SEUIL_INFIRMATIONS or instance.infirmations < instance.confirmations * 1.5:
        return
    lieu = instance.nom_voie or instance.ville or 'position inconnue'
    notifier(
        TypeNotification.INCIDENT,
        titre='Signalement probablement faux',
        texte=f'{instance.get_type_display()} · {lieu} · {instance.infirmations} votes « n’existe pas »',
        lien='/incidents?suspect=1',
        niveau=NiveauNotification.DANGER,
        cle=f'incident-faux-{instance.id}',
        une_seule_fois=True,
    )


@receiver(post_save, sender=Impression)
def plafond_campagne(sender, instance, created, **kwargs):
    if not created:
        return
    campagne = instance.campagne
    if not campagne.plafond_atteint():
        return
    notifier(
        TypeNotification.PUB,
        titre='Plafond journalier atteint',
        # plafond_atteint() compte tous les evenements de la journee, clics compris.
        texte=f'Campagne « {campagne.nom} » : {campagne.plafond_journalier} impressions',
        lien='/publicites',
        niveau=NiveauNotification.ATTENTION,
        cle=f'plafond-{campagne.id}-{timezone.localdate().isoformat()}',
        une_seule_fois=True,
    )
