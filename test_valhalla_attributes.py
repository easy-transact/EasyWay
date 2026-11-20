#!/usr/bin/env python3
"""
Script de diagnostic pour tester les attributs retournes et acceptes par Valhalla (/trace_attributes).
Usage:
    python test_valhalla_attributes.py [VALHALLA_URL]
    # Ou dans le conteneur web:
    docker compose exec web python test_valhalla_attributes.py
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
    "node.traffic_signal",       # Devrait etre rejete (attribut sur edge)
    "edge.begin_osm_node_id",    # Devrait etre rejete (non supporte en filtre)
    "edge.end_osm_node_id",      # Devrait etre rejete (non supporte en filtre)
    "matched.point",
    "matched.edge_index",
    "matched.distance_along_edge",
]


def tester_trace_complete():
    print(f"=== 1. Test /trace_attributes complet (sans filtre) sur {VALHALLA_URL} ===")
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
        return data
    except Exception as exc:
        print(f"Echec connexion Valhalla: {exc}")
        return None


def tester_filtres_individuels():
    print(f"\n=== 2. Test des filtres d'attributs individuels sur {VALHALLA_URL} ===")
    acceptes = []
    rejetes = []
    for attr in ATTRIBUTS_A_TESTER:
        payload = {
            "shape": POINTS_TEST,
            "costing": "auto",
            "shape_match": "map_snap",
            "filters": {"attributes": [attr], "action": "include"},
        }
        try:
            resp = requests.post(f"{VALHALLA_URL}/trace_attributes", json=payload, timeout=5)
            if resp.status_code == 200:
                print(f"  [OK 200]  {attr}")
                acceptes.append(attr)
            else:
                msg = resp.json().get("error", resp.text) if resp.headers.get("content-type") == "application/json" else resp.text
                print(f"  [REFUSE {resp.status_code}] {attr} -> {msg}")
                rejetes.append((attr, resp.status_code, msg))
        except Exception as exc:
            print(f"  [ERREUR]  {attr} -> {exc}")

    print("\n--- Bilan des filtres ---")
    print(f"Acceptes ({len(acceptes)}): {', '.join(acceptes)}")
    print(f"Rejetes ({len(rejetes)}): {', '.join([r[0] for r in rejetes])}")


if __name__ == "__main__":
    tester_trace_complete()
    tester_filtres_individuels()
