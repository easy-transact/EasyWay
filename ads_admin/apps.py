from django.apps import AppConfig


class AdsAdminConfig(AppConfig):
    name = 'ads_admin'

    def ready(self):
        from . import signals  # noqa: F401
