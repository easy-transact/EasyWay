import { useEffect, useState } from 'react';
import DataTable from '../components/DataTable';
import { approuverLieu, listerLieux, rejeterLieu } from '../api';
import { Filter, CheckCircle2, XCircle } from 'lucide-react';

const COLONNES = [
  { key: 'name', label: 'Nom' },
  { key: 'category', label: 'Catégorie' },
  { key: 'city', label: 'Ville' },
  { key: 'neighborhood', label: 'Quartier' },
  { key: 'proposed_by', label: 'Proposé par', render: (l) => l.proposed_by || '-' },
];

export default function LieuxPage() {
  const [statut, setStatut] = useState('EN_ATTENTE');
  const [lieux, setLieux] = useState([]);
  const [chargement, setChargement] = useState(true);
  const [erreur, setErreur] = useState(null);
  const [motifParId, setMotifParId] = useState({});

  async function recharger() {
    setChargement(true);
    setErreur(null);
    try {
      const donnees = await listerLieux(statut);
      setLieux(donnees.results);
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

  async function approuver(id) {
    try {
      await approuverLieu(id);
      recharger();
    } catch (e) {
      setErreur(e.message);
    }
  }

  async function rejeter(id) {
    const motif = motifParId[id];
    if (!motif) {
      setErreur('Un motif de rejet est requis.');
      return;
    }
    try {
      await rejeterLieu(id, motif);
      recharger();
    } catch (e) {
      setErreur(e.message);
    }
  }

  return (
    <section className="page-container">
      <div className="page-header">
        <div>
          <h2>Gestion des Lieux</h2>
          <p className="subtitle">Approuvez ou rejetez les lieux proposés par la communauté.</p>
        </div>
        <div className="header-filters">
          <div className="filter-group">
            <Filter size={18} className="text-muted" />
            <select value={statut} onChange={(e) => setStatut(e.target.value)} className="select-modern">
              <option value="EN_ATTENTE">En attente</option>
              <option value="APPROUVE">Approuvé</option>
              <option value="REJETE">Rejeté</option>
            </select>
          </div>
        </div>
      </div>

      <div className="card">
        {erreur && <div className="erreur">{erreur}</div>}
        {chargement ? (
          <p className="chargement">Chargement des lieux...</p>
        ) : (
          <DataTable
            columns={COLONNES}
            rows={lieux}
            renderActions={
              statut === 'EN_ATTENTE'
                ? (lieu) => (
                    <div className="actions-ligne">
                      <button className="btn-success" onClick={() => approuver(lieu.id)}>
                        <CheckCircle2 size={16} /> Approuver
                      </button>
                      <input
                        placeholder="Motif de rejet"
                        value={motifParId[lieu.id] || ''}
                        onChange={(e) =>
                          setMotifParId((precedent) => ({ ...precedent, [lieu.id]: e.target.value }))
                        }
                      />
                      <button className="btn-danger" onClick={() => rejeter(lieu.id)}>
                        <XCircle size={16} /> Rejeter
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
