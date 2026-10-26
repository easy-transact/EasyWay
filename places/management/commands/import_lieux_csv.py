"""
Importe les Lieu depuis un CSV (ex. lieux_cameroun.csv, separateur ";") :
    nom;categorie;ville;latitude;longitude;sources

Avec --remplacer, supprime d'abord les lieux importes (source OPENSTREETMAP)
et garde ceux proposes par les utilisateurs ou ajoutes depuis le back-office ;
--tout supprime aussi ces derniers. Suppression et import dans une seule
transaction : en cas d'erreur, la table reste telle qu'avant.

Ville vide : laissee vide, a completer ensuite avec backfill_ville_quartier.

Usage :
    python manage.py import_lieux_csv lieux_cameroun.csv --dry-run
    python manage.py import_lieux_csv lieux_cameroun.csv --remplacer
"""

import csv

from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from places.models import Lieu, SourceLieu, StatutLieu
from places.utils import normaliser

# Emprise large du Cameroun : une position hors de ce cadre est une erreur
# de saisie (latitude/longitude inversees, zero...).
LAT_MIN, LAT_MAX = 1.6, 13.1
LON_MIN, LON_MAX = 8.4, 16.3

TAILLE_LOT = 2000
COLONNES = {'nom', 'categorie', 'ville', 'latitude', 'longitude'}


class Command(BaseCommand):
    help = 'Importe les lieux depuis un CSV (nom;categorie;ville;latitude;longitude;sources).'

    def add_arguments(self, parser):
        parser.add_argument('fichier')
        parser.add_argument(
            '--remplacer', action='store_true',
            help='Supprime d\'abord les lieux importes (OPENSTREETMAP), garde les ajouts utilisateurs/back-office.',
        )
        parser.add_argument('--tout', action='store_true', help='Avec --remplacer : supprime TOUS les lieux.')
        parser.add_argument('--dry-run', action='store_true', help='Lit et valide le fichier sans rien ecrire.')

    def handle(self, *args, **options):
        if options['tout'] and not options['remplacer']:
            raise CommandError('--tout ne va pas sans --remplacer.')

        lieux, ignores = self._lire(options['fichier'])
        self.stdout.write(f'{len(lieux)} lieu(x) valides, {ignores} ligne(s) ignoree(s).')

        a_supprimer = Lieu.objects.none()
        if options['remplacer']:
            a_supprimer = Lieu.objects.all() if options['tout'] else Lieu.objects.filter(source=SourceLieu.OPENSTREETMAP)
        nb_supprimes = a_supprimer.count()
        self.stdout.write(f'{nb_supprimes} lieu(x) existant(s) a supprimer, {Lieu.objects.count() - nb_supprimes} garde(s).')

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('Dry-run : rien n\'a ete ecrit.'))
            return

        with transaction.atomic():
            if nb_supprimes:
                # Les lieux fusionnes pointent vers d'autres lieux : on casse ces
                # liens avant la suppression (SET_NULL lieu par lieu sinon).
                Lieu.objects.filter(fusionne_dans__in=a_supprimer).update(fusionne_dans=None)
                a_supprimer.delete()
            for debut in range(0, len(lieux), TAILLE_LOT):
                Lieu.objects.bulk_create(lieux[debut:debut + TAILLE_LOT])
                self.stdout.write(f'  {min(debut + TAILLE_LOT, len(lieux))}/{len(lieux)}')
        self.stdout.write(self.style.SUCCESS(f'{len(lieux)} lieu(x) importe(s), {nb_supprimes} supprime(s).'))

    def _lire(self, chemin):
        try:
            fichier = open(chemin, encoding='utf-8-sig', newline='')
        except OSError as exc:
            raise CommandError(f'Lecture impossible : {exc}') from exc

        lieux, ignores, vus = [], 0, set()
        with fichier:
            lecteur = csv.DictReader(fichier, delimiter=';')
            manquantes = COLONNES - set(lecteur.fieldnames or [])
            if manquantes:
                raise CommandError(f'Colonnes manquantes : {", ".join(sorted(manquantes))}')
            for ligne in lecteur:
                nom = (ligne['nom'] or '').strip()
                try:
                    lat, lon = float(ligne['latitude']), float(ligne['longitude'])
                except (TypeError, ValueError):
                    ignores += 1
                    continue
                if not nom or not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
                    ignores += 1
                    continue
                # Doublon exact (meme nom, meme position a ~10 m pres) : un seul.
                cle = (normaliser(nom), round(lat, 4), round(lon, 4))
                if cle in vus:
                    ignores += 1
                    continue
                vus.add(cle)
                lieux.append(Lieu(
                    nom=nom[:255],
                    nom_normalise=normaliser(nom)[:255],
                    categorie=(ligne['categorie'] or 'Autre').strip()[:100],
                    ville=(ligne['ville'] or '').strip()[:255],
                    position=Point(lon, lat, srid=4326),
                    source=SourceLieu.OPENSTREETMAP,
                    statut=StatutLieu.APPROUVE,
                ))
        return lieux, ignores
