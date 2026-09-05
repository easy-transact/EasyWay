import { useEffect, useState } from 'react';
import DataTable from '../components/DataTable';
import { listerIncidents, retirerIncident } from '../api';
import { Filter, Trash2 } from 'lucide-react';

const COLONNES = [
  { key: 'type', label: 'Type' },
  { key: 'subtype', label: 'Sous-type' },
  { key: 'city', label: 'Ville' },
  { key: 'street_name', label: 'Voie' },
  { key: 'confirmations', label: 'Confirmations' },
  { key: 'disputes', label: 'Infirmations' },
  { key: 'author_phone', label: 'Auteur' },
  { key: 'created_at', label: 'Créé le', render: (i) => new Date(i.created_at).toLocaleString() },
];

export default function IncidentsPage() {
  const [statut, setStatut] = useState('');
  const [incidents, setIncidents] = useState([]);
  const [chargement, setChargement] = useState(true);
  const [erreur, setErreur] = useState(null);
  const [motifParId, setMotifParId] = useState({});

  async function recharger() {
    setChargement(true);
    setErreur(null);
    try {
      const donnees = await listerIncidents(statut || undefined);
      setIncidents(donnees.results);
    } catch (e) {
      setErreur(e.message);
    } finally {
      setChargement(false);
    }
  }

  useEffect(() => {
    recharger();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statut]);

  async function retirer(id) {
    const motif = motifParId[id];
    if (!motif) {
      setErreur('Un motif de retrait est requis.');
      return;
    }
    try {
      await retirerIncident(id, motif);
      recharger();
    } catch (e) {
      setErreur(e.message);
    }
  }

  return (
    <section className="page-container">
      <div className="page-header">
        <div>
          <h2>Gestion des Incidents</h2>
          <p className="subtitle">Consultez et modérez les incidents signalés sur la carte.</p>
        </div>
        <div className="header-filters">
          <div className="filter-group">
            <Filter size={18} className="text-muted" />
            <select value={statut} onChange={(e) => setStatut(e.target.value)} className="select-modern">
              <option value="">Actifs + en attente</option>
              <option value="RETIRE">Retirés</option>
              <option value="EXPIRE">Expirés</option>
            </select>
          </div>
        </div>
      </div>
      
      <div className="card">
        {erreur && <div className="erreur">{erreur}</div>}
        {chargement ? (
          <p className="chargement">Chargement des incidents...</p>
        ) : (
          <DataTable
            columns={COLONNES}
            rows={incidents}
            renderActions={
              statut === ''
                ? (incident) => (
                    <div className="actions-ligne">
                      <input
                        placeholder="Motif de retrait"
                        value={motifParId[incident.id] || ''}
                        onChange={(e) =>
                          setMotifParId((precedent) => ({ ...precedent, [incident.id]: e.target.value }))
                        }
                      />
                      <button className="btn-danger" onClick={() => retirer(incident.id)}>
                        <Trash2 size={16} /> Retirer
                      </button>
                    </div>
                  )
                : undefined
            }
          />
        )}
      </div>
    </section>
  );
}
