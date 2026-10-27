from django.urls import path

from . import views, vues_avis

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
    path('places/map/', views.LieuxCarteView.as_view(), name='carte'),
    path('places/map/categories/', views.CategoriesCarteView.as_view(), name='carte-categories'),
    path('places/along-route/', views.LieuxSurTrajetView.as_view(), name='sur-trajet'),
    path('places/<uuid:id>/', views.LieuDetailView.as_view(), name='lieu-detail'),
    path('garages/<uuid:id>/reviews/', vues_avis.AvisGarageView.as_view(), name='avis-garage'),
    path('garages/<uuid:id>/reviews/mine/', vues_avis.MonAvisGarageView.as_view(), name='mon-avis-garage'),

    # Back-office (moderation, reserve au staff)
    path('staff/places/', views.LieuModerationListView.as_view(), name='staff-lieux'),
    path('staff/reviews/', vues_avis.AvisModerationListView.as_view(), name='staff-avis'),
    path('staff/reviews/<uuid:id>/remove/', vues_avis.AvisRetirerView.as_view(), name='staff-avis-retirer'),
    path('staff/places/bulk/', views.LieuActionGroupeeView.as_view(), name='staff-lieux-groupe'),
    path('staff/places/categories/', views.CategoriesLieuxView.as_view(), name='staff-lieux-categories'),
    path('staff/places/<uuid:id>/approve/', views.LieuApprouverView.as_view(), name='staff-lieu-approuver'),
    path('staff/places/<uuid:id>/reject/', views.LieuRejeterView.as_view(), name='staff-lieu-rejeter'),
    path('staff/places/<uuid:id>/', views.LieuSupprimerView.as_view(), name='staff-lieu-supprimer'),
    path(
        'staff/establishments/',
        views.EtablissementListCreateView.as_view(),
        name='staff-etablissements',
    ),
    path(
        'staff/establishments/<uuid:id>/',
        views.EtablissementDetailView.as_view(),
        name='staff-etablissement-detail',
    ),
]
