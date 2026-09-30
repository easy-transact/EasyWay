from django.urls import path
from drf_spectacular.utils import extend_schema
from rest_framework_simplejwt.views import TokenRefreshView

from . import views
from .serializers import JetonsSerializer, RafraichissementRequeteSerializer

app_name = 'accounts'

# Stock TokenRefreshView/serializer, unmodified: access/refresh (English) is
# now the project-wide convention, so SimpleJWT's own field names already
# match. Only the documented schema is overridden: the stock serializer made
# drf-spectacular declare `access` as required in the request. The response
# is JetonsSerializer since ROTATE_REFRESH_TOKENS also returns a new refresh.
RafraichirView = extend_schema(
    tags=['Authentication'],
    request=RafraichissementRequeteSerializer,
    responses=JetonsSerializer,
)(TokenRefreshView)

urlpatterns = [
    # Authentification
    path('auth/check-existence/', views.VerifierExistenceView.as_view(), name='verifier-existence'),
    path('auth/register/', views.InscriptionView.as_view(), name='inscription'),
    path('auth/login/', views.ConnexionView.as_view(), name='connexion'),
    path('auth/google-login/', views.ConnexionGoogleView.as_view(), name='connexion-google'),
    path('auth/refresh/', RafraichirView.as_view(), name='rafraichir'),
    path('auth/logout/', views.DeconnexionView.as_view(), name='deconnexion'),
    path(
        'auth/verify-email/<str:uidb64>/<str:token>/',
        views.VerifierEmailView.as_view(),
        name='verifier-email',
    ),
    path(
        'auth/password/reset/',
        views.DemandeReinitialisationView.as_view(),
        name='mot-de-passe-reinitialiser',
    ),
    path(
        'auth/password/confirm/',
        views.ConfirmationReinitialisationView.as_view(),
        name='mot-de-passe-confirmer',
    ),

    # Compte (profil, parametres, appareils)
    path('users/me/', views.MoiView.as_view(), name='moi'),
    path('users/me/avatar/', views.AvatarView.as_view(), name='moi-avatar'),
    path('users/me/settings/', views.ParametresView.as_view(), name='moi-parametres'),
    path('users/me/statistics/', views.StatistiquesView.as_view(), name='moi-statistiques'),
    path('devices/', views.AppareilCreationView.as_view(), name='appareils'),
    path('devices/<uuid:id>/', views.AppareilSuppressionView.as_view(), name='appareil-suppression'),

    # Configuration publique
    path('config/', views.ConfigView.as_view(), name='config'),

    # Back-office (moderation, reserve au staff)
    path('staff/users/', views.UtilisateurModerationListView.as_view(), name='staff-utilisateurs'),
    path('staff/users/<uuid:id>/ban/', views.UtilisateurBanView.as_view(), name='staff-utilisateur-bannir'),
    path('staff/users/<uuid:id>/unban/', views.UtilisateurUnbanView.as_view(), name='staff-utilisateur-debannir'),
]
