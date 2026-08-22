from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

app_name = 'accounts'

urlpatterns = [
    # Authentification
    path('auth/verifier-existence/', views.VerifierExistenceView.as_view(), name='verifier-existence'),
    path('auth/inscription/', views.InscriptionView.as_view(), name='inscription'),
    path('auth/connexion/', views.ConnexionView.as_view(), name='connexion'),
    path('auth/connexion-google/', views.ConnexionGoogleView.as_view(), name='connexion-google'),
    path('auth/rafraichir/', TokenRefreshView.as_view(), name='rafraichir'),
    path('auth/deconnexion/', views.DeconnexionView.as_view(), name='deconnexion'),
    path('auth/verifier-email/<str:uidb64>/<str:jeton>/', views.VerifierEmailView.as_view(), name='verifier-email'),
    path(
        'auth/mot-de-passe/reinitialiser/',
        views.DemandeReinitialisationView.as_view(),
        name='mot-de-passe-reinitialiser',
    ),
    path(
        'auth/mot-de-passe/confirmer/',
        views.ConfirmationReinitialisationView.as_view(),
        name='mot-de-passe-confirmer',
    ),

    # Compte (profil, parametres, appareils)
    path('utilisateurs/moi/', views.MoiView.as_view(), name='moi'),
    path('utilisateurs/moi/avatar/', views.AvatarView.as_view(), name='moi-avatar'),
    path('utilisateurs/moi/parametres/', views.ParametresView.as_view(), name='moi-parametres'),
    path('utilisateurs/moi/statistiques/', views.StatistiquesView.as_view(), name='moi-statistiques'),
    path('appareils/', views.AppareilCreationView.as_view(), name='appareils'),
    path('appareils/<uuid:id>/', views.AppareilSuppressionView.as_view(), name='appareil-suppression'),

    # Configuration publique
    path('config/', views.ConfigView.as_view(), name='config'),
]
