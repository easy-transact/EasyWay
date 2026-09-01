"""
Django settings for config project.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('DJANGO_SECRET_KEY', default='django-insecure-42v-p@slr%1$b&nm#-v^r+2^xga$e8=^c#dv)#70aea9^_$d$=')

DEBUG = env('DEBUG')

ALLOWED_HOSTS = env.list('DJANGO_ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])

# Requis par Django >= 4 pour accepter les POST (admin, login) recus via un
# domaine autre que ALLOWED_HOSTS local (ex : tunnel ngrok). Vide par defaut.
CSRF_TRUSTED_ORIGINS = env.list('CSRF_TRUSTED_ORIGINS', default=[])

if not DEBUG:
    # Durcissement releve par `manage.py check --deploy` -- desactive en dev
    # (DEBUG=True) pour ne pas casser le HTTP simple en local/ngrok. Les
    # trois flags TLS sont explicitement desactivables (pas juste
    # SECURE_SSL_REDIRECT) pour une phase transitoire sans HTTPS devant
    # l'appli (ex. premier deploiement, port expose directement le temps
    # qu'un nom de domaine + reverse-proxy TLS soit branche) -- sans ca,
    # SECURE_SSL_REDIRECT redirigerait vers un HTTPS qui n'existe pas encore,
    # rendant l'appli injoignable.
    SECURE_SSL_REDIRECT = env.bool('SECURE_SSL_REDIRECT', default=True)
    SESSION_COOKIE_SECURE = env.bool('SESSION_COOKIE_SECURE', default=True)
    CSRF_COOKIE_SECURE = env.bool('CSRF_COOKIE_SECURE', default=True)
    # Valeur prudente au premier deploiement (1h, pas le 1 an usuel) : HSTS
    # est mis en cache par le navigateur, une erreur de config HTTPS
    # deviendrait difficile a corriger rapidement. A augmenter une fois
    # le HTTPS confirme stable.
    SECURE_HSTS_SECONDS = env.int('SECURE_HSTS_SECONDS', default=3600)
    # Suppose un reverse-proxy (nginx/Traefik/LB) qui termine le TLS et
    # transmet X-Forwarded-Proto -- a verifier avant activation : un proxy
    # qui ne pose pas cet en-tete (ou qui laisse passer celui du client)
    # romprait la detection HTTPS, voire ouvrirait une usurpation.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.gis',
    'django.contrib.postgres',

    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'drf_spectacular',
    'corsheaders',

    'accounts',
    'places',
    'trips',
    'community',
    'ads_admin',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    # cf. config/middleware.py : le reverse proxy prod (ea-nginx/cPanel) met en
    # cache par URL seule, sans tenir compte d'Authorization -- doit tourner
    # avant tout middleware qui pourrait court-circuiter la reponse.
    'config.middleware.NoStoreApiMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases
# Requires the PostGIS extension enabled on the target database (CREATE EXTENSION postgis;)

DATABASES = {
    'default': env.db(
        'DATABASE_URL',
        default='postgis://easyway:easyway@localhost:5432/easyway',
    ),
}
DATABASES['default']['ENGINE'] = 'django.contrib.gis.db.backends.postgis'
# A transaction-mode PgBouncer (planned in front of Postgres for prod) hands
# a request's connection back to the pool between statements and can't hold
# a named server-side cursor open across that gap -- both fail silently
# rather than raising, so this is set now rather than debugged the day
# someone reaches for QuerySet.iterator() on a big table.
DATABASES['default']['DISABLE_SERVER_SIDE_CURSORS'] = True
DATABASES['default']['CONN_MAX_AGE'] = 0

AUTH_USER_MODEL = 'accounts.Utilisateur'


REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    # Throttle scopes couvrent les endpoints sensibles (inscription/connexion/
    # reset mot de passe) : pas besoin de django-ratelimit pour ca.
    'DEFAULT_THROTTLE_RATES': {
        'inscription': '10/hour',
        'connexion': '20/hour',
        'mot-de-passe': '5/hour',
    },
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Easy Way API',
    'DESCRIPTION': 'Community navigation and road incident reporting application',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    # Plusieurs modeles ont un champ 'statut' distinct (Lieu, Trajet, Incident) :
    # sans ca drf-spectacular genere des noms d'enum ambigus (StatutXxxEnum).
    'ENUM_NAME_OVERRIDES': {
        'PlaceStatusEnum': 'places.models.StatutLieu',
        'TripStatusEnum': 'trips.models.StatutTrajet',
        'IncidentStatusEnum': 'community.models.StatutIncident',
    },
}

# Jetons d'acces et de rafraichissement (section 4.1 : "jetons d'acces et de
# rafraichissement" ; deconnexion = "revoquerFamille(jeton)" -> rotation +
# blacklist du refresh token approxime la revocation de famille de jetons).
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
}

# Connexion avec Google (ClientGoogleOAuth.verifierJeton, Fig. 11) : audience
# attendue lors de la verification du jeton d'identite transmis par l'app mobile.
GOOGLE_OAUTH_CLIENT_ID = env('GOOGLE_OAUTH_CLIENT_ID', default='')

# test-ui/ (Vite, port 5173) et back-office/ (Vite, port 5174 -- 5173 deja
# pris) : les deux clients web amenes a appeler l'API en cross-origin pour
# l'instant. Vide (donc aucune origine autorisee) des que DEBUG=False.
CORS_ALLOWED_ORIGINS = env.list(
    'CORS_ALLOWED_ORIGINS',
    default=['http://localhost:5173', 'http://localhost:5174'] if DEBUG else [],
)

# Idempotency-Key (POST /api/incidents/, cf. community/views.py) n'est pas
# dans la liste par defaut de django-cors-headers -- sans cet ajout, le
# preflight OPTIONS le rejette et le navigateur bloque la requete avant
# meme qu'elle atteigne Django.
from corsheaders.defaults import default_headers  # noqa: E402

CORS_ALLOW_HEADERS = list(default_headers) + ['idempotency-key']

# Routage (ClientValhalla, trips/services/) : instance Valhalla du
# docker-compose local, ou service manage si deploye separement.
VALHALLA_URL = env('VALHALLA_URL', default='http://localhost:8002')

# Geocodeur externe (ClientNominatim, places/services/) : recherche/inverse
# fusionnes dans les donnees locales, avec repli si indisponible (P2b).
NOMINATIM_URL = env('NOMINATIM_URL', default='http://localhost:8003')

# Photon (ClientPhoton, places/services/) : recherche uniquement -- pas de
# geocodage inverse cote Photon, Nominatim garde ce role (P2b).
PHOTON_URL = env('PHOTON_URL', default='http://localhost:2322')

# Quota horaire de signalements (ServiceIncident._verifier_quota) -- desactive
# temporairement (2026-08-25) pour ne pas bloquer les tests manuels repetes
# pendant le developpement. A remettre a True avant un usage reel.
QUOTA_SIGNALEMENTS_ACTIF = env.bool('QUOTA_SIGNALEMENTS_ACTIF', default=False)

# Cache du calcul d'itineraire (ServiceItineraire, 3 min) et compteurs du
# disjoncteur ClientValhalla. IGNORE_EXCEPTIONS : un Redis indisponible degrade
# en cache-miss (recalcul a chaque appel) plutot que de faire planter l'API --
# le cache est une optimisation, jamais une dependance dure.
CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': env('REDIS_URL', default='redis://localhost:6379/1'),
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            'IGNORE_EXCEPTIONS': True,
        },
    },
}


# Password validation
# https://docs.djangoproject.com/en/6.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 8},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.1/topics/i18n/

# Anglais : les messages d'erreur INTEGRES a Django/DRF (404 par defaut,
# "this field is required", "invalid pk", validateurs de mot de passe, etc.)
# suivent cette langue -- doit rester coherent avec la traduction manuelle du
# reste de l'API (accounts/serializers.py et consorts, cf. discussion), sans
# quoi ces messages-la restent en francais alors que tout le reste est en anglais.
LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True

# Celery (community/tasks.py) : DB Redis distincte (/2) du cache applicatif
# (/1) -- broker/resultats et cache ne doivent jamais partager un espace de
# cles. Beat schedule fige en code plutot que django-celery-beat : suffisant
# tant que rien n'a besoin d'editer la cadence depuis l'admin.
CELERY_BROKER_URL = env('CELERY_BROKER_URL', default='redis://localhost:6379/2')
CELERY_RESULT_BACKEND = env('CELERY_RESULT_BACKEND', default='redis://localhost:6379/2')
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    'expirer-incidents': {
        'task': 'community.tasks.expirer_incidents',
        'schedule': 60.0,
    },
    'consommer-positions': {
        'task': 'trips.tasks.consommer_positions',
        # Assez frequent pour vider le flux sans laisser trainer les positions
        # (impact direct sur la fraicheur du trafic), sans non plus matraquer
        # Meili/Redis a chaque tick -- pas mesure sur donnees reelles, a ajuster.
        'schedule': 10.0,
    },
    'flusher-echantillons-vitesse': {
        'task': 'trips.tasks.flusher_echantillons_vitesse',
        # Alignee sur TAILLE_BUCKET_S (5 min, consommateur_positions.py) : un
        # bucket ne peut de toute facon pas se fermer plus souvent que ca.
        'schedule': 300.0,
    },
}

# P5 (trips/services/producteur_evenements.py) : DB Redis distincte (/3) pour
# le flux Streams des positions GPS brutes -- retention courte (~2h, purgee
# par MAXLEN sur XADD), jamais partagee avec le cache ou le broker Celery.
TELEMETRIE_REDIS_URL = env('TELEMETRIE_REDIS_URL', default='redis://localhost:6379/3')


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.1/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# WhiteNoise : le conteneur Django sert lui-meme son admin/Swagger UI sans
# nginx/reverse-proxy dedie aux fichiers statiques -- collectstatic (fait par
# docker/entrypoint-web.sh au demarrage) remplit STATIC_ROOT, WhiteNoise sert
# depuis la, compresse et hashe les noms de fichiers (cache long terme sur).
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
}

# Dev only : FileSystemStorage local. A remplacer par django-storages/S3
# (ou R2) avant la mise en production.
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Email
# https://docs.djangoproject.com/en/6.1/topics/email/#topic-email-configuration
# Dev/local : MailDev (docker-compose service `maildev`) catche tout sur le port
# SMTP 1025, sans authentification ni TLS ; consultable sur http://localhost:1080.

EMAIL_BACKEND = env('EMAIL_BACKEND', default='django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = env('EMAIL_HOST', default='localhost')
EMAIL_PORT = env.int('EMAIL_PORT', default=1025)
EMAIL_HOST_USER = env('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = env('EMAIL_HOST_PASSWORD', default='')
EMAIL_USE_TLS = env.bool('EMAIL_USE_TLS', default=False)
EMAIL_USE_SSL = env.bool('EMAIL_USE_SSL', default=False)
DEFAULT_FROM_EMAIL = env('DEFAULT_FROM_EMAIL', default='Easy Way <no-reply@easyway.local>')

# Base utilisee pour construire les liens envoyes par email (verification,
# reinitialisation) ; a pointer vers le deep link / la page de l'app mobile.
FRONTEND_URL = env('FRONTEND_URL', default='http://localhost:8000')
