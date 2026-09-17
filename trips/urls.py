from django.urls import path

from . import views

app_name = 'trips'

urlpatterns = [
    path('routes/calculate/', views.CalculItineraireView.as_view(), name='calculer-itineraire'),
    path('trips/', views.TrajetListeCreationView.as_view(), name='trajets'),
    path('trips/<uuid:id>/', views.TrajetDetailView.as_view(), name='trajet-detail'),
    path('trips/<uuid:id>/rate/', views.NoterTrajetView.as_view(), name='trajet-note'),
    path('telemetry/positions/', views.TelemetriePositionsView.as_view(), name='telemetrie-positions'),
    path('speed-limit/', views.LimiteVitesseView.as_view(), name='limite-vitesse'),

    # Back-office (reserve au staff)
    path('staff/speed-zones/', views.ZoneVitesseListCreateView.as_view(), name='staff-zones-vitesse'),
    path(
        'staff/speed-zones/<uuid:id>/',
        views.ZoneVitesseDetailView.as_view(),
        name='staff-zone-vitesse-detail',
    ),
    path('staff/trips/', views.TrajetModerationListView.as_view(), name='staff-trajets'),
]
