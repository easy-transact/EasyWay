#!/bin/sh
# ENTRYPOINT du service web uniquement -- migrate/collectstatic n'ont besoin
# de tourner qu'une fois par deploiement, pas a chaque conteneur. Les
# services celery (docker-compose.yml) passent entrypoint: [] pour sauter ce
# script et lancer directement leur commande -- sinon plusieurs services
# feraient la meme migration en meme temps au demarrage.
set -e

python manage.py migrate --noinput
python manage.py collectstatic --noinput

exec "$@"
