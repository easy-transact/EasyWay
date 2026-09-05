import { useEffect, useState } from 'react';
import DataTable from '../components/DataTable';
import { bannirUtilisateur, debannirUtilisateur, listerUtilisateurs } from '../api';
import { Search, ShieldBan, ShieldCheck } from 'lucide-react';

const COLONNES = [
  { key: 'phone', label: 'Téléphone' },
  { key: 'full_name', label: 'Nom' },
  { key: 'email', label: 'Email' },
  { key: 'city', label: 'Ville' },
  { key: 'plan', label: 'Plan', render: (u) => <span className={`badge plan-${u.plan.toLowerCase()}`}>{u.plan}</span> },
  { key: 'reputation_score', label: 'Réputation', render: (u) => <strong>{u.reputation_score}</strong> },
  {
    key: 'is_banned',
    label: 'Statut',
    render: (u) => (u.is_banned ? <span className="badge badge-danger">Banni{u.banned_until ? ` jusqu'au ${new Date(u.banned_until).toLocaleDateString()}` : ' (permanent)'}</span> : <span className="badge badge-success">Actif</span>),
  },
];

export default function UtilisateursPage() {
  const [recherche, setRecherche] = useState('');
  const [utilisateurs, setUtilisateurs] = useState([]);
  const [chargement, setChargement] = useState(true);
  const [erreur, setErreur] = useState(null);
  const [jusquAParId, setJusquAParId] = useState({});

  async function recharger(search = recherche) {
    setChargement(true);
    setErreur(null);
    try {
      const donnees = await listerUtilisateurs({ search: search || undefined });
      setUtilisateurs(donnees.results);
    } catch (e) {
      setErreur(e.message);
    } finally {
      setChargement(false);
    }
  }

  useEffect(() => {
    recharger('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function soumettreRecherche(evenement) {
    evenement.preventDefault();
    recharger();
  }

  async function bannir(id) {
    try {
      await bannirUtilisateur(id, jusquAParId[id]);
      recharger();
    } catch (e) {
      setErreur(e.message);
    }
  }

  async function debannir(id) {
    try {
      await debannirUtilisateur(id);
      recharger();
    } catch (e) {
      setErreur(e.message);
    }
  }

  return (
    <section className="page-container">
      <div className="page-header">
        <div>
          <h2>Gestion des Utilisateurs</h2>
          <p className="subtitle">Gérez les comptes, les bannissements et la réputation des utilisateurs.</p>
        </div>
        <div className="header-filters">
          <form className="search-bar" onSubmit={soumettreRecherche}>
            <Search size={18} className="text-muted" />
            <input 
              placeholder="Téléphone / Nom / Email" 
              value={recherche} 
              onChange={(e) => setRecherche(e.target.value)} 
              className="input-search"
            />
            <button type="submit" className="btn-secondary">Rechercher</button>
          </form>
        </div>
      </div>
      
      <div className="card">
        {erreur && <div className="erreur">{erreur}</div>}
        {chargement ? (
          <p className="chargement">Chargement des utilisateurs...</p>
        ) : (
          <DataTable
            columns={COLONNES}
            rows={utilisateurs}
            renderActions={(utilisateur) =>
              utilisateur.is_staff ? (
                <span className="badge badge-staff">Compte staff</span>
              ) : utilisateur.is_banned ? (
                <button className="btn-success" onClick={() => debannir(utilisateur.id)}>
                  <ShieldCheck size={16} /> Débannir
                </button>
              ) : (
                <div className="actions-ligne">
                  <input
                    type="date"
                    value={jusquAParId[utilisateur.id] || ''}
                    onChange={(e) =>
                      setJusquAParId((precedent) => ({ ...precedent, [utilisateur.id]: e.target.value }))
                    }
                  />
                  <button className="btn-danger" onClick={() => bannir(utilisateur.id)}>
                    <ShieldBan size={16} /> Bannir
                  </button>
                </div>
              )
            }
          />
        )}
      </div>
    </section>
  );
}
