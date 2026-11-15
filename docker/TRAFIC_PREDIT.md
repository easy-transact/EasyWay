# Trafic prévu dans les tuiles Valhalla

Les profils de vitesse par semaine (calculés depuis l'historique des
conducteurs EasyWay) sont ajoutés aux tuiles routières par l'outil
`valhalla_add_predicted_traffic`. Valhalla s'en sert ensuite pour chaque
calcul, en particulier pour un départ différé (`depart_at` de
`/api/routes/calculate/`).

Format et code : `trips/services/trafic_predit.py`.

## Quand le faire

- Une fois par semaine ou par mois : les profils changent lentement.
- Après une reconstruction des tuiles (nouvelle carte) : elle efface les
  profils.

Il faut un historique suffisant : au moins 50 échantillons et 24 heures de la
semaine couvertes par arête. Les premières semaines, peu d'arêtes auront un
profil ; ce n'est pas un problème, les autres gardent les vitesses de Valhalla.

## Procédure (serveur, `docker compose`)

1. **Exporter les profils** depuis le worker Celery, qui voit
   `/custom_files` en écriture :

   ```bash
   docker compose exec celery_worker python manage.py exporter_trafic_predit /custom_files/trafic_predit
   ```

2. **Les ajouter aux tuiles** dans le conteneur Valhalla :

   ```bash
   docker compose exec valhalla valhalla_add_predicted_traffic -c /custom_files/valhalla.json -t /custom_files/trafic_predit
   ```

3. **Reconstruire l'archive des tuiles** (`valhalla_tiles.tar`) pour que le
   service lise les tuiles modifiées, puis redémarrer Valhalla :

   ```bash
   docker compose exec valhalla valhalla_build_extract -c /custom_files/valhalla.json -v --overwrite
   docker compose restart valhalla
   ```

   Si `traffic.tar` est aussi régénéré (option `--with-traffic`), c'est
   prévu : la publication du trafic détecte le nouveau fichier, réapprend les
   correspondances d'arêtes et garde l'historique par voie OSM.

## À vérifier la première fois

Les commandes des étapes 2 et 3 suivent la documentation de Valhalla 3.5.1 et
l'image `gis-ops/docker-valhalla`, mais n'ont pas été lancées sur votre
serveur. Vérifier :

- le chemin réel de `valhalla.json` et du dossier des tuiles dans le
  conteneur (`ls /custom_files`) ;
- dans le journal de l'étape 2, les lignes `Parsed N ... compressed traffic
  speeds` et `Updated N directed edges` (N > 0) ;
- sur un itinéraire connu, qu'un `depart_at` un lundi à 8 h donne une durée
  plus longue qu'un dimanche à 8 h.

Faire d'abord une sauvegarde de `/custom_files/valhalla_tiles.tar` et du
dossier des tuiles.

## Mise à jour de Valhalla (≥ 3.6) : tronçons OSM

Pour rattacher signalements et vitesses au **tronçon entre deux carrefours**
(au lieu de la voie OSM entière), les tuiles doivent contenir les
identifiants de nœuds OSM :

- Valhalla **3.6.1 ou plus** (option ajoutée par la PR #5450) ; viser une
  version précise (ex. 3.9.1), pas `latest`.
- Dans la configuration de construction des tuiles :
  `"mjolnir": {"keep_osm_node_ids": true}` (nœuds des carrefours, suffisant).
- Reconstruire les tuiles. Le code détecte seul les nœuds (`edge.node_id`,
  `end_node.node_id` dans `trace_attributes`) : aucun réglage côté Django.
- Après la reconstruction, refaire l'export du trafic prévu (ci-dessus).
