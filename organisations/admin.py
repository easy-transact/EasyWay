from django.contrib import admin

from .models import Chauffeur, JournalOrganisation, MembreOrganisation, Organisation, Vehicule


@admin.register(Organisation)
class OrganisationAdmin(admin.ModelAdmin):
    list_display = ('raison_sociale', 'type', 'ville', 'statut', 'formule', 'cree_le')
    list_filter = ('type', 'statut', 'formule')
    search_fields = ('raison_sociale', 'nom_commercial', 'ville', 'email')


@admin.register(MembreOrganisation)
class MembreOrganisationAdmin(admin.ModelAdmin):
    list_display = ('utilisateur', 'organisation', 'role', 'actif', 'accepte_le')
    list_filter = ('role', 'actif')


@admin.register(Chauffeur)
class ChauffeurAdmin(admin.ModelAdmin):
    list_display = ('nom_complet', 'organisation', 'telephone', 'permis_expire_le', 'statut')
    list_filter = ('statut',)
    search_fields = ('nom_complet', 'telephone', 'numero_permis')


@admin.register(Vehicule)
class VehiculeAdmin(admin.ModelAdmin):
    list_display = ('immatriculation', 'organisation', 'type', 'statut', 'chauffeur')
    list_filter = ('type', 'statut')
    search_fields = ('immatriculation', 'marque', 'modele')


@admin.register(JournalOrganisation)
class JournalOrganisationAdmin(admin.ModelAdmin):
    list_display = ('organisation', 'action', 'acteur', 'cree_le')
    list_filter = ('action',)
