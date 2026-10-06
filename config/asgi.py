"""
ASGI config for config project.

Servi par le service `ws` (daphne, cf. docker-compose) pour le WebSocket
/ws/incidents/ (signalements en temps reel, community/consumers.py). Le HTTP
de l'API reste servi par gunicorn/WSGI (config/wsgi.py) ; la branche 'http'
ci-dessous ne sert qu'a ne pas casser un appel HTTP qui arriverait ici.

Pas d'AllowedHostsOriginValidator : l'application mobile n'envoie pas
d'en-tete Origin, que ce validateur refuserait. La barriere a l'ouverture est
la signature HMAC (SignatureHmacWsMiddleware), l'authentification le JWT du
premier message.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Avant tout import qui touche aux modeles (consumers) : initialise Django.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from django.urls import path  # noqa: E402

from community.consumers import IncidentsConsumer  # noqa: E402
from config.signature_hmac import SignatureHmacWsMiddleware  # noqa: E402

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': SignatureHmacWsMiddleware(URLRouter([
        path('ws/incidents/', IncidentsConsumer.as_asgi()),
    ])),
})
