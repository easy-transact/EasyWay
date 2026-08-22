from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Appareil, Droits, Parametres, Utilisateur


@admin.register(Utilisateur)
class UtilisateurAdmin(UserAdmin):
    ordering = ['email']
    list_display = ['email', 'nom_complet', 'formule', 'is_staff', 'is_active', 'est_banni']
    search_fields = ['email', 'nom_complet']
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Profil', (
            {'fields': ('nom_complet', 'telephone', 'ville', 'type_vehicule', 'url_avatar', 'avatar')}
        )),
        ('Etat', {'fields': (
            'email_verifie', 'mode_invisible', 'formule', 'formule_expire_le',
            'score_reputation', 'points', 'est_banni', 'banni_jusqu_a', 'suppression_demandee_le',
        )}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
    )
    add_fieldsets = (
        (None, {'classes': ('wide',), 'fields': ('email', 'nom_complet', 'password1', 'password2')}),
    )


@admin.register(Droits)
class DroitsAdmin(admin.ModelAdmin):
    list_display = [
        'formule', 'max_adresses_enregistrees', 'publicite_active',
        'routage_avance', 'packs_hors_ligne', 'retention_historique_jours',
    ]


admin.site.register(Parametres)
admin.site.register(Appareil)
