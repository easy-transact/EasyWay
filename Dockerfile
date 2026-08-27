# syntax=docker/dockerfile:1

# Django 6.1 exige Python >= 3.12 (verifie ce meme mois sur un serveur cPanel
# qui n'avait que 3.9 -- deploiement bloque net, cf. discussion). 3.12-slim
# plutot que 3.13 : plus de temps pour que les roues precompilees de toutes
# les dependances (psycopg-binary, cryptography, pillow...) existent pour
# cette version, moins de risque de devoir compiler depuis les sources.
FROM python:3.12-slim-bookworm

# GDAL/GEOS/PROJ (django.contrib.gis, charges via ctypes -- doivent exister
# comme libs partagees du systeme, pas juste des paquets pip) + libpq
# (psycopg) + boost/expat/bz2/zlib/cmake (pyosmium, seed_places.py, au cas ou
# aucune roue precompilee n'existe pour cette version de Python -- pip se
# rabat alors sur une compilation depuis les sources).
#
# Paquets -dev plutot que les libs runtime seules : entrainent leurs
# dependances runtime de toute facon, plus fiable que de deviner les noms de
# paquets runtime-only exacts pour cette image sans pouvoir tester le build
# localement (pas de Docker cote WSL sur la machine de dev -- cf. notes).
# Optimisation possible plus tard (build multi-stage, image finale plus
# legere) une fois ce premier build confirme fonctionnel.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gdal-bin \
    libgdal-dev \
    libgeos-dev \
    libproj-dev \
    libpq-dev \
    libboost-dev \
    libexpat1-dev \
    libbz2-dev \
    zlib1g-dev \
    cmake \
    curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings

WORKDIR /app

# Couche dependances separee du code applicatif : rebuild image quand seul le
# code change ne retelecharge/recompile pas tout requirements.txt.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .
RUN chmod +x docker/entrypoint.sh

# Non-root : staticfiles/media doivent rester inscriptibles par cet
# utilisateur (collectstatic tourne au demarrage du conteneur, pas ici --
# STATIC_ROOT depend de variables d'environnement fournies a l'execution,
# pas au moment du build).
RUN groupadd -r django && useradd -r -g django -d /app django \
    && mkdir -p /app/staticfiles /app/media \
    && chown -R django:django /app
USER django

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -sf http://localhost:8000/api/schema/ || exit 1

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
