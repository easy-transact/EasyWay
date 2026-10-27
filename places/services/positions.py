"""
Positions approximatives : certaines sources (Overture surtout) placent les
lieux sans adresse precise au centre de leur quartier. A Makepe, 69 lieux de
lieux_cameroun.csv partagent exactement le point (4.03587, 9.7581) --
garages, ferme, associations... "Y aller" y menerait au mauvais endroit.

Regle : une position (au millionieme de degre, ~10 cm) partagee par au moins
SEUIL_DOUBLONS lieux est marquee approximative pour tous ces lieux. Un lieu
deplace a la main par le staff n'est plus marque (cf. LieuSupprimerView.patch).
"""

from django.db import connection

SEUIL_DOUBLONS = 3

# Aussi utilisee telle quelle par la migration 0010 (pas d'import d'app dans
# une migration) : garder les deux en phase.
SQL_MARQUER = """
WITH points AS (
    SELECT id, round(ST_Y(position::geometry)::numeric, 6) AS lat,
               round(ST_X(position::geometry)::numeric, 6) AS lon
    FROM lieu
), doublons AS (
    SELECT lat, lon FROM points GROUP BY lat, lon HAVING count(*) >= %(seuil)s
), cible AS (
    SELECT points.id, (doublons.lat IS NOT NULL) AS approximative
    FROM points LEFT JOIN doublons ON doublons.lat = points.lat AND doublons.lon = points.lon
)
UPDATE lieu SET position_approximative = cible.approximative
FROM cible
WHERE lieu.id = cible.id AND lieu.position_approximative <> cible.approximative
"""


def marquer_positions_approximatives():
    """Recalcule le marqueur pour toute la table ; retourne le nombre de lieux
    marques approximatifs."""
    with connection.cursor() as curseur:
        curseur.execute(SQL_MARQUER, {'seuil': SEUIL_DOUBLONS})
        curseur.execute('SELECT count(*) FROM lieu WHERE position_approximative')
        return curseur.fetchone()[0]
