from django.urls import path

from . import views

app_name = 'community'

urlpatterns = [
    path('incidents/proches/', views.IncidentsProchesView.as_view(), name='incidents-proches'),
    path('incidents/', views.IncidentCreationView.as_view(), name='incidents'),
    path('incidents/<uuid:id>/', views.IncidentDetailView.as_view(), name='incident-detail'),
    path('incidents/<uuid:id>/vote/', views.VoterIncidentView.as_view(), name='incident-vote'),
    path('utilisateurs/moi/signalements/', views.MesSignalementsView.as_view(), name='mes-signalements'),
]
