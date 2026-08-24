from django.urls import path

from . import views

app_name = 'trips'

urlpatterns = [
    path('routes/calculate/', views.CalculItineraireView.as_view(), name='calculer-itineraire'),
    path('trips/', views.TrajetListeCreationView.as_view(), name='trajets'),
    path('trips/<uuid:id>/', views.TrajetDetailView.as_view(), name='trajet-detail'),
    path('trips/<uuid:id>/rate/', views.NoterTrajetView.as_view(), name='trajet-note'),
    path('telemetry/positions/', views.TelemetriePositionsView.as_view(), name='telemetrie-positions'),
]
