"""Liens automatiques du suivi de flotte (cf. flotte.py)."""

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from community.models import Incident

from . import flotte
from .models import Chauffeur


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def compte_cree(sender, instance, created, **kwargs):
    # Inscription d'un chauffeur deja saisi par son agence : la fiche est reliee.
    if created:
        flotte.lier_chauffeurs_au_compte(instance)


@receiver(post_save, sender=Chauffeur)
def fiche_enregistree(sender, instance, update_fields=None, **kwargs):
    # Le lien lui-meme est enregistre avec update_fields=['utilisateur', ...] :
    # ne pas reboucler dessus.
    if update_fields and 'utilisateur' in update_fields:
        return
    flotte.lier_compte_a_la_fiche(instance)


@receiver(post_save, sender=Incident)
def signalement_cree(sender, instance, created, **kwargs):
    if created:
        flotte.rattacher_signalement(instance)
