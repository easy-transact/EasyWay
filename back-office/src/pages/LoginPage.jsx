import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { connecter } from '../api';

export default function LoginPage() {
  const [phone, setPhone] = useState('');
  const [password, setPassword] = useState('');
  const [erreur, setErreur] = useState(null);
  const [enCours, setEnCours] = useState(false);
  const navigate = useNavigate();

  async function soumettre(evenement) {
    evenement.preventDefault();
    setErreur(null);
    setEnCours(true);
    try {
      await connecter(phone, password);
      navigate('/places', { replace: true });
    } catch (e) {
      setErreur(e.message);
    } finally {
      setEnCours(false);
    }
  }

  return (
    <div className="page-connexion">
      <form onSubmit={soumettre}>
        <h1>EasyWay Back-office</h1>
        <label>
          Telephone
          <input value={phone} onChange={(e) => setPhone(e.target.value)} autoFocus required />
        </label>
        <label>
          Mot de passe
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        {erreur && <p className="erreur">{erreur}</p>}
        <button type="submit" disabled={enCours}>
          {enCours ? 'Connexion...' : 'Se connecter'}
        </button>
      </form>
    </div>
  );
}
