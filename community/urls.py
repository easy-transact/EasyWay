from django.urls import path

from . import views

app_name = 'community'

urlpatterns = [
    path('incidents/nearby/', views.IncidentsProchesView.as_view(), name='incidents-proches'),
    path('incidents/along-route/', views.IncidentsSurTrajetView.as_view(), name='incidents-sur-trajet'),
    path('incidents/city/', views.IncidentsParVilleView.as_view(), name='incidents-par-ville'),
    path('incidents/', views.IncidentCreationView.as_view(), name='incidents'),
    path('incidents/<uuid:id>/', views.IncidentDetailView.as_view(), name='incident-detail'),
    path('incidents/<uuid:id>/vote/', views.VoterIncidentView.as_view(), name='incident-vote'),
    path('users/me/reports/', views.MesSignalementsView.as_view(), name='mes-signalements'),
]
