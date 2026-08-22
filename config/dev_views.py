"""
Proxy Valhalla cote serveur (DEBUG uniquement, jamais monte en prod) : le
frontend de test (test-ui/, Vite) n'appelle jamais Valhalla directement,
seulement l'API Django. Delegue a ClientValhalla (trips/services/) --
meme disjoncteur, meme logique de collecte de variantes (alternates Valhalla
+ objectif shortest si besoin) que le vrai module trajets (P3), pas une
copie qui risquerait de diverger.
"""

import requests
from django.conf import settings
from django.http import JsonResponse

from trips.services.client_valhalla import ClientValhalla


def valhalla_status(request):
    try:
        r = requests.get(f'{settings.VALHALLA_URL}/status', timeout=5)
        r.raise_for_status()
        return JsonResponse({'ok': True, **r.json()})
    except requests.RequestException as exc:
        return JsonResponse({'ok': False, 'erreur': str(exc)}, status=502)


def valhalla_route(request):
    try:
        from_lat = float(request.GET['from_lat'])
        from_lon = float(request.GET['from_lon'])
        to_lat = float(request.GET['to_lat'])
        to_lon = float(request.GET['to_lon'])
    except (KeyError, ValueError):
        return JsonResponse({'ok': False, 'erreur': 'from_lat/from_lon/to_lat/to_lon requis.'}, status=400)

    options = {'costing': request.GET.get('costing', 'auto')}
    routes = ClientValhalla().calculer_itineraires((from_lat, from_lon), (to_lat, to_lon), options)
    return JsonResponse({'ok': True, 'routes': routes})
