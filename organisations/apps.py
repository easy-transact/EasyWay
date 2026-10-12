from django.apps import AppConfig


class OrganisationsConfig(AppConfig):
    name = 'organisations'

    def ready(self):
        from . import signals  # noqa: F401
