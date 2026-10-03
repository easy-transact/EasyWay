"""Import en masse des infractions (CSV ou JSON) -- partage par l'endpoint
staff POST /api/staff/infractions/import/ et la commande import_infractions.

Upsert : une ligne met a jour l'infraction de meme `code` si elle en a un,
sinon celle de meme libelle (normalise, sans accents/casse), sinon en cree
une. Les lignes invalides sont rapportees (numero + erreurs) sans bloquer
les autres -- la liste source vient d'un site web, quelques lignes mal
formees ne doivent pas empecher d'importer les 80 autres.
"""

import csv
import io
import json
import re

from django.db import transaction

from places.utils import normaliser

from .models import Infraction
from .serializers import InfractionSerializer

# En-tetes acceptes (normalises : minuscules, sans accents, espaces -> _)
# -> champ de InfractionSerializer. Francais et anglais, pour coller a ce
# qu'un tableur exporte sans avoir a renommer les colonnes.
ALIAS_COLONNES = {
    'code': 'code', 'numero': 'code', 'n': 'code', 'no': 'code', 'article': 'code',
    'label': 'label', 'libelle': 'label', 'infraction': 'label', 'designation': 'label',
    'category': 'category', 'categorie': 'category', 'type': 'category', 'filtre': 'category',
    'fine_amount': 'fine_amount', 'amende': 'fine_amount', 'montant': 'fine_amount',
    'amende_min': 'fine_amount', 'montant_amende': 'fine_amount', 'amende_fcfa': 'fine_amount',
    'fine_amount_max': 'fine_amount_max', 'amende_max': 'fine_amount_max', 'montant_max': 'fine_amount_max',
    'legal_reference': 'legal_reference', 'reference': 'legal_reference',
    'reference_legale': 'legal_reference', 'texte': 'legal_reference',
    'additional_penalties': 'additional_penalties', 'sanctions': 'additional_penalties',
    'sanctions_complementaires': 'additional_penalties',
    'is_active': 'is_active', 'actif': 'is_active',
}
CHAMPS_MONTANT = ('fine_amount', 'fine_amount_max')


class FichierIllisible(Exception):
    pass


def lire_fichier(contenu: bytes, nom: str) -> list[dict]:
    """CSV (separateur , ou ; detecte, UTF-8 avec ou sans BOM) ou JSON (liste
    d'objets, ou {"infractions": [...]})."""
    try:
        texte = contenu.decode('utf-8-sig')
    except UnicodeDecodeError:
        texte = contenu.decode('latin-1')

    if nom.lower().endswith('.json') or texte.lstrip().startswith(('[', '{')):
        try:
            donnees = json.loads(texte)
        except json.JSONDecodeError as exc:
            raise FichierIllisible(f'Invalid JSON: {exc}') from exc
        if isinstance(donnees, dict):
            donnees = donnees.get('infractions', [])
        if not isinstance(donnees, list):
            raise FichierIllisible('JSON must be a list of infractions.')
        return donnees

    try:
        dialecte = csv.Sniffer().sniff(texte[:4096], delimiters=',;\t')
    except csv.Error:
        dialecte = csv.excel
    return list(csv.DictReader(io.StringIO(texte), dialect=dialecte))


def _cle_colonne(nom: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', normaliser(str(nom))).strip('_')


def _montant(valeur):
    """'25 000 FCFA' / '25.000' / 25000 -> 25000 ; '' -> None."""
    if valeur is None or isinstance(valeur, int):
        return valeur
    chiffres = re.sub(r'[^0-9]', '', str(valeur))
    return int(chiffres) if chiffres else None


def normaliser_ligne(ligne: dict) -> dict:
    donnees = {}
    for colonne, valeur in ligne.items():
        champ = ALIAS_COLONNES.get(_cle_colonne(colonne))
        if champ is None:
            continue
        if isinstance(valeur, str):
            valeur = valeur.strip()
        if champ in CHAMPS_MONTANT:
            # Fourchette dans une seule cellule : "5 000 - 25 000".
            if champ == 'fine_amount' and isinstance(valeur, str) and re.search(r'\d\s*[-–à]\s*\d', valeur):
                bas, haut = re.split(r'\s*[-–à]\s*', valeur, maxsplit=1)
                donnees.setdefault('fine_amount_max', _montant(haut))
                valeur = bas
            valeur = _montant(valeur)
        elif champ == 'is_active' and isinstance(valeur, str):
            valeur = normaliser(valeur) not in ('0', 'non', 'no', 'false', 'faux', '')
        if valeur in ('', None) and champ != 'is_active':
            continue
        donnees[champ] = valeur
    return donnees


def _existante(donnees: dict):
    if donnees.get('code'):
        trouvee = Infraction.objects.filter(code=str(donnees['code']).strip()).first()
        if trouvee:
            return trouvee
    if donnees.get('label'):
        return Infraction.objects.filter(libelle_normalise=normaliser(donnees['label'])).first()
    return None


def importer(lignes: list[dict], dry_run: bool = False) -> dict:
    resultat = {'created': 0, 'updated': 0, 'errors': [], 'dry_run': dry_run}
    with transaction.atomic():
        for numero, ligne in enumerate(lignes, start=1):
            if not isinstance(ligne, dict):
                resultat['errors'].append({'row': numero, 'errors': {'non_field_errors': ['Not an object.']}})
                continue
            donnees = normaliser_ligne(ligne)
            existante = _existante(donnees)
            serializer = InfractionSerializer(existante, data=donnees, partial=existante is not None)
            if not serializer.is_valid():
                resultat['errors'].append({'row': numero, 'errors': serializer.errors})
                continue
            serializer.save()
            resultat['updated' if existante else 'created'] += 1
        if dry_run:
            transaction.set_rollback(True)
    return resultat
