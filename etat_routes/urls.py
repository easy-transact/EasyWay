from django.urls import path

from . import views

app_name = 'etat_routes'

urlpatterns = [
    path('road-conditions/', views.EtatRoutesCarteView.as_view(), name='carte'),
    path('staff/road-conditions/ranking/', views.ClassementView.as_view(), name='classement'),
    path('staff/road-conditions/settings/', views.ReglagesView.as_view(), name='reglages'),
]
