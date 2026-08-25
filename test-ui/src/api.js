// Client de test : ne parle qu'a l'API Django (jamais Valhalla directement,
// sauf le badge de statut qui passe par le proxy dev -- voir /api/dev/valhalla/*).

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const CLE_STOCKAGE_JETONS = 'easyway_test_ui_tokens';

let jetons = chargerJetons();

function chargerJetons() {
  try {
    const brut = localStorage.getItem(CLE_STOCKAGE_JETONS);
    return brut ? JSON.parse(brut) : null;
  } catch {
    return null;
  }
}

function sauvegarderJetons(nouveauxJetons) {
  jetons = nouveauxJetons;
  if (jetons) {
    localStorage.setItem(CLE_STOCKAGE_JETONS, JSON.stringify(jetons));
  } else {
    localStorage.removeItem(CLE_STOCKAGE_JETONS);
  }
}

export function estConnecte() {
  return jetons !== null;
}

export function deconnecter() {
  sauvegarderJetons(null);
}

async function appel(path, { method = 'GET', body, headers = {}, authentifie = false, dejaReessaye = false } = {}) {
  const entetes = { ...headers };
  if (body !== undefined) entetes['Content-Type'] = 'application/json';
  if (authentifie) {
    if (!jetons) throw new Error('Non connecte.');
    entetes['Authorization'] = `Bearer ${jetons.access}`;
  }

  const reponse = await fetch(`${API_URL}${path}`, {
    method,
    headers: entetes,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  // Access token expire (15 min) : une seule tentative de rafraichissement
  // automatique, jamais de boucle si le refresh lui-meme est invalide/expire.
  if (reponse.status === 401 && authentifie && !dejaReessaye && jetons?.refresh) {
    const rafraichi = await tenterRafraichissement();
    if (rafraichi) {
      return appel(path, { method, body, headers, authentifie, dejaReessaye: true });
    }
    deconnecter();
  }

  const texte = await reponse.text();
  const donnees = texte ? JSON.parse(texte) : null;
  if (!reponse.ok) {
    const erreur = new Error(resumerErreur(donnees) || `HTTP ${reponse.status}`);
    erreur.status = reponse.status;
    erreur.corps = donnees;
    throw erreur;
  }
  return donnees;
}

function resumerErreur(donnees) {
  if (!donnees) return null;
  if (donnees.detail) return donnees.detail;
  if (donnees.non_field_errors) return donnees.non_field_errors.join(' ');
  // Erreur de validation par champ : {"email": ["..."], ...}
  const premiereCle = Object.keys(donnees)[0];
  if (premiereCle && Array.isArray(donnees[premiereCle])) {
    return `${premiereCle}: ${donnees[premiereCle].join(' ')}`;
  }
  return null;
}

async function tenterRafraichissement() {
  try {
    const reponse = await fetch(`${API_URL}/api/auth/refresh/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh: jetons.refresh }),
    });
    if (!reponse.ok) return false;
    const donnees = await reponse.json();
    sauvegarderJetons({ access: donnees.access, refresh: donnees.refresh || jetons.refresh });
    return true;
  } catch {
    return false;
  }
}

export async function connecter(email, password) {
  const donnees = await appel('/api/auth/login/', { method: 'POST', body: { email, password } });
  sauvegarderJetons(donnees.tokens);
  return donnees.user;
}

export async function inscrire({ email, password, fullName }) {
  const donnees = await appel('/api/auth/register/', {
    method: 'POST',
    body: {
      email, password, password_confirmation: password,
      full_name: fullName, accepts_terms: true,
    },
  });
  sauvegarderJetons(donnees.tokens);
  return donnees.user;
}

export function rechercherLieux(q, position) {
  const params = new URLSearchParams({ q });
  if (position) {
    params.set('lat', position.lat);
    params.set('lon', position.lon);
  }
  return appel(`/api/places/search/?${params}`);
}

export function statutValhalla() {
  return appel('/api/dev/valhalla/status/');
}

export function calculerItineraire(depart, arrivee, avoid = []) {
  return appel('/api/routes/calculate/', {
    method: 'POST',
    authentifie: true,
    body: {
      origin_lat: depart.lat, origin_lon: depart.lon,
      destination_lat: arrivee.lat, destination_lon: arrivee.lon,
      avoid: avoid.map((p) => ({ lat: p.lat, lon: p.lon })),
    },
  });
}

export function config() {
  return appel('/api/config/');
}

export function incidentsProches(cellules) {
  return appel(`/api/incidents/nearby/?cells=${cellules.join(',')}`);
}

export function signalerIncident({ type, lat, lon }) {
  return appel('/api/incidents/', {
    method: 'POST',
    authentifie: true,
    headers: { 'Idempotency-Key': crypto.randomUUID() },
    body: { type, lat, lon },
  });
}

export function voterIncident(id, direction) {
  return appel(`/api/incidents/${id}/vote/`, {
    method: 'POST',
    authentifie: true,
    body: { direction },
  });
}

export function retirerIncident(id) {
  return appel(`/api/incidents/${id}/`, { method: 'DELETE', authentifie: true });
}
