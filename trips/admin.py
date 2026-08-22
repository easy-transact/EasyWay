from django.contrib import admin

from .models import EchantillonVitesse, Itineraire, Manoeuvre, Trajet


class ManoeuvreInline(admin.TabularInline):
    model = Manoeuvre
    extra = 0


class ItineraireInline(admin.TabularInline):
    model = Itineraire
    extra = 0
    show_change_link = True


@admin.register(Trajet)
class TrajetAdmin(admin.ModelAdmin):
    list_display = ['libelle_origine', 'libelle_destination', 'utilisateur', 'statut', 'demarre_le']
    list_filter = ['statut']
    search_fields = ['libelle_origine', 'libelle_destination', 'utilisateur__email']
    inlines = [ItineraireInline]


@admin.register(Itineraire)
class ItineraireAdmin(admin.ModelAdmin):
    list_display = ['libelle', 'trajet', 'est_recommande', 'distance', 'duree']
    inlines = [ManoeuvreInline]


admin.site.register(EchantillonVitesse)
