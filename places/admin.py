from django.contrib import admin

from .models import AdresseEnregistree, Lieu, RechercheRecente


@admin.action(description='Approuver les lieux selectionnes')
def approuver(modeladmin, request, queryset):
    for lieu in queryset:
        lieu.approuver()


@admin.action(description='Rejeter les lieux selectionnes')
def rejeter(modeladmin, request, queryset):
    for lieu in queryset:
        lieu.rejeter(motif='Rejete en masse depuis l\'admin')


@admin.register(Lieu)
class LieuAdmin(admin.ModelAdmin):
    list_display = ['nom', 'categorie', 'ville', 'statut', 'source', 'score_popularite']
    list_filter = ['statut', 'source', 'categorie', 'ville']
    search_fields = ['nom', 'nom_normalise', 'adresse']
    actions = [approuver, rejeter]


admin.site.register(AdresseEnregistree)
admin.site.register(RechercheRecente)
