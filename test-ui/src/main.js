import 'leaflet/dist/leaflet.css';
import L from 'leaflet';
import './style.css';
import { rechercherLieux, statutValhalla, calculerItineraire } from './api.js';
import { decoderPolyline6 } from './polyline.js';

document.querySelector('#app').innerHTML = `
  <div id="panneau">
    <h1>EasyWay -- console de test</h1>
    <p class="sous-titre">
      Recherche de lieux (Django + PostGIS trigram) et calcul d'itineraire
      (Django proxie Valhalla -- le navigateur ne parle jamais a Valhalla directement).
    </p>

    <h2>Statut Valhalla <span id="badge-valhalla"></span></h2>

    <h2>Recherche de lieux</h2>
    <input type="text" id="q" placeholder="Ex: Marche, Palais, Hopital..." autocomplete="off">
    <ul class="resultats" id="resultats"></ul>

    <h2>Exemples d'itineraire</h2>
    <p class="sous-titre">
      Le nombre d'itineraires depend du trajet choisi -- Valhalla ne propose
      une option supplementaire que si un chemin reellement different existe.
      Ces trois exemples donnent 1, 1 et 3 itineraires respectivement.
    </p>
    <div style="display:flex; flex-direction:column; gap:.4rem;">
      <button class="secondaire" id="exemple-axe">1 itineraire -- un seul axe (Marche Central -> Marche Central de Douala)</button>
      <button class="secondaire" id="exemple-quartier">1 itineraire -- interieur de quartier (Bonamoussadi, rues locales)</button>
      <button class="secondaire" id="exemple-triple">3 itineraires -- carrefours multiples (Marche Central -> Hopital General)</button>
    </div>

    <h2>Itineraire</h2>
    <div class="slot" id="slot-depart"><div class="label">Depart</div><div id="depart-contenu">-- choisir un resultat --</div></div>
    <div class="slot" id="slot-arrivee"><div class="label">Arrivee</div><div id="arrivee-contenu">-- choisir un resultat --</div></div>
    <div style="display:flex; gap:.4rem;">
      <button id="btn-itineraire" disabled>Calculer l'itineraire</button>
      <button class="secondaire" id="btn-reset">Reinitialiser</button>
    </div>
    <div id="resume-itineraire"></div>
    <div id="detail-itineraire"></div>
  </div>
  <div id="carte"></div>
`;

const DOUALA = [4.0483, 9.7043];
const carte = L.map('carte').setView(DOUALA, 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap contributors',
  maxZoom: 19,
}).addTo(carte);

const marqueursResultats = L.layerGroup().addTo(carte);
let marqueurDepart = null;
let marqueurArrivee = null;
let tracéItineraire = null;

let depart = null;
let arrivee = null;

async function verifierValhalla() {
  const badge = document.getElementById('badge-valhalla');
  badge.innerHTML = '<span class="badge">...</span>';
  try {
    const data = await statutValhalla();
    badge.innerHTML = data.ok
      ? `<span class="badge ok">UP</span> <span style="color:#999;font-size:.75rem">v${data.version}</span>`
      : '<span class="badge err">DOWN</span>';
  } catch {
    badge.innerHTML = '<span class="badge err">INJOIGNABLE</span>';
  }
}

function icone(couleur) {
  return L.divIcon({
    className: '',
    html: `<div style="width:14px;height:14px;border-radius:50%;background:${couleur};border:2px solid #fff;box-shadow:0 0 3px rgba(0,0,0,.5)"></div>`,
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  });
}

let debounce;
function surRecherche(e) {
  clearTimeout(debounce);
  const q = e.target.value.trim();
  debounce = setTimeout(() => rechercher(q), 250);
}

async function rechercher(q) {
  const ul = document.getElementById('resultats');
  marqueursResultats.clearLayers();
  if (q.length < 2) { ul.innerHTML = ''; return; }

  const centre = carte.getCenter();
  const resultats = await rechercherLieux(q, { lat: centre.lat, lon: centre.lng });

  ul.innerHTML = resultats.length === 0 ? '<li>Aucun resultat.</li>' : '';
  for (const lieu of resultats) {
    const li = document.createElement('li');
    li.className = 'lieu';
    const estExterne = lieu.source === 'nominatim';
    li.innerHTML = `
      <div class="nom">${lieu.libelle} <span class="badge-source ${estExterne ? 'externe' : ''}">${estExterne ? 'Nominatim' : 'local'}</span></div>
      <div class="meta">${lieu.categorie} -- ${lieu.sous_libelle || ''}${lieu.distance_m !== null ? ` -- ${lieu.distance_m} m` : ''}</div>
      <div class="actions">
        <button class="secondaire btn-depart">Depart</button>
        <button class="secondaire btn-arrivee">Arrivee</button>
      </div>
    `;
    li.querySelector('.btn-depart').onclick = () => definirPoint('depart', lieu);
    li.querySelector('.btn-arrivee').onclick = () => definirPoint('arrivee', lieu);
    ul.appendChild(li);

    L.marker([lieu.lat, lieu.lon], { icon: icone(estExterne ? '#e08a1e' : '#4477ff') })
      .bindPopup(`<b>${lieu.libelle}</b><br>${lieu.categorie} (${estExterne ? 'Nominatim' : 'local'})`)
      .addTo(marqueursResultats);
  }
  if (resultats.length > 0) {
    const groupe = L.featureGroup(marqueursResultats.getLayers());
    carte.fitBounds(groupe.getBounds().pad(0.3));
  }
}

function definirPoint(role, lieu) {
  if (role === 'depart') {
    depart = lieu;
    if (marqueurDepart) carte.removeLayer(marqueurDepart);
    marqueurDepart = L.marker([lieu.lat, lieu.lon], { icon: icone('#2a8f2a') }).addTo(carte);
  } else {
    arrivee = lieu;
    if (marqueurArrivee) carte.removeLayer(marqueurArrivee);
    marqueurArrivee = L.marker([lieu.lat, lieu.lon], { icon: icone('#d92b2b') }).addTo(carte);
  }
  document.getElementById(`${role}-contenu`).innerHTML =
    `<b>${lieu.libelle}</b><br>lat ${lieu.lat.toFixed(5)}, lon ${lieu.lon.toFixed(5)}`;
  document.getElementById(`slot-${role}`).classList.add('rempli');
  document.getElementById('btn-itineraire').disabled = !(depart && arrivee);
}

function reinitialiser() {
  depart = null; arrivee = null;
  for (const role of ['depart', 'arrivee']) {
    document.getElementById(`${role}-contenu`).textContent = '-- choisir un resultat --';
    document.getElementById(`slot-${role}`).classList.remove('rempli');
  }
  document.getElementById('btn-itineraire').disabled = true;
  document.getElementById('resume-itineraire').innerHTML = '';
  document.getElementById('detail-itineraire').innerHTML = '';
  if (marqueurDepart) { carte.removeLayer(marqueurDepart); marqueurDepart = null; }
  if (marqueurArrivee) { carte.removeLayer(marqueurArrivee); marqueurArrivee = null; }
  if (tracéItineraire) { carte.removeLayer(tracéItineraire); tracéItineraire = null; }
  tracésAlternatifs.forEach((t) => carte.removeLayer(t));
  tracésAlternatifs = [];
  routes = [];
}

let routes = [];
let tracésAlternatifs = [];

async function surCalculItineraire() {
  const resume = document.getElementById('resume-itineraire');
  const detail = document.getElementById('detail-itineraire');
  resume.textContent = 'Calcul en cours...';
  detail.innerHTML = '';

  let data;
  try {
    data = await calculerItineraire(depart, arrivee);
  } catch (e) {
    // Sans ce catch, une exception ici (reseau, JSON invalide, etc.) laissait
    // "Calcul en cours..." affiche indefiniment sans aucun retour visible --
    // l'erreur reelle n'apparaissait que dans la console du navigateur.
    console.error('calculerItineraire a echoue :', e);
    resume.innerHTML = `<span class="badge err">ECHEC</span> ${e.message || e}`;
    return;
  }

  if (!data.ok) {
    resume.innerHTML = `<span class="badge err">ECHEC</span> ${data.erreur}`;
    return;
  }

  routes = data.routes;
  try {
    afficherChoixRoutes();
    selectionnerRoute(0);
  } catch (e) {
    console.error('affichage de l\'itineraire a echoue :', e);
    resume.innerHTML = `<span class="badge err">ECHEC</span> erreur d'affichage -- voir la console`;
  }
}

function afficherChoixRoutes() {
  const resume = document.getElementById('resume-itineraire');
  if (routes.length === 1) {
    resume.innerHTML = '<span class="badge ok">OK</span> 1 itineraire trouve (pas d\'alternative distincte sur ce trajet)';
    return;
  }
  const options = routes
    .map((trip, i) => `
      <button class="secondaire btn-route" data-index="${i}" style="margin:.2rem .3rem .2rem 0">
        ${trip.summary.length.toFixed(1)} km -- ${Math.round(trip.summary.time / 60)} min
      </button>
    `)
    .join('');
  resume.innerHTML = `<span class="badge ok">OK</span> ${routes.length} itineraires -- <span style="color:#888;font-size:.78rem">cliquez pour comparer :</span><div>${options}</div>`;
  resume.querySelectorAll('.btn-route').forEach((btn) => {
    btn.addEventListener('click', () => selectionnerRoute(Number(btn.dataset.index)));
  });
}

function selectionnerRoute(indexChoisi) {
  const detail = document.getElementById('detail-itineraire');

  if (tracéItineraire) carte.removeLayer(tracéItineraire);
  tracésAlternatifs.forEach((t) => carte.removeLayer(t));
  tracésAlternatifs = [];

  routes.forEach((trip, i) => {
    const points = trip.legs.flatMap((leg) => decoderPolyline6(leg.shape));
    if (i === indexChoisi) {
      tracéItineraire = L.polyline(points, { color: '#2255dd', weight: 5, opacity: 0.9 }).addTo(carte);
    } else {
      const alt = L.polyline(points, { color: '#999', weight: 3, opacity: 0.55, dashArray: '6 6' })
        .addTo(carte)
        .on('click', () => selectionnerRoute(i));
      tracésAlternatifs.push(alt);
    }
  });
  carte.fitBounds(tracéItineraire.getBounds().pad(0.2));

  const boutons = document.querySelectorAll('.btn-route');
  boutons.forEach((btn) => btn.classList.toggle('choisi', Number(btn.dataset.index) === indexChoisi));

  const tripChoisi = routes[indexChoisi];
  detail.innerHTML = tripChoisi.legs
    .flatMap((leg) => leg.maneuvers)
    .map((m) => `<div class="maneuver">${m.instruction}<div class="distance">${m.length.toFixed(2)} km</div></div>`)
    .join('');
}

function chargerExemple(lieuDepart, lieuArrivee) {
  reinitialiser();
  definirPoint('depart', lieuDepart);
  definirPoint('arrivee', lieuArrivee);
  surCalculItineraire();
}

document.getElementById('q').addEventListener('input', surRecherche);
document.getElementById('btn-itineraire').addEventListener('click', surCalculItineraire);
document.getElementById('btn-reset').addEventListener('click', reinitialiser);

// Verifie empiriquement (voir la conversation) : un seul axe possible entre
// ces deux points -- Valhalla ne propose aucune alternative meme en la
// demandant explicitement, quels que soient les costing_options essayes.
document.getElementById('exemple-axe').addEventListener('click', () => chargerExemple(
  { libelle: 'Marche Central', lat: 4.04830, lon: 9.70430 },
  { libelle: 'Marché Central de Douala', lat: 4.03588, lon: 9.70465 },
));

// Deux points a l'interieur de Bonamoussadi, sans passer par un axe partage --
// l'itineraire emprunte des rues locales uniquement (ex. "Rue 5.080").
document.getElementById('exemple-quartier').addEventListener('click', () => chargerExemple(
  { libelle: 'Bonamoussadi', lat: 4.094354, lon: 9.7393663 },
  { libelle: 'TOTAL Bonamoussadi 1', lat: 4.0864022, lon: 9.7345574 },
));

// Trajet avec un rond-point et plusieurs choix de rues valides -- Valhalla y
// trouve 3 itineraires distincts (verifie a plusieurs reprises via curl).
document.getElementById('exemple-triple').addEventListener('click', () => chargerExemple(
  { libelle: 'Marche Central', lat: 4.0483, lon: 9.7043 },
  { libelle: 'Hôpital Général de Douala', lat: 4.0469, lon: 9.6970 },
));

verifierValhalla();
