// Table generique reutilisee par les 3 pages de moderation (Lieux/Incidents/
// Utilisateurs) : columns = [{key, label}], renderActions(row) optionnel.
export default function DataTable({ columns, rows, renderActions, cleLigne = 'id' }) {
  if (rows.length === 0) {
    return <p className="table-vide">Rien a afficher.</p>;
  }

  return (
    <table className="table-donnees">
      <thead>
        <tr>
          {columns.map((colonne) => (
            <th key={colonne.key}>{colonne.label}</th>
          ))}
          {renderActions && <th>Actions</th>}
        </tr>
      </thead>
      <tbody>
        {rows.map((ligne) => (
          <tr key={ligne[cleLigne]}>
            {columns.map((colonne) => (
              <td key={colonne.key}>{colonne.render ? colonne.render(ligne) : ligne[colonne.key]}</td>
            ))}
            {renderActions && <td>{renderActions(ligne)}</td>}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
