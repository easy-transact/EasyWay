from django.urls import path

from . import views

app_name = 'places'

urlpatterns = [
    path('places/search/', views.RechercheView.as_view(), name='recherche'),
    path('places/reverse/', views.InverseView.as_view(), name='inverse'),
    path('places/propose/', views.ProposerLieuView.as_view(), name='proposer'),
    path('places/saved/', views.AdresseEnregistreeListCreateView.as_view(), name='enregistres'),
    path(
        'places/saved/<uuid:id>/',
        views.AdresseEnregistreeDetailView.as_view(),
        name='enregistres-detail',
    ),
    path('places/recent/', views.RechercheRecenteView.as_view(), name='recents'),
    path('places/<uuid:id>/', views.LieuDetailView.as_view(), name='lieu-detail'),
]
