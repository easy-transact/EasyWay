#!/bin/bash
set -euo pipefail

DATA_DIR=/photon/photon_data

# nominatim-import est long (parcourt toute la table placex de Nominatim) --
# ne le refait pas si l'index existe deja d'un run precedent (volume monte).
if [ ! -d "$DATA_DIR/elasticsearch" ]; then
  echo "[photon] Aucun index existant -- import depuis Nominatim (${NOMINATIM_HOST}:${NOMINATIM_PORT})..."
  java -jar photon.jar -nominatim-import \
    -host "${NOMINATIM_HOST}" \
    -port "${NOMINATIM_PORT}" \
    -database "${NOMINATIM_DB}" \
    -user "${NOMINATIM_USER}" \
    -password "${NOMINATIM_PASSWORD}" \
    -languages "${PHOTON_LANGUAGES}" \
    -data-dir "$DATA_DIR"
else
  echo "[photon] Index existant trouve dans $DATA_DIR, import saute."
fi

echo "[photon] Demarrage du serveur sur :2322..."
exec java -jar photon.jar -data-dir "$DATA_DIR" -listen-port 2322 -listen-ip 0.0.0.0
