# Pas d'image officielle Photon sur Docker Hub -- komoot ne publie que le jar
# (github.com/komoot/photon/releases). On le recupere ici plutot que de coder
# en dur un numero de version qui finira par etre perime.
FROM eclipse-temurin:21-jre-jammy

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /photon

RUN JAR_URL=$(curl -s https://api.github.com/repos/komoot/photon/releases/latest \
        | grep '"browser_download_url.*\.jar"' | cut -d '"' -f 4 | head -n 1) \
    && test -n "$JAR_URL" \
    && curl -L -o photon.jar "$JAR_URL"

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 2322
ENTRYPOINT ["/entrypoint.sh"]
