from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

app_name = 'accounts'

urlpatterns = [
    path('verifier-existence/', views.VerifierExistenceView.as_view(), name='verifier-existence'),
    path('inscription/', views.InscriptionView.as_view(), name='inscription'),
    path('connexion/', views.ConnexionView.as_view(), name='connexion'),
    path('connexion-google/', views.ConnexionGoogleView.as_view(), name='connexion-google'),
    path('rafraichir/', TokenRefreshView.as_view(), name='rafraichir'),
    path('deconnexion/', views.DeconnexionView.as_view(), name='deconnexion'),
    path('verifier-email/<str:uidb64>/<str:jeton>/', views.VerifierEmailView.as_view(), name='verifier-email'),
    path(
        'mot-de-passe/reinitialiser/',
        views.DemandeReinitialisationView.as_view(),
        name='mot-de-passe-reinitialiser',
    ),
    path(
        'mot-de-passe/confirmer/',
        views.ConfirmationReinitialisationView.as_view(),
        name='mot-de-passe-confirmer',
    ),
]
