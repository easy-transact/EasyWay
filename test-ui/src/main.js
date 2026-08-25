import 'leaflet/dist/leaflet.css';
import L from 'leaflet';
import * as h3 from 'h3-js';
import './style.css';
import {
  rechercherLieux, statutValhalla, calculerItineraire, config,
  connecter, inscrire, deconnecter, estConnecte,
  incidentsProches, signalerIncident, voterIncident, retirerIncident,
} from './api.js';
import { decoderPolyline6 } from './polyline.js';

const RESOLUTION_H3 = 8; // doit matcher community/models.py:RESOLUTION_H3_FIN cote backend

document.querySelector('#app').innerHTML = `
  <div id="panneau">
    <h1>EasyWay -- console de test</h1>
    <p class="sous-titre">
      Teste les vraies routes de l'API (pas de raccourci dev) : recherche de
      lieux, calcul d'itineraire authentifie (avec evitement de points reels),
      signalement d'incidents et confirmation qu'eviter un incident produit
      effectivement un trajet different.
    </p>

    <h2>Compte</h2>
    <div class="compte">
      <div class="ligne"><input type="email" id="email" placeholder="email" autocomplete="username"></div>
      <div class="ligne"><input type="password" id="mot-de-passe" placeholder="mot de passe" autocomplete="current-password"></div>
      <div class="ligne">
        <button id="btn-connexion">Se connecter</button>
        <button class="secondaire" id="btn-inscription">S'inscrire</button>
        <button class="secondaire" id="btn-deconnexion">Se deconnecter</button>
      </div>
      <div class="statut deconnecte" id="statut-compte">Non connecte -- requis pour itineraire/incidents.</div>
      <div class="erreur" id="erreur-compte"></div>
    </div>

    <h2>Statut Valhalla <span id="badge-valhalla"></span></h2>

    <h2>Recherche de lieux</h2>
    <input type="text" id="q" placeholder="Ex: Marche, Palais, Hopital..." autocomplete="off">
    <ul class="resultats" id="resultats"></ul>

    <h2>Itineraire</h2>
    <div class="slot" id="slot-depart"><div class="label">Depart</div><div id="depart-contenu">-- choisir un resultat --</div></div>
    <div class="slot" id="slot-arrivee"><div class="label">Arrivee</div><div id="arrivee-contenu">-- choisir un resultat --</div></div>
    <div class="label" style="font-size:.72rem;color:#999;text-transform:uppercase;letter-spacing:.04em;">Points evites</div>
    <div class="chip-liste" id="liste-evitement"><span style="color:#aaa;font-size:.78rem;">aucun</span></div>
    <div style="display:flex; gap:.4rem;">
      <button id="btn-itineraire" disabled>Calculer l'itineraire</button>
      <button class="secondaire" id="btn-reset">Reinitialiser</button>
    </div>
    <div id="resume-itineraire"></div>
    <div id="detail-itineraire"></div>

    <h2>Incidents</h2>
    <p class="sous-titre">Cellules H3 (res ${RESOLUTION_H3}) autour du centre de la carte, recalculees a chaque deplacement.</p>
    <select id="type-incident"></select>
    <div style="margin-top:.4rem;">
      <button class="mode-signalement" id="btn-signaler">Signaler un incident ici (clic sur la carte)</button>
    </div>
    <div class="statut deconnecte" id="statut-signalement" style="margin-top:.4rem;"></div>
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
const marqueursIncidents = L.layerGroup().addTo(carte);
let marqueurDepart = null;
let marqueurArrivee = null;
let tracéItineraire = null;

let depart = null;
let arrivee = null;
let evitement = []; // [{lat, lon, label}]
let typesIncident = {};

// ------------------------------------------------------------------
// Compte

function majStatutCompte() {
  const statut = document.getElementById('statut-compte');
  if (estConnecte()) {
    statut.textContent = 'Connecte.';
    statut.className = 'statut connecte';
  } else {
    statut.textContent = 'Non connecte -- requis pour itineraire/incidents.';
    statut.className = 'statut deconnecte';
  }
}

async function surConnexion() {
  const email = document.getElementById('email').value.trim();
  const motDePasse = document.getElementById('mot-de-passe').value;
  const erreur = document.getElementById('erreur-compte');
  erreur.textContent = '';
  try {
    await connecter(email, motDePasse);
    majStatutCompte();
  } catch (e) {
    erreur.textContent = e.message;
  }
}

async function surInscription() {
  const email = document.getElementById('email').value.trim();
  const motDePasse = document.getElementById('mot-de-passe').value;
  const erreur = document.getElementById('erreur-compte');
  erreur.textContent = '';
  try {
    await inscrire({ email, password: motDePasse, fullName: 'Test UI' });
    majStatutCompte();
  } catch (e) {
    erreur.textContent = e.message;
  }
}

function surDeconnexion() {
  deconnecter();
  majStatutCompte();
}

// ------------------------------------------------------------------
// Statut Valhalla (proxy dev -- inchange)

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

// ------------------------------------------------------------------
// Config publique (remplit le select des types d'incident)

async function chargerConfig() {
  try {
    const donnees = await config();
    typesIncident = donnees.incident_types || {};
    const select = document.getElementById('type-incident');
    select.innerHTML = Object.entries(typesIncident)
      .map(([code, libelle]) => `<option value="${code}">${libelle}</option>`)
      .join('');
  } catch (e) {
    console.error('GET /api/config/ a echoue :', e);
  }
}

// ------------------------------------------------------------------
// Recherche de lieux

function icone(couleur) {
  return L.divIcon({
    className: '',
    html: `<div style="width:14px;height:14px;border-radius:50%;background:${couleur};border:2px solid #fff;box-shadow:0 0 3px rgba(0,0,0,.5)"></div>`,
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  });
}

function iconeIncident() {
  return L.divIcon({
    className: '',
    html: '<div style="width:16px;height:16px;border-radius:3px;background:#d92b2b;border:2px solid #fff;box-shadow:0 0 3px rgba(0,0,0,.6);transform:rotate(45deg)"></div>',
    iconSize: [16, 16],
    iconAnchor: [8, 8],
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
    const estExterne = lieu.source !== 'local';
    li.innerHTML = `
      <div class="nom">${lieu.label} <span class="badge-source ${estExterne ? 'externe' : ''}">${lieu.source}</span></div>
      <div class="meta">${lieu.category} -- ${lieu.sublabel || ''}${lieu.distance_m !== null ? ` -- ${lieu.distance_m} m` : ''}</div>
      <div class="actions">
        <button class="secondaire btn-depart">Depart</button>
        <button class="secondaire btn-arrivee">Arrivee</button>
      </div>
    `;
    li.querySelector('.btn-depart').onclick = () => definirPoint('depart', lieu);
    li.querySelector('.btn-arrivee').onclick = () => definirPoint('arrivee', lieu);
    ul.appendChild(li);

    L.marker([lieu.lat, lieu.lon], { icon: icone(estExterne ? '#e08a1e' : '#4477ff') })
      .bindPopup(`<b>${lieu.label}</b><br>${lieu.category} (${lieu.source})`)
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
    `<b>${lieu.label}</b><br>lat ${lieu.lat.toFixed(5)}, lon ${lieu.lon.toFixed(5)}`;
  document.getElementById(`slot-${role}`).classList.add('rempli');
  document.getElementById('btn-itineraire').disabled = !(depart && arrivee);
}

function reinitialiser() {
  depart = null; arrivee = null; evitement = [];
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
  majListeEvitement();
}

// ------------------------------------------------------------------
// Calcul d'itineraire (vraie route authentifiee, avec evitement)

let routes = [];
let tracésAlternatifs = [];

function majListeEvitement() {
  const conteneur = document.getElementById('liste-evitement');
  if (evitement.length === 0) {
    conteneur.innerHTML = '<span style="color:#aaa;font-size:.78rem;">aucun</span>';
    return;
  }
  conteneur.innerHTML = evitement
    .map((p, i) => `<span class="chip">${p.label} <button data-index="${i}" title="retirer">&times;</button></span>`)
    .join('');
  conteneur.querySelectorAll('button').forEach((btn) => {
    btn.addEventListener('click', () => {
      evitement.splice(Number(btn.dataset.index), 1);
      majListeEvitement();
      if (depart && arrivee) surCalculItineraire();
    });
  });
}

function ajouterEvitement(point) {
  const dejaPresent = evitement.some((p) => p.lat === point.lat && p.lon === point.lon);
  if (dejaPresent) return;
  evitement.push(point);
  majListeEvitement();
}

async function surCalculItineraire() {
  const resume = document.getElementById('resume-itineraire');
  const detail = document.getElementById('detail-itineraire');
  resume.textContent = 'Calcul en cours...';
  detail.innerHTML = '';

  let data;
  try {
    data = await calculerItineraire(depart, arrivee, evitement);
  } catch (e) {
    console.error('calculerItineraire a echoue :', e);
    resume.innerHTML = `<span class="badge err">ECHEC</span> ${e.message}`;
    return;
  }

  routes = data;
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
  const suffixeEvitement = evitement.length ? ` -- en evitant ${evitement.length} point(s)` : '';
  if (routes.length === 1) {
    resume.innerHTML = `<span class="badge ok">OK</span> 1 itineraire trouve (pas d'alternative distincte sur ce trajet)${suffixeEvitement}`;
    return;
  }
  const options = routes
    .map((route, i) => `
      <button class="secondaire btn-route" data-index="${i}" style="margin:.2rem .3rem .2rem 0">
        ${(route.distance / 1000).toFixed(1)} km -- ${Math.round(route.duration / 60)} min
      </button>
    `)
    .join('');
  resume.innerHTML = `<span class="badge ok">OK</span> ${routes.length} itineraires${suffixeEvitement} -- <span style="color:#888;font-size:.78rem">cliquez pour comparer :</span><div>${options}</div>`;
  resume.querySelectorAll('.btn-route').forEach((btn) => {
    btn.addEventListener('click', () => selectionnerRoute(Number(btn.dataset.index)));
  });
}

function selectionnerRoute(indexChoisi) {
  const detail = document.getElementById('detail-itineraire');

  if (tracéItineraire) carte.removeLayer(tracéItineraire);
  tracésAlternatifs.forEach((t) => carte.removeLayer(t));
  tracésAlternatifs = [];

  routes.forEach((route, i) => {
    const points = decoderPolyline6(route.geometry);
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

  const route = routes[indexChoisi];
  const enteteTrafic = route.degraded
    ? '<div class="maneuver" style="color:#b8631a">Valhalla indisponible -- ligne droite degradee, pas un vrai trace routier.</div>'
    : `<div class="maneuver" style="color:#888">Trafic : ${route.traffic_level}${route.duration_with_traffic !== null ? ` (${Math.round(route.duration_with_traffic / 60)} min avec trafic)` : ''}</div>`;
  detail.innerHTML = enteteTrafic + route.maneuvers
    .map((m) => `<div class="maneuver">${m.instruction}<div class="distance">${(m.distance / 1000).toFixed(2)} km</div></div>`)
    .join('');
}

// ------------------------------------------------------------------
// Incidents

function cellulesAutourDuCentre() {
  const centre = carte.getCenter();
  const cellule = h3.latLngToCell(centre.lat, centre.lng, RESOLUTION_H3);
  return h3.gridDisk(cellule, 1); // la cellule centrale + son anneau -- couvre la vue sans surcharger l'appel
}

async function rafraichirIncidents() {
  try {
    const incidents = await incidentsProches(cellulesAutourDuCentre());
    marqueursIncidents.clearLayers();
    for (const incident of incidents) {
      const marqueur = L.marker([incident.lat, incident.lon], { icon: iconeIncident() });
      marqueur.bindPopup(contenuPopupIncident(incident));
      marqueur.on('popupopen', () => attacherActionsPopup(incident));
      marqueur.addTo(marqueursIncidents);
    }
  } catch (e) {
    console.error('GET /api/incidents/nearby/ a echoue :', e);
  }
}

function contenuPopupIncident(incident) {
  const libelleType = typesIncident[incident.type] || incident.type;
  return `
    <div style="font-size:.85rem;min-width:180px;">
      <b>${libelleType}</b>${incident.street_name ? ` -- ${incident.street_name}` : ''}<br>
      <span style="color:#888">${incident.status} -- confirmations ${incident.confirmations} / contestations ${incident.disputes}</span>
      <div style="display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.4rem;">
        <button class="secondaire" data-action="confirm" style="font-size:.72rem;padding:.25rem .5rem;">Confirmer</button>
        <button class="secondaire" data-action="dispute" style="font-size:.72rem;padding:.25rem .5rem;">Contester</button>
        <button class="secondaire" data-action="avoid" style="font-size:.72rem;padding:.25rem .5rem;">Eviter dans l'itineraire</button>
        <button class="secondaire" data-action="remove" style="font-size:.72rem;padding:.25rem .5rem;">Retirer</button>
      </div>
      <div class="erreur" data-role="erreur" style="margin-top:.3rem;"></div>
    </div>
  `;
}

function attacherActionsPopup(incident) {
  // Le contenu du popup n'existe dans le DOM qu'une fois ouvert -- d'ou le
  // rebranchement des ecouteurs a chaque popupopen plutot qu'a la creation.
  const popup = document.querySelector('.leaflet-popup-content');
  if (!popup) return;
  const erreur = popup.querySelector('[data-role=erreur]');

  const gerer = (action) => async () => {
    erreur.textContent = '';
    try {
      if (action === 'confirm' || action === 'dispute') {
        await voterIncident(incident.id, action);
      } else if (action === 'avoid') {
        ajouterEvitement({ lat: incident.lat, lon: incident.lon, label: `Incident: ${typesIncident[incident.type] || incident.type}` });
        if (depart && arrivee) await surCalculItineraire();
      } else if (action === 'remove') {
        await retirerIncident(incident.id);
      }
      carte.closePopup();
      rafraichirIncidents();
    } catch (e) {
      erreur.textContent = e.message;
    }
  };

  popup.querySelectorAll('[data-action]').forEach((btn) => {
    btn.addEventListener('click', gerer(btn.dataset.action));
  });
}

let modeSignalement = false;
function basculerModeSignalement() {
  modeSignalement = !modeSignalement;
  const bouton = document.getElementById('btn-signaler');
  const statut = document.getElementById('statut-signalement');
  bouton.classList.toggle('actif', modeSignalement);
  bouton.textContent = modeSignalement ? 'Cliquez sur la carte pour signaler...' : 'Signaler un incident ici (clic sur la carte)';
  statut.textContent = '';
}

async function surClicCarte(e) {
  if (!modeSignalement) return;
  basculerModeSignalement(); // un seul signalement par armement, evite les doublons accidentels
  const statut = document.getElementById('statut-signalement');
  const type = document.getElementById('type-incident').value;
  try {
    const incident = await signalerIncident({ type, lat: e.latlng.lat, lon: e.latlng.lng });
    statut.className = 'statut connecte';
    statut.textContent = `Signale : ${typesIncident[incident.type] || incident.type}${incident.duplicate_of_existing ? ' (fusionne avec un incident existant a proximite)' : ''}.`;
    rafraichirIncidents();
  } catch (err) {
    statut.className = 'statut deconnecte';
    statut.textContent = err.message;
  }
}

// ------------------------------------------------------------------

document.getElementById('btn-connexion').addEventListener('click', surConnexion);
document.getElementById('btn-inscription').addEventListener('click', surInscription);
document.getElementById('btn-deconnexion').addEventListener('click', surDeconnexion);
document.getElementById('q').addEventListener('input', surRecherche);
document.getElementById('btn-itineraire').addEventListener('click', surCalculItineraire);
document.getElementById('btn-reset').addEventListener('click', reinitialiser);
document.getElementById('btn-signaler').addEventListener('click', basculerModeSignalement);
carte.on('click', surClicCarte);

let debounceDeplacement;
carte.on('moveend', () => {
  clearTimeout(debounceDeplacement);
  debounceDeplacement = setTimeout(rafraichirIncidents, 300);
});

majStatutCompte();
verifierValhalla();
chargerConfig();
rafraichirIncidents();
