import { useEffect, useState } from 'react';
import DataTable from '../components/DataTable';
import { listerIncidents, retirerIncident } from '../api';

const COLONNES = [
  { key: 'type', label: 'Type' },
  { key: 'subtype', label: 'Sous-type' },
  { key: 'city', label: 'Ville' },
  { key: 'street_name', label: 'Voie' },
  { key: 'confirmations', label: 'Confirmations' },
  { key: 'disputes', label: 'Infirmations' },
  { key: 'author_phone', label: 'Auteur' },
  { key: 'created_at', label: 'Cree le', render: (i) => new Date(i.created_at).toLocaleString() },
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
    <section>
      <h2>Incidents</h2>
      <div className="barre-outils">
        <label>
          Statut
          <select value={statut} onChange={(e) => setStatut(e.target.value)}>
            <option value="">Actifs + en attente</option>
            <option value="RETIRE">Retires</option>
            <option value="EXPIRE">Expires</option>
          </select>
        </label>
      </div>
      {erreur && <p className="erreur">{erreur}</p>}
      {chargement ? (
        <p className="chargement">Chargement...</p>
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
                    <button onClick={() => retirer(incident.id)}>Retirer</button>
                  </div>
                )
              : undefined
          }
        />
      )}
    </section>
  );
}
