from django.urls import path

from . import views

app_name = 'ads_admin'

urlpatterns = [
    path('staff/me/', views.StaffMoiView.as_view(), name='staff-moi'),
    path('staff/badges/', views.BadgesView.as_view(), name='staff-badges'),
    path('staff/stats/', views.StatsView.as_view(), name='staff-stats'),
    path('staff/notifications/', views.NotificationListView.as_view(), name='staff-notifications'),
    path('staff/notifications/read-all/', views.NotificationToutLuView.as_view(), name='staff-notifications-tout-lu'),
    path(
        'staff/notifications/preferences/',
        views.PreferencesNotificationView.as_view(),
        name='staff-notifications-preferences',
    ),
    path('staff/notifications/<uuid:id>/read/', views.NotificationLueView.as_view(), name='staff-notification-lue'),
    path('staff/audit/', views.AuditListView.as_view(), name='staff-audit'),
    path('staff/ads/', views.CampagneListCreateView.as_view(), name='staff-campagnes'),
    path('staff/ads/<uuid:id>/', views.CampagneDetailView.as_view(), name='staff-campagne-detail'),
]
