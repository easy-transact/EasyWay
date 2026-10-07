from django.urls import path

from . import views

app_name = 'organisations'

urlpatterns = [
    # Portail agence
    path('org/me/', views.MonOrganisationView.as_view(), name='moi'),
    path('org/profile/', views.ProfilOrganisationView.as_view(), name='profil'),
    path('org/dashboard/', views.TableauDeBordView.as_view(), name='tableau-de-bord'),
    path('org/journal/', views.JournalView.as_view(), name='journal'),
    path('org/members/', views.MembresView.as_view(), name='membres'),
    path('org/members/<uuid:id>/', views.MembreDetailView.as_view(), name='membre-detail'),
    path(
        'org/members/<uuid:id>/resend-invitation/',
        views.MembreRenvoyerInvitationView.as_view(),
        name='membre-renvoyer-invitation',
    ),
    path('org/members/<uuid:id>/make-owner/', views.TransfererProprieteView.as_view(), name='transferer-propriete'),
    path('org/invitations/accept/', views.AccepterInvitationView.as_view(), name='accepter-invitation'),
    path('org/drivers/', views.ChauffeursView.as_view(), name='chauffeurs'),
    path('org/drivers/<uuid:id>/', views.ChauffeurDetailView.as_view(), name='chauffeur-detail'),
    path('org/vehicles/', views.VehiculesView.as_view(), name='vehicules'),
    path('org/vehicles/<uuid:id>/', views.VehiculeDetailView.as_view(), name='vehicule-detail'),

    # Back-office EasyWay (staff)
    path('staff/organisations/', views.OrganisationsStaffView.as_view(), name='staff-organisations'),
    path('staff/organisations/<uuid:id>/', views.OrganisationStaffDetailView.as_view(), name='staff-organisation'),
    path(
        'staff/organisations/<uuid:id>/suspend/',
        views.OrganisationSuspendreView.as_view(),
        name='staff-organisation-suspendre',
    ),
    path(
        'staff/organisations/<uuid:id>/reactivate/',
        views.OrganisationReactiverView.as_view(),
        name='staff-organisation-reactiver',
    ),
    path(
        'staff/organisations/<uuid:id>/members/<uuid:membre_id>/resend-invitation/',
        views.MembreRenvoyerInvitationStaffView.as_view(),
        name='staff-membre-renvoyer-invitation',
    ),
    path('staff/organisations/<uuid:id>/journal/', views.JournalStaffView.as_view(), name='staff-organisation-journal'),
]
