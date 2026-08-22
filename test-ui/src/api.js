// Client de test : ne parle qu'a l'API Django. Valhalla n'est jamais appele
// directement depuis le navigateur -- /api/dev/valhalla/* proxie cote serveur.

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

async function getJson(path) {
  const reponse = await fetch(`${API_URL}${path}`);
  return reponse.json();
}

export function rechercherLieux(q, position) {
  const params = new URLSearchParams({ q });
  if (position) {
    params.set('lat', position.lat);
    params.set('lon', position.lon);
  }
  return getJson(`/api/lieux/recherche/?${params}`);
}

export function statutValhalla() {
  return getJson('/api/dev/valhalla/status/');
}

export function calculerItineraire(depart, arrivee, costing = 'auto') {
  const params = new URLSearchParams({
    from_lat: depart.lat, from_lon: depart.lon,
    to_lat: arrivee.lat, to_lon: arrivee.lon,
    costing,
  });
  return getJson(`/api/dev/valhalla/route/?${params}`);
}
