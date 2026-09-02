from django.core.management.base import BaseCommand

from accounts.models import Utilisateur
from accounts.utils import NumeroTelephoneInvalide, valider_et_normaliser_telephone


class Command(BaseCommand):
    help = (
        "Normalise en E.164 les numeros de telephone existants (cf. accounts.utils). "
        "Necessaire une seule fois apres l'ajout de la validation stricte a la "
        "connexion/inscription : un compte enregistre avant ce changement, avec un "
        "numero stocke dans un autre format (sans indicatif, avec un 0 initial...), "
        "ne peut plus se connecter des que son numero est retape -- authenticate() "
        "compare une chaine exacte contre la valeur normalisee soumise."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help="N'ecrit rien en base, affiche seulement ce qui changerait.",
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        modifies = deja_ok = invalides = collisions = 0

        utilisateurs = Utilisateur.objects.exclude(telephone__isnull=True).exclude(telephone='')
        for utilisateur in utilisateurs:
            brut = utilisateur.telephone
            try:
                normalise = valider_et_normaliser_telephone(brut)
            except NumeroTelephoneInvalide as exc:
                invalides += 1
                self.stderr.write(f'{utilisateur.id} ({brut!r}) : invalide, laisse tel quel -- {exc}')
                continue

            if normalise == brut:
                deja_ok += 1
                continue

            # Deux numeros ecrits differemment (ex. avec/sans le 0 initial)
            # peuvent normaliser vers la meme forme E.164 -- unique=True sur
            # le champ empecherait le save(), mieux vaut le detecter ici et
            # signaler pour resolution manuelle que planter le backfill.
            if Utilisateur.objects.filter(telephone=normalise).exclude(pk=utilisateur.pk).exists():
                collisions += 1
                self.stderr.write(
                    f'{utilisateur.id} ({brut!r} -> {normalise!r}) : collision avec un '
                    'compte existant deja sur ce numero normalise, laisse tel quel -- '
                    'a resoudre manuellement.'
                )
                continue

            self.stdout.write(f'{utilisateur.id}: {brut!r} -> {normalise!r}')
            if not dry_run:
                utilisateur.telephone = normalise
                utilisateur.save(update_fields=['telephone'])
            modifies += 1

        suffixe = ' (dry-run, rien ecrit)' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f'{modifies} normalises, {deja_ok} deja au bon format, '
            f'{invalides} invalides ignores, {collisions} collisions ignorees.{suffixe}'
        ))
