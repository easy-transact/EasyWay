"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView

urlpatterns = [
    path('admin/', admin.site.urls),

    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),

    path('api/', include('accounts.urls')),
    path('api/', include('places.urls')),
    path('api/', include('trips.urls')),
    path('api/', include('community.urls')),

    # Fichiers televerses (avatars) servis par Django lui-meme, y compris en
    # prod : aucun serveur devant gunicorn ne sert /media/ (ea-nginx -> Apache
    # -> gunicorn, cf. docker-compose.prod.yml), donc le static() habituel,
    # inactif quand DEBUG=False, laissait chaque avatar_url en 404. serve()
    # refuse les chemins hors MEDIA_ROOT ; volume faible (un avatar par
    # compte), a deplacer vers le proxy si ca devient un goulot.
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]

if settings.DEBUG:
    from . import dev_views

    urlpatterns += [
        path('api/dev/valhalla/status/', dev_views.valhalla_status, name='dev-valhalla-status'),
        path('api/dev/valhalla/route/', dev_views.valhalla_route, name='dev-valhalla-route'),
    ]
