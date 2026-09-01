class NoStoreApiMiddleware:
    """Force Cache-Control: no-store sur /api/ -- l'appli est deployee derriere
    un reverse proxy cPanel dont le cache partage (ea-nginx) cle uniquement sur
    l'URL, sans tenir compte du header Authorization. Une reponse authentifiee
    mise en cache une fois (ex. une liste vide) etait alors servie a tous les
    autres utilisateurs tapant la meme URL -- observe en prod sur
    GET /api/places/saved/. no-store empeche tout cache partage de la
    conserver, quoi que la config nginx fasse par ailleurs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith('/api/'):
            response['Cache-Control'] = 'no-store'
        return response
