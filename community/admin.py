from django.contrib import admin

from .models import Incident, Vote


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ['type', 'nom_voie', 'ville', 'statut', 'confirmations', 'infirmations', 'expire_le']
    list_filter = ['type', 'statut']
    search_fields = ['nom_voie', 'ville', 'auteur__email']


admin.site.register(Vote)
