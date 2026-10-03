from django.urls import path

from . import views

app_name = 'infractions'

urlpatterns = [
    path('infractions/', views.InfractionListView.as_view(), name='infractions'),
    path('infractions/categories/', views.CategorieInfractionListView.as_view(), name='categories'),

    # Back-office (reserve au staff)
    path('staff/infractions/', views.InfractionModerationListView.as_view(), name='staff-infractions'),
    path('staff/infractions/import/', views.ImportInfractionsView.as_view(), name='staff-infractions-import'),
    path(
        'staff/infractions/<uuid:id>/',
        views.InfractionModerationDetailView.as_view(),
        name='staff-infraction-detail',
    ),
]
