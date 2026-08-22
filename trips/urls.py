from django.urls import path

from . import views

app_name = 'trips'

urlpatterns = [
    path('itineraires/calculer/', views.CalculItineraireView.as_view(), name='calculer-itineraire'),
    path('trajets/', views.TrajetListeCreationView.as_view(), name='trajets'),
    path('trajets/<uuid:id>/', views.TrajetDetailView.as_view(), name='trajet-detail'),
    path('trajets/<uuid:id>/note/', views.NoterTrajetView.as_view(), name='trajet-note'),
]
