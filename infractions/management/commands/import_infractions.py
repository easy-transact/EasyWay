from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from infractions.importation import FichierIllisible, importer, lire_fichier


class Command(BaseCommand):
    help = (
        "Importe/met a jour la liste des infractions routieres depuis un fichier "
        "CSV ou JSON (memes colonnes que POST /api/staff/infractions/import/)."
    )

    def add_arguments(self, parser):
        parser.add_argument('fichier', type=Path)
        parser.add_argument('--dry-run', action='store_true', help='Valide sans rien enregistrer.')

    def handle(self, *args, fichier, dry_run, **options):
        if not fichier.exists():
            raise CommandError(f'{fichier} introuvable.')
        try:
            lignes = lire_fichier(fichier.read_bytes(), fichier.name)
        except FichierIllisible as exc:
            raise CommandError(str(exc)) from exc

        resultat = importer(lignes, dry_run=dry_run)
        for erreur in resultat['errors']:
            self.stderr.write(f"Ligne {erreur['row']} : {erreur['errors']}")
        prefixe = '[dry-run] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(
            f"{prefixe}{resultat['created']} creee(s), {resultat['updated']} mise(s) a jour, "
            f"{len(resultat['errors'])} erreur(s)."
        ))
