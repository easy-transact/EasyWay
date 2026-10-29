from django.urls import path

from . import views, vues_pilotage

app_name = 'trips'

urlpatterns = [
    path('routes/calculate/', views.CalculItineraireView.as_view(), name='calculer-itineraire'),
    path('trips/', views.TrajetListeCreationView.as_view(), name='trajets'),
    path('trips/<uuid:id>/', views.TrajetDetailView.as_view(), name='trajet-detail'),
    path('trips/<uuid:id>/rate/', views.NoterTrajetView.as_view(), name='trajet-note'),
    path('trips/<uuid:id>/better-route/', views.MeilleurItineraireView.as_view(), name='trajet-meilleur-itineraire'),
    path('trips/<uuid:id>/switch-route/', views.ChangerItineraireView.as_view(), name='trajet-changer-itineraire'),
    path('trips/<uuid:id>/alternative-routes/', views.AutresItinerairesView.as_view(), name='trajet-autres-itineraires'),
    path('telemetry/positions/', views.TelemetriePositionsView.as_view(), name='telemetrie-positions'),
    path('speed-limit/', views.LimiteVitesseView.as_view(), name='limite-vitesse'),
    path(
        'speed-zones/along-route/',
        views.ZonesVitesseSurTrajetView.as_view(),
        name='zones-vitesse-sur-trajet',
    ),

    # Back-office (reserve au staff)
    path('staff/speed-zones/', views.ZoneVitesseListCreateView.as_view(), name='staff-zones-vitesse'),
    path(
        'staff/speed-zones/<uuid:id>/',
        views.ZoneVitesseDetailView.as_view(),
        name='staff-zone-vitesse-detail',
    ),
    path('staff/trips/', views.TrajetModerationListView.as_view(), name='staff-trajets'),
    path('staff/trips/<uuid:id>/', views.TrajetModerationDetailView.as_view(), name='staff-trajet-detail'),

    # Pilotage (back-office) : ce que disent les donnees deja recues.
    path('staff/insights/eta/', vues_pilotage.FiabiliteTempsView.as_view(), name='pilotage-eta'),
    path('staff/insights/moderation-queue/', vues_pilotage.FileModerationView.as_view(), name='pilotage-moderation'),
    path('staff/insights/route-proposals/', vues_pilotage.PropositionsView.as_view(), name='pilotage-propositions'),
    path('staff/insights/data-density/', vues_pilotage.DensiteDonneesView.as_view(), name='pilotage-densite'),
    path('staff/insights/live-map/', vues_pilotage.CarteDirectView.as_view(), name='pilotage-direct'),
]
