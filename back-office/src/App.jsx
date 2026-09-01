import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import Layout from './components/Layout';
import RequireStaff from './RequireStaff';
import LoginPage from './pages/LoginPage';
import LieuxPage from './pages/LieuxPage';
import IncidentsPage from './pages/IncidentsPage';
import UtilisateursPage from './pages/UtilisateursPage';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/places"
          element={
            <RequireStaff>
              <Layout>
                <LieuxPage />
              </Layout>
            </RequireStaff>
          }
        />
        <Route
          path="/incidents"
          element={
            <RequireStaff>
              <Layout>
                <IncidentsPage />
              </Layout>
            </RequireStaff>
          }
        />
        <Route
          path="/users"
          element={
            <RequireStaff>
              <Layout>
                <UtilisateursPage />
              </Layout>
            </RequireStaff>
          }
        />
        <Route path="*" element={<Navigate to="/places" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
