from django.contrib import admin

from .models import Infraction


@admin.register(Infraction)
class InfractionAdmin(admin.ModelAdmin):
    list_display = ['code', 'libelle', 'categorie', 'amende_fcfa', 'amende_max_fcfa', 'actif']
    list_filter = ['actif', 'categorie']
    search_fields = ['code', 'libelle', 'reference_legale']
