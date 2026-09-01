import { NavLink, useNavigate } from 'react-router-dom';
import { deconnecter } from '../api';

export default function Layout({ children }) {
  const navigate = useNavigate();

  function seDeconnecter() {
    deconnecter();
    navigate('/login', { replace: true });
  }

  return (
    <div className="mise-en-page">
      <nav className="nav-laterale">
        <h1>EasyWay BO</h1>
        <NavLink to="/places" className={({ isActive }) => (isActive ? 'lien-actif' : '')}>
          Lieux
        </NavLink>
        <NavLink to="/incidents" className={({ isActive }) => (isActive ? 'lien-actif' : '')}>
          Incidents
        </NavLink>
        <NavLink to="/users" className={({ isActive }) => (isActive ? 'lien-actif' : '')}>
          Utilisateurs
        </NavLink>
        <button className="bouton-deconnexion" onClick={seDeconnecter}>
          Deconnexion
        </button>
      </nav>
      <main className="contenu-principal">{children}</main>
    </div>
  );
}
