import { useEffect, useState } from 'react';
import DataTable from '../components/DataTable';
import { bannirUtilisateur, debannirUtilisateur, listerUtilisateurs } from '../api';

const COLONNES = [
  { key: 'phone', label: 'Telephone' },
  { key: 'full_name', label: 'Nom' },
  { key: 'email', label: 'Email' },
  { key: 'city', label: 'Ville' },
  { key: 'plan', label: 'Plan' },
  { key: 'reputation_score', label: 'Reputation' },
  {
    key: 'is_banned',
    label: 'Statut',
    render: (u) => (u.is_banned ? `Banni${u.banned_until ? ` jusqu'au ${new Date(u.banned_until).toLocaleDateString()}` : ' (permanent)'}` : 'Actif'),
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
    <section>
      <h2>Utilisateurs</h2>
      <form className="barre-outils" onSubmit={soumettreRecherche}>
        <label>
          Recherche (telephone / nom / email)
          <input value={recherche} onChange={(e) => setRecherche(e.target.value)} />
        </label>
        <button type="submit">Rechercher</button>
      </form>
      {erreur && <p className="erreur">{erreur}</p>}
      {chargement ? (
        <p className="chargement">Chargement...</p>
      ) : (
        <DataTable
          columns={COLONNES}
          rows={utilisateurs}
          renderActions={(utilisateur) =>
            utilisateur.is_staff ? (
              <span>Compte staff</span>
            ) : utilisateur.is_banned ? (
              <button onClick={() => debannir(utilisateur.id)}>Debannir</button>
            ) : (
              <div className="actions-ligne">
                <input
                  type="date"
                  value={jusquAParId[utilisateur.id] || ''}
                  onChange={(e) =>
                    setJusquAParId((precedent) => ({ ...precedent, [utilisateur.id]: e.target.value }))
                  }
                />
                <button onClick={() => bannir(utilisateur.id)}>Bannir</button>
              </div>
            )
          }
        />
      )}
    </section>
  );
}
