from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from accounts.tests import connecter, creer_utilisateur

from .models import Infraction

CSV_GENDARMERIE = (
    "N°;Libellé;Catégorie;Amende;Sanctions\n"
    "1;Défaut de triangle de pré-signalisation;Équipement;25 000 FCFA;\n"
    "2;Excès de vitesse;Vitesse;5 000 - 25 000;Retrait de permis\n"
    "3;;Vitesse;1000;\n"
).encode('utf-8-sig')


def creer_infraction(**extra):
    valeurs = {'libelle': 'Defaut de triangle', 'categorie': 'Equipement', 'amende_fcfa': 25000, **extra}
    return Infraction.objects.create(**valeurs)


class InfractionPubliqueTests(TestCase):
    def test_liste_publique_masque_les_inactives_et_filtre(self):
        creer_infraction()
        creer_infraction(libelle='Excès de vitesse', categorie='Vitesse', amende_fcfa=5000)
        creer_infraction(libelle='Ancienne', actif=False)

        reponse = self.client.get(reverse('infractions:infractions'))
        self.assertEqual(reponse.status_code, 200)
        self.assertEqual(len(reponse.json()), 2)

        libelles = [i['label'] for i in self.client.get(
            reverse('infractions:infractions'), {'search': 'exces'}
        ).json()]
        self.assertEqual(libelles, ['Excès de vitesse'])

        par_categorie = self.client.get(reverse('infractions:infractions'), {'category': 'vitesse'}).json()
        self.assertEqual(len(par_categorie), 1)
        self.assertEqual(len(self.client.get(reverse('infractions:infractions'), {'min_fine': 10000}).json()), 1)

    def test_categories_avec_nombre(self):
        creer_infraction()
        creer_infraction(libelle='Excès de vitesse', categorie='Vitesse', amende_fcfa=5000)
        creer_infraction(libelle='Vitesse 2', categorie='Vitesse', amende_fcfa=5000)
        reponse = self.client.get(reverse('infractions:categories')).json()
        self.assertEqual(reponse, [{'category': 'Equipement', 'count': 1}, {'category': 'Vitesse', 'count': 2}])


class InfractionStaffTests(TestCase):
    def setUp(self):
        cache.clear()
        staff = creer_utilisateur(email='staff@easyway.local', is_staff=True)
        self.auth = connecter(self.client, staff.telephone)

    def test_reserve_au_staff(self):
        normal = creer_utilisateur(email='normal@easyway.local')
        reponse = self.client.get(reverse('infractions:staff-infractions'), **connecter(self.client, normal.telephone))
        self.assertEqual(reponse.status_code, 403)

    def test_crud(self):
        reponse = self.client.post(
            reverse('infractions:staff-infractions'),
            {'label': 'Défaut de triangle', 'category': 'Équipement', 'fine_amount': 25000},
            content_type='application/json', **self.auth,
        )
        self.assertEqual(reponse.status_code, 201)
        identifiant = reponse.json()['id']

        doublon = self.client.post(
            reverse('infractions:staff-infractions'),
            {'label': 'defaut de TRIANGLE', 'fine_amount': 1}, content_type='application/json', **self.auth,
        )
        self.assertEqual(doublon.status_code, 400)

        url = reverse('infractions:staff-infraction-detail', args=[identifiant])
        reponse = self.client.patch(url, {'is_active': False}, content_type='application/json', **self.auth)
        self.assertFalse(reponse.json()['is_active'])
        self.assertEqual(self.client.get(reverse('infractions:infractions')).json(), [])

        self.assertEqual(self.client.delete(url, **self.auth).status_code, 204)
        self.assertFalse(Infraction.objects.exists())

    def test_amende_max_inferieure_au_min_rejetee(self):
        reponse = self.client.post(
            reverse('infractions:staff-infractions'),
            {'label': 'X', 'fine_amount': 5000, 'fine_amount_max': 1000},
            content_type='application/json', **self.auth,
        )
        self.assertEqual(reponse.status_code, 400)
        self.assertIn('fine_amount_max', reponse.json())

    def test_import_csv_puis_reimport_met_a_jour(self):
        url = reverse('infractions:staff-infractions-import')
        fichier = SimpleUploadedFile('infractions.csv', CSV_GENDARMERIE, content_type='text/csv')
        resultat = self.client.post(url, {'file': fichier}, **self.auth).json()
        self.assertEqual((resultat['created'], resultat['updated']), (2, 0))
        self.assertEqual(resultat['errors'][0]['row'], 3)  # libelle manquant

        vitesse = Infraction.objects.get(code='2')
        self.assertEqual((vitesse.amende_fcfa, vitesse.amende_max_fcfa), (5000, 25000))
        self.assertEqual(vitesse.sanctions_complementaires, 'Retrait de permis')

        resultat = self.client.post(
            url, {'infractions': [{'code': '2', 'amende': '6 000'}]}, content_type='application/json', **self.auth,
        ).json()
        self.assertEqual((resultat['created'], resultat['updated']), (0, 1))
        vitesse.refresh_from_db()
        self.assertEqual(vitesse.amende_fcfa, 6000)

    def test_import_dry_run_n_enregistre_rien(self):
        fichier = SimpleUploadedFile('infractions.csv', CSV_GENDARMERIE, content_type='text/csv')
        resultat = self.client.post(
            reverse('infractions:staff-infractions-import'), {'file': fichier, 'dry_run': True}, **self.auth,
        ).json()
        self.assertEqual(resultat['created'], 2)
        self.assertFalse(Infraction.objects.exists())
