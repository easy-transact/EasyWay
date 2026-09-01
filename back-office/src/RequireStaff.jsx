import { useEffect, useState } from 'react';
import { Navigate } from 'react-router-dom';
import { estConnecte, verifierAccesStaff } from './api';

export default function RequireStaff({ children }) {
  const [etat, setEtat] = useState(estConnecte() ? 'verification' : 'non-connecte');

  useEffect(() => {
    if (etat !== 'verification') return;
    verifierAccesStaff()
      .then(() => setEtat('autorise'))
      .catch((erreur) => setEtat(erreur.status === 403 ? 'refuse' : 'non-connecte'));
  }, [etat]);

  if (etat === 'non-connecte') return <Navigate to="/login" replace />;
  if (etat === 'verification') return <p className="chargement">Verification des droits...</p>;
  if (etat === 'refuse') {
    return (
      <div className="acces-refuse">
        <p>Ce compte n'a pas les droits staff necessaires pour le back-office.</p>
      </div>
    );
  }
  return children;
}
