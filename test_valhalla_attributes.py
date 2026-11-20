#!/usr/bin/env python3
"""
Script de diagnostic pour tester les attributs retournes et acceptes par Valhalla (/trace_attributes).
Usage:
    python test_valhalla_attributes.py [VALHALLA_URL]
    # Ou dans le conteneur web:
    docker compose -f docker-compose.prod.yml exec web python test_valhalla_attributes.py
"""

import json
import os
import sys
import requests

VALHALLA_URL = sys.argv[1] if len(sys.argv) > 1 else os.getenv("VALHALLA_URL", "http://localhost:8002")

# Coordonnees de test (centre de Douala / axe passant)
POINTS_TEST = [
    {"lat": 4.0505, "lon": 9.7042, "time": 0},
    {"lat": 4.0515, "lon": 9.7055, "time": 10},
    {"lat": 4.0525, "lon": 9.7068, "time": 20},
]

ATTRIBUTS_A_TESTER = [
    "edge.id",
    "edge.way_id",
    "edge.forward",
    "edge.speed",
    "edge.length",
    "edge.road_class",
    "edge.names",
    "edge.roundabout",
    "edge.toll",
    "edge.traffic_signal",
    "node.type",
    "node.traffic_signal",
    "edge.begin_osm_node_id",
    "edge.end_osm_node_id",
    "matched.point",
    "matched.edge_index",
    "matched.distance_along_edge",
]


def tester_version():
    print(f"=== 0. Version de Valhalla ({VALHALLA_URL}/status) ===")
    try:
        resp = requests.get(f"{VALHALLA_URL}/status", timeout=5)
        print(f"Status: {resp.status_code}")
        print(f"Response: {resp.text}")
    except Exception as exc:
        print(f"Erreur status: {exc}")


def tester_trace_complete():
    print(f"\n=== 1. Test /trace_attributes complet (sans filtre) sur {VALHALLA_URL} ===")
    payload = {
        "shape": POINTS_TEST,
        "costing": "auto",
        "shape_match": "map_snap",
    }
    try:
        resp = requests.post(f"{VALHALLA_URL}/trace_attributes", json=payload, timeout=10)
        print(f"Status Code: {resp.status_code}")
        if resp.status_code != 200:
            print(f"Erreur: {resp.text}")
            return None
        data = resp.json()
        edges = data.get("edges", [])
        print(f"Nombre d'aretes retournees: {len(edges)}")
        if edges:
            premiere = edges[0]
            print("\nCles presentes sur edge[0]:")
            for k in sorted(premiere.keys()):
                val = premiere[k]
                if isinstance(val, (dict, list)):
                    print(f"  - {k}: {type(val).__name__} (cles: {list(val.keys()) if isinstance(val, dict) else len(val)})")
                else:
                    print(f"  - {k}: {val}")

            end_node = premiere.get("end_node")
            if end_node:
                print("\nCles presentes sur end_node de edge[0]:")
                for k in sorted(end_node.keys()):
                    print(f"  - {k}: {end_node[k]}")

            # Verification des champs specifiques
            print("\nPresence des champs critiques:")
            print(f"  - way_id: {premiere.get('way_id')}")
            print(f"  - forward: {premiere.get('forward')}")
            print(f"  - traffic_signal (sur edge): {premiere.get('traffic_signal')}")
            print(f"  - traffic_signal (sur end_node): {(end_node or {}).get('traffic_signal')}")
            print(f"  - node_id (debut arete): {premiere.get('node_id')}")
            print(f"  - node_id (fin arete/end_node): {(end_node or {}).get('node_id')}")
            print(f"  - begin_osm_node_id: {premiere.get('begin_osm_node_id')}")
            print(f"  - end_osm_node_id: {premiere.get('end_osm_node_id')}")
        return data
    except Exception as exc:
        print(f"Echec connexion Valhalla: {exc}")
        return None


def tester_filtres_specifiques():
    print(f"\n=== 2. Contenu exact retourne selon les filtres demandes ===")
    groupes_filtres = [
        ("Filtre noeuds OSM", ["edge.begin_osm_node_id", "edge.end_osm_node_id"]),
        ("Filtre node.traffic_signal", ["node.traffic_signal"]),
        ("Filtre edge.traffic_signal", ["edge.traffic_signal"]),
        ("Filtre ATTRIBUTS_TRONCON actuel", [
            'edge.id', 'edge.length', 'edge.speed', 'edge.way_id', 'edge.road_class', 'edge.names',
            'edge.roundabout', 'edge.toll', 'edge.traffic_signal', 'node.type',
            'matched.point', 'matched.type', 'matched.edge_index', 'matched.distance_along_edge',
        ]),
    ]
    for nom, attrs in groupes_filtres:
        payload = {
            "shape": POINTS_TEST,
            "costing": "auto",
            "shape_match": "map_snap",
            "filters": {"attributes": attrs, "action": "include"},
        }
        try:
            resp = requests.post(f"{VALHALLA_URL}/trace_attributes", json=payload, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                edges = data.get("edges", [])
                print(f"\n* {nom} (HTTP 200) -> {len(edges)} aretes")
                if edges:
                    print(f"  Cles edge[0]: {list(edges[0].keys())}")
                    if "end_node" in edges[0]:
                        print(f"  Cles edge[0]['end_node']: {list(edges[0]['end_node'].keys())}")
                    print(f"  Valeurs edge[0]: {json.dumps(edges[0], indent=2)}")
            else:
                print(f"\n* {nom} (HTTP {resp.status_code}) -> {resp.text}")
        except Exception as exc:
            print(f"\n* {nom} -> Erreur: {exc}")


if __name__ == "__main__":
    tester_version()
    tester_trace_complete()
    tester_filtres_specifiques()
