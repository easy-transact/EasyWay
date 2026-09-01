import { useEffect, useState } from 'react';
import DataTable from '../components/DataTable';
import { approuverLieu, listerLieux, rejeterLieu } from '../api';

const COLONNES = [
  { key: 'name', label: 'Nom' },
  { key: 'category', label: 'Categorie' },
  { key: 'city', label: 'Ville' },
  { key: 'neighborhood', label: 'Quartier' },
  { key: 'proposed_by', label: 'Propose par', render: (l) => l.proposed_by || '-' },
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
    <section>
      <h2>Lieux</h2>
      <div className="barre-outils">
        <label>
          Statut
          <select value={statut} onChange={(e) => setStatut(e.target.value)}>
            <option value="EN_ATTENTE">En attente</option>
            <option value="APPROUVE">Approuve</option>
            <option value="REJETE">Rejete</option>
          </select>
        </label>
      </div>
      {erreur && <p className="erreur">{erreur}</p>}
      {chargement ? (
        <p className="chargement">Chargement...</p>
      ) : (
        <DataTable
          columns={COLONNES}
          rows={lieux}
          renderActions={
            statut === 'EN_ATTENTE'
              ? (lieu) => (
                  <div className="actions-ligne">
                    <button onClick={() => approuver(lieu.id)}>Approuver</button>
                    <input
                      placeholder="Motif de rejet"
                      value={motifParId[lieu.id] || ''}
                      onChange={(e) =>
                        setMotifParId((precedent) => ({ ...precedent, [lieu.id]: e.target.value }))
                      }
                    />
                    <button onClick={() => rejeter(lieu.id)}>Rejeter</button>
                  </div>
                )
              : undefined
          }
        />
      )}
    </section>
  );
}
