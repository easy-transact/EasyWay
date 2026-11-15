# Vitesses par défaut réalistes dans les tuiles Valhalla (revue 1.1)

## Le problème

Valhalla choisit sa route avec des vitesses **théoriques par classe de route**.
Ses valeurs par défaut supposent un réseau fluide : Akwa Palace → Total
Bonateki (3,2 km) est annoncé à 3 min, soit **63 km/h de moyenne en plein
Douala** (vérifié le 28/10). Résultat : un axe primaire supposé rapide est
toujours préféré à une rue secondaire, même quand l'axe est bouché.

La correction de durée en ville (`trips/services/duree_ville.py`) applique un
facteur **après** le choix de Valhalla : elle change la durée affichée, **pas le
tracé choisi**, et comme le facteur est identique pour tous les candidats, elle
ne change pas non plus leur ordre. Le trafic mesuré (`traffic.tar`) ne couvre
que les arêtes où ≥ 2 conducteurs EasyWay passent dans les 5 min — rare pour
l'instant.

**La seule correction de fond agit au moment de la construction des tuiles** :
donner à Valhalla des vitesses par défaut plus proches de la réalité
camerounaise, avec un **écart faible entre axe primaire urbain et rue
secondaire**. Un axe primaire et une secondaire deviennent alors comparables, et
Valhalla départage sur la distance et le nombre de manœuvres.

> Ce travail **n'a pas pu être lancé ici** (pas d'instance Valhalla dans
> l'environnement de dev). Les commandes ci-dessous sont à exécuter **sur le
> serveur** et à vérifier (cf. « À vérifier la première fois »).

## Étape 1 — le plus simple : vitesses par défaut OSM communautaires

L'image `ghcr.io/gis-ops/docker-valhalla` sait appliquer, à la construction des
tuiles, le jeu de vitesses par défaut maintenu par la communauté OpenStreetMap
(nettement moins optimiste en ville que les valeurs internes de Valhalla), via
la variable d'environnement `use_default_speeds_config`.

C'est déjà posé dans `docker-compose.yml` (service `valhalla`) :

```yaml
      - use_default_speeds_config=True
```

Pour que ça prenne effet, il faut **un rebuild des tuiles une seule fois**
(comme `build_time_zones`), puis régénérer `traffic.tar` :

```bash
# 1. Sauvegarde
cp valhalla/custom_files/valhalla_tiles.tar valhalla/custom_files/valhalla_tiles.tar.bak

# 2. Rebuild (force_rebuild=True une passe), puis le remettre à False
#    Soit en passant la variable à la volée :
docker compose run --rm -e force_rebuild=True valhalla
#    (laisser finir la construction, puis Ctrl-C), ou mettre force_rebuild=True
#    dans le compose, `docker compose up -d valhalla`, attendre, puis repasser à False.

# 3. Régénérer le squelette traffic.tar et redémarrer
docker compose exec valhalla valhalla_build_extract -c /custom_files/valhalla.json -v --overwrite --with-traffic
docker compose restart valhalla
```

La publication du trafic (`trips.tasks.publier_trafic_temps_reel`) détecte le
nouveau fichier, réapprend les correspondances d'arêtes et garde l'historique
par voie OSM (cf. `TRAFIC_PREDIT.md`).

## Étape 2 — si l'étape 1 ne suffit pas : valeurs calibrées Cameroun

Si après l'étape 1 les axes principaux restent trop favorisés, fournir un
fichier de vitesses **sur mesure** et y pointer la config de build Valhalla
(`mjolnir.default_speeds_config` dans `custom_files/valhalla.json`), puis
rebuilder comme à l'étape 1.

Un modèle de départ est fourni : `docker/default_speeds.cameroun.json` (hors de
`custom_files/` exprès, pour ne pas être chargé tel quel par mégarde).
**Le schéma exact dépend de la version de Valhalla** (vérifier contre la doc de
la version installée) ; les *valeurs* (km/h) sont le point de départ EasyWay à
caler sur les données réelles. Idée directrice : en ville, resserrer l'écart —
primaire ~40, secondaire/tertiaire ~30-35, résidentiel ~25 — au lieu des
60+ km/h implicites. Après vérification du schéma, copier le fichier dans
`custom_files/` et y pointer `mjolnir.default_speeds_config`.

Pointer la config dans `custom_files/valhalla.json` :

```json
  "mjolnir": {
    "default_speeds_config": "/custom_files/default_speeds.json",
    ...
  }
```

## Calibrer les valeurs

Le projet enregistre déjà de quoi calibrer, sans deviner :

- `PrevisionItineraire` (durée annoncée) comparé à `Trajet.duree_reelle` —
  l'écart par créneau horaire montre de combien Valhalla sur-estime.
- `EchantillonVitesse` regroupé par classe de route donne la vitesse réelle
  observée par classe.
- Garder 2-3 trajets de référence où la secondaire est connue plus rapide
  (Akwa Palace → Total Bonateki est déjà cité dans le code) et comparer la
  route recommandée **avant / après** chaque étape.

## À vérifier la première fois

- Le chemin réel de `valhalla.json` et du dossier des tuiles dans le conteneur
  (`docker compose exec valhalla ls /custom_files`).
- Que `use_default_speeds_config` est bien pris en compte par la version de
  l'image (journal de construction des tuiles).
- Sur un itinéraire de référence bouché aux heures de pointe, que la route
  recommandée bascule (ou au moins que l'écart de durée primaire/secondaire se
  resserre).

## Épingler la version de l'image (recommandé)

`docker-compose.yml` utilise `ghcr.io/gis-ops/docker-valhalla/valhalla:latest`.
Épingler une **version précise** (ex. ≥ 3.6.1, comme pour les tronçons OSM, cf.
`TRAFIC_PREDIT.md`) évite qu'un `latest` change le comportement des vitesses par
défaut ou du build d'un déploiement à l'autre. À faire avec le même rebuild.
