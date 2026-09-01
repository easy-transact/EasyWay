from rest_framework.pagination import PageNumberPagination


class StaffPagination(PageNumberPagination):
    """Pagination des listes de moderation du back-office (Lieux/Incidents/
    Utilisateurs) -- rien d'equivalent n'existe encore ailleurs dans l'API,
    qui ne pagine aujourd'hui aucune liste cote utilisateur final."""

    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100
