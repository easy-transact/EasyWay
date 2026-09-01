// Client API : ne parle qu'au backend Django. Meme forme que test-ui/src/api.js
// (wrapper fetch, refresh JWT sur 401) -- mais /api/auth/login/ attend
// {phone, password}, pas {email, password} (ConnexionSerializer, accounts/serializers.py).

const API_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';
const CLE_STOCKAGE_JETONS = 'easyway_bo_tokens';

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

async function appel(path, { method = 'GET', body, headers = {}, dejaReessaye = false } = {}) {
  const entetes = { ...headers };
  if (body !== undefined) entetes['Content-Type'] = 'application/json';
  if (jetons) entetes['Authorization'] = `Bearer ${jetons.access}`;

  const reponse = await fetch(`${API_URL}${path}`, {
    method,
    headers: entetes,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  // Access token expire (15 min) : une seule tentative de rafraichissement
  // automatique, jamais de boucle si le refresh lui-meme est invalide/expire.
  if (reponse.status === 401 && !dejaReessaye && jetons?.refresh) {
    const rafraichi = await tenterRafraichissement();
    if (rafraichi) {
      return appel(path, { method, body, headers, dejaReessaye: true });
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

export async function connecter(phone, password) {
  const donnees = await appel('/api/auth/login/', { method: 'POST', body: { phone, password } });
  sauvegarderJetons(donnees.tokens);
  return donnees.user;
}

// Aucun champ is_staff dans la reponse de login (UtilisateurSerializer ne
// l'expose pas -- partage avec l'app mobile) : on verifie le staff via un
// vrai appel a un endpoint reserve, page_size=1 pour rester leger.
export async function verifierAccesStaff() {
  await appel('/api/staff/places/?page_size=1');
}

export function listerLieux(status, page = 1) {
  const params = new URLSearchParams({ page: String(page) });
  if (status) params.set('status', status);
  return appel(`/api/staff/places/?${params}`);
}

export function approuverLieu(id) {
  return appel(`/api/staff/places/${id}/approve/`, { method: 'POST' });
}

export function rejeterLieu(id, reason) {
  return appel(`/api/staff/places/${id}/reject/`, { method: 'POST', body: { reason } });
}

export function listerIncidents(status, page = 1) {
  const params = new URLSearchParams({ page: String(page) });
  if (status) params.set('status', status);
  return appel(`/api/staff/incidents/?${params}`);
}

export function retirerIncident(id, reason) {
  return appel(`/api/staff/incidents/${id}/remove/`, { method: 'POST', body: { reason } });
}

export function listerUtilisateurs({ search, banned, page = 1 } = {}) {
  const params = new URLSearchParams({ page: String(page) });
  if (search) params.set('search', search);
  if (banned !== undefined) params.set('banned', String(banned));
  return appel(`/api/staff/users/?${params}`);
}

export function bannirUtilisateur(id, until) {
  return appel(`/api/staff/users/${id}/ban/`, { method: 'POST', body: { until: until || null } });
}

export function debannirUtilisateur(id) {
  return appel(`/api/staff/users/${id}/unban/`, { method: 'POST' });
}
