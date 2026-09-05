import { NavLink, useNavigate } from 'react-router-dom';
import { deconnecter } from '../api';
import { LayoutDashboard, MapPin, AlertTriangle, Users, LogOut, Bell, User } from 'lucide-react';

export default function Layout({ children }) {
  const navigate = useNavigate();

  function seDeconnecter() {
    deconnecter();
    navigate('/login', { replace: true });
  }

  return (
    <div className="mise-en-page">
      <nav className="nav-laterale">
        <div className="logo-container">
          <div className="logo-icon"><LayoutDashboard size={24} /></div>
          <h1>EasyWay BO</h1>
        </div>
        <div className="nav-links">
          <NavLink to="/places" className={({ isActive }) => (isActive ? 'lien-actif' : '')}>
            <MapPin size={20} /> Lieux
          </NavLink>
          <NavLink to="/incidents" className={({ isActive }) => (isActive ? 'lien-actif' : '')}>
            <AlertTriangle size={20} /> Incidents
          </NavLink>
          <NavLink to="/users" className={({ isActive }) => (isActive ? 'lien-actif' : '')}>
            <Users size={20} /> Utilisateurs
          </NavLink>
        </div>
        <button className="bouton-deconnexion" onClick={seDeconnecter}>
          <LogOut size={18} /> Déconnexion
        </button>
      </nav>
      
      <div className="main-wrapper">
        <header className="top-header">
           <div className="header-search">
              {/* Future breadcrumbs or search */}
           </div>
           <div className="header-actions">
              <button className="icon-btn"><Bell size={20} /></button>
              <div className="avatar">
                <User size={20} />
              </div>
           </div>
        </header>
        <main className="contenu-principal">
          {children}
        </main>
      </div>
    </div>
  );
}
