"""
Pilotage (back-office, staff) : ce que les donnees deja recues disent de la
qualite du service. Lecture seule, rien n'est persiste ici.

  GET /api/staff/insights/eta/                fiabilite des temps de trajet
  GET /api/staff/insights/moderation-queue/   signalements a traiter en priorite
  GET /api/staff/insights/route-proposals/    propositions better-route et acceptation
  GET /api/staff/insights/data-density/       volume de donnees (trajets, passages, tronçons)
  GET /api/staff/insights/live-map/           signalements actifs + vitesses des 15 dernieres min
  GET /api/staff/insights/live-vehicles/      vehicules en direct, points anonymes (admins seulement)
"""

import statistics
from collections import Counter, defaultdict
from datetime import timedelta

import h3
from django.contrib.gis.geos import Polygon
from django.contrib.gis.measure import D
from django.db.models import Avg, Count
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import BasePermission, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from community.models import Incident, StatutIncident
from etat_routes.models import NoteTroncon, PassageTroncon, Troncon
from etat_routes.services.reglages import reglages
from etat_routes.services.troncons import classe_attendue

from .models import FUSEAU_TRAFIC, PropositionItineraire, StatutTrajet, Trajet
from .services import positions_direct, retards_signalements, risque_itineraire
from .services.duree_ville import AGGLOMERATIONS
from .services.geo import distance_haversine_m

ECART_ABERRANT = (0.2, 5.0)   # duree reelle / prevue hors de cet intervalle : trajet interrompu, pas une erreur de prevision
NB_TRAJETS_MAX = 5000
COULOIR_TRAJETS_M = 50
NB_INCIDENTS_FILE = 300
FENETRE_DIRECT_MIN = 15
CONDUCTEURS_MIN_ZONE = 2      # anonymat : une zone avec un seul conducteur n'est pas affichee


def _jours(request, defaut, maximum=180):
    try:
        return min(max(int(request.query_params.get('days', defaut)), 1), maximum)
    except ValueError:
        return defaut


def _zone(point):
    if point is None:
        return 'Inconnue'
    for nom, lat, lon, rayon in AGGLOMERATIONS:
        if distance_haversine_m((point.y, point.x), (lat, lon)) <= rayon * 1000:
            return nom
    return 'Hors ville'


def _bilan(ecarts):
    """ecarts : (reelle - prevue) / prevue. Erreur mediane (absolue), biais
    (median, signe : positif = trajets plus longs que prevu), part a +/- 20 %."""
    if not ecarts:
        return {'count': 0, 'median_error_pct': None, 'bias_pct': None, 'within_20_pct': None}
    return {
        'count': len(ecarts),
        'median_error_pct': round(statistics.median(abs(e) for e in ecarts) * 100, 1),
        'bias_pct': round(statistics.median(ecarts) * 100, 1),
        'within_20_pct': round(sum(1 for e in ecarts if abs(e) <= 0.2) / len(ecarts) * 100, 1),
    }


class FiabiliteTempsView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(tags=['Staff - Pilotage'], summary='Fiabilite des temps de trajet (prevu vs reel)')
    def get(self, request):
        jours = _jours(request, 30)
        trajets = list(
            Trajet.objects.filter(
                statut=StatutTrajet.TERMINE, termine_le__gte=timezone.now() - timedelta(days=jours),
                duree_prevue__gt=0, duree_reelle__gt=0,
            ).order_by('-termine_le').values(
                'id', 'libelle_origine', 'libelle_destination', 'duree_prevue', 'duree_reelle',
                'distance_prevue', 'demarre_le', 'termine_le', 'position_origine', 'type_vehicule',
            )[:NB_TRAJETS_MAX]
        )
        retenus, ignores = [], 0
        for t in trajets:
            ratio = t['duree_reelle'] / t['duree_prevue']
            if not ECART_ABERRANT[0] <= ratio <= ECART_ABERRANT[1]:
                ignores += 1
                continue
            t['ecart'] = ratio - 1
            debut = t['demarre_le'] or t['termine_le'] - timedelta(seconds=t['duree_reelle'])
            t['heure'] = debut.astimezone(FUSEAU_TRAFIC).hour
            t['zone'] = _zone(t['position_origine'])
            retenus.append(t)

        def par(cle):
            groupes = defaultdict(list)
            for t in retenus:
                groupes[t[cle] or 'VOITURE'].append(t['ecart'])
            return [{'key': k, **_bilan(v)} for k, v in sorted(groupes.items(), key=lambda g: str(g[0]))]

        pires = sorted(retenus, key=lambda t: -abs(t['ecart']))[:15]
        return Response({
            'days': jours,
            'ignored_interrupted': ignores,
            'summary': _bilan([t['ecart'] for t in retenus]),
            'by_hour': par('heure'),
            'by_zone': par('zone'),
            'by_vehicle': par('type_vehicule'),
            'worst': [
                {
                    'trip_id': str(t['id']), 'origin': t['libelle_origine'], 'destination': t['libelle_destination'],
                    'planned_s': t['duree_prevue'], 'actual_s': t['duree_reelle'], 'distance_m': t['distance_prevue'],
                    'error_pct': round(t['ecart'] * 100, 1), 'zone': t['zone'], 'ended_at': t['termine_le'],
                }
                for t in pires
            ],
        })


class FileModerationView(APIView):
    """Signalements actifs pas encore valides, du plus urgent au moins urgent :
    gravite x conducteurs concernes (trajets en cours qui passent a moins de
    50 m) x doute (un signalement deja tres confirme est moins urgent)."""

    permission_classes = [IsAdminUser]

    @extend_schema(tags=['Staff - Pilotage'], summary='Signalements a traiter en priorite')
    def get(self, request):
        maintenant = timezone.now()
        incidents = Incident.objects.filter(
            statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE], expire_le__gt=maintenant,
            valide_par_staff=False,
        ).select_related('auteur').order_by('-cree_le')[:NB_INCIDENTS_FILE]
        actifs = Trajet.objects.filter(statut=StatutTrajet.ACTIF, geometrie__isnull=False)
        lignes = []
        for incident in incidents:
            gravite = risque_itineraire.gravite_de(incident)
            fiabilite = retards_signalements.confiance(incident)
            concernes = actifs.filter(geometrie__dwithin=(incident.position, D(m=COULOIR_TRAJETS_M))).count()
            lignes.append({
                'id': str(incident.id), 'type': incident.type, 'subtype': incident.sous_type or '',
                'status': incident.statut, 'lat': incident.position.y, 'lon': incident.position.x,
                'created_at': incident.cree_le, 'expires_at': incident.expire_le,
                'severity': gravite, 'confidence': round(fiabilite, 2),
                'confirmations': incident.confirmations, 'denials': incident.infirmations,
                'author_reputation': incident.auteur.score_reputation if incident.auteur_id else None,
                'active_trips_nearby': concernes,
                'score': round(max(gravite, 0.5) * (1 + concernes) * (1.5 - fiabilite), 1),
            })
        lignes.sort(key=lambda l: -l['score'])
        return Response({'results': lignes})


class PropositionsView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(tags=['Staff - Pilotage'], summary='Propositions better-route et taux d acceptation')
    def get(self, request):
        jours = _jours(request, 30)
        depuis = timezone.now() - timedelta(days=jours)
        propositions = PropositionItineraire.objects.filter(cree_le__gte=depuis)

        par_cause = []
        for cause, libelle in PropositionItineraire.Cause.choices:
            lot = propositions.filter(cause=cause)
            total = lot.count()
            acceptees = lot.filter(acceptee=True).count()
            moyennes = lot.aggregate(gain=Avg('gain_s'), evites=Avg('nb_dangers_evites'))
            par_cause.append({
                'cause': cause, 'label': libelle, 'count': total, 'accepted': acceptees,
                'acceptance_pct': round(acceptees / total * 100, 1) if total else None,
                'avg_time_saved_s': round(moyennes['gain']) if moyennes['gain'] is not None else None,
                'avg_hazards_avoided': round(moyennes['evites'], 1) if moyennes['evites'] is not None else None,
            })

        par_jour = defaultdict(lambda: {'count': 0, 'accepted': 0})
        for cree_le, acceptee in propositions.values_list('cree_le', 'acceptee'):
            jour = par_jour[cree_le.astimezone(FUSEAU_TRAFIC).date().isoformat()]
            jour['count'] += 1
            jour['accepted'] += int(acceptee)

        recentes = propositions.select_related('trajet').order_by('-cree_le')[:30]
        return Response({
            'days': jours,
            'by_cause': par_cause,
            'by_day': [{'date': d, **v} for d, v in sorted(par_jour.items())],
            'recent': [
                {
                    'created_at': p.cree_le, 'action': p.action, 'cause': p.cause, 'time_saved_s': p.gain_s,
                    'risk_before': p.risque_avant, 'risk_after': p.risque_apres,
                    'hazards_avoided': p.nb_dangers_evites, 'accepted': p.acceptee, 'trip_id': str(p.trajet_id),
                    'trip': f'{p.trajet.libelle_origine} → {p.trajet.libelle_destination}',
                }
                for p in recentes
            ],
        })


class DensiteDonneesView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(tags=['Staff - Pilotage'], summary='Volume de donnees recues')
    def get(self, request):
        jours = _jours(request, 7, maximum=90)
        maintenant = timezone.now()
        depuis = maintenant - timedelta(days=jours)
        reg = reglages()

        trajets_heure = Counter()
        conducteurs_jour = defaultdict(set)
        for demarre_le, utilisateur_id in Trajet.objects.filter(demarre_le__gte=depuis).values_list(
            'demarre_le', 'utilisateur_id',
        ):
            local = demarre_le.astimezone(FUSEAU_TRAFIC)
            trajets_heure[local.hour] += 1
            conducteurs_jour[local.date().isoformat()].add(utilisateur_id)

        passages_heure, conducteurs_heure = Counter(), defaultdict(set)
        for horodatage, conducteur in PassageTroncon.objects.filter(horodatage__gte=depuis).values_list(
            'horodatage', 'conducteur',
        ).iterator():
            heure = horodatage.astimezone(FUSEAU_TRAFIC).hour
            passages_heure[heure] += 1
            conducteurs_heure[heure].add(conducteur)

        fenetre = maintenant - timedelta(days=reg['fenetre_vitesse_j'])
        mesurables = PassageTroncon.objects.filter(horodatage__gte=fenetre, heure_creuse=True).values('troncon').annotate(
            n=Count('id'), c=Count('conducteur', distinct=True),
        ).filter(n__gte=reg['passages_min'], c__gte=reg['conducteurs_min_vitesse']).count()

        try:
            from .services.producteur_evenements import connexion_redis_telemetrie
            from .services.trafic_temps_reel import CLE_ARETES_PUBLIEES

            aretes_temps_reel = connexion_redis_telemetrie().zcard(CLE_ARETES_PUBLIEES)
        except Exception:
            aretes_temps_reel = None

        return Response({
            'days': jours,
            'active_trips_now': Trajet.objects.filter(statut=StatutTrajet.ACTIF).count(),
            'trips_started': sum(trajets_heure.values()),
            'drivers_by_day': [{'date': d, 'drivers': len(s)} for d, s in sorted(conducteurs_jour.items())],
            'by_hour': [
                {'hour': h, 'trips_started': trajets_heure[h], 'passages': passages_heure[h],
                 'drivers': len(conducteurs_heure[h])}
                for h in range(24)
            ],
            'segments': {
                'known': Troncon.objects.count(),
                'driven_in_period': PassageTroncon.objects.filter(horodatage__gte=depuis).values('troncon').distinct().count(),
                'with_night_speed': mesurables,
                'by_class': dict(NoteTroncon.objects.values_list('classe').annotate(n=Count('troncon_id'))),
            },
            'edges_with_live_speed': aretes_temps_reel,
            'active_reports': dict(
                Incident.objects.filter(
                    statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE], expire_le__gt=maintenant,
                ).values_list('type').annotate(n=Count('id'))
            ),
            'thresholds': {
                'speed_passages_min': reg['passages_min'], 'speed_drivers_min': reg['conducteurs_min_vitesse'],
                'alert_drivers_min': reg['conducteurs_min_alerte'],
            },
        })


class EstAdmin(BasePermission):
    """Compte administrateur (is_superuser) : les positions individuelles,
    meme anonymes, ne sont pas montrees aux moderateurs."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


class VehiculesDirectView(APIView):
    """Vehicules en trajet vus depuis moins de 2 min : points anonymes
    (identifiant du jour, type, vitesse, cap), bbox facultative. Admins seulement."""

    permission_classes = [EstAdmin]

    @extend_schema(tags=['Staff - Pilotage'], summary='Vehicules en direct (anonymes, admins)')
    def get(self, request):
        try:
            bbox = [float(request.query_params[k]) for k in ('south', 'west', 'north', 'east')]
        except (KeyError, ValueError):
            bbox = None
        return Response({'updated_at': timezone.now(), 'vehicles': positions_direct.lire(bbox)})


class CarteDirectView(APIView):
    """bbox optionnelle (south, west, north, east) : signalements actifs, vitesse
    moyenne par tranche de route sur les 15 dernieres minutes (rapport a la
    vitesse attendue de sa classe), conducteurs par zone H3 (>= 2, anonymat)."""

    permission_classes = [IsAdminUser]

    @extend_schema(tags=['Staff - Pilotage'], summary='Carte en direct (signalements + vitesses)')
    def get(self, request):
        maintenant = timezone.now()
        incidents = Incident.objects.filter(
            statut__in=[StatutIncident.ACTIF, StatutIncident.EN_ATTENTE], expire_le__gt=maintenant,
        )
        passages = PassageTroncon.objects.filter(horodatage__gte=maintenant - timedelta(minutes=FENETRE_DIRECT_MIN))
        try:
            bbox = [float(request.query_params[k]) for k in ('south', 'west', 'north', 'east')]
        except (KeyError, ValueError):
            bbox = None
        if bbox:
            rectangle = Polygon.from_bbox((bbox[1], bbox[0], bbox[3], bbox[2]))
            rectangle.srid = 4326
            incidents = incidents.filter(position__intersects=rectangle)
            passages = passages.filter(troncon__geometrie__intersects=rectangle)

        vitesses = list(
            passages.values('troncon').annotate(v=Avg('vitesse_kmh'), n=Count('id'), c=Count('conducteur', distinct=True))[:3000]
        )
        troncons = Troncon.objects.in_bulk([v['troncon'] for v in vitesses])
        attendues = reglages()['vitesse_attendue_kmh']
        segments = []
        for v in vitesses:
            troncon = troncons.get(v['troncon'])
            if troncon is None:
                continue
            attendue = attendues[classe_attendue(troncon.classe_route)]
            segments.append({
                'id': troncon.id, 'street': troncon.nom_voie, 'speed_kmh': round(v['v']),
                'expected_kmh': attendue, 'ratio': round(v['v'] / attendue, 2), 'drivers': v['c'],
                'coords': [[lat, lon] for lon, lat in troncon.geometrie.coords],
            })

        par_zone = defaultdict(set)
        for cellule, trajet in passages.values_list('troncon__cellule_h3_res8', 'trajet_id'):
            par_zone[h3.cell_to_parent(h3.int_to_str(cellule), 7)].add(trajet)
        zones = []
        for cellule, trajets in par_zone.items():
            if len(trajets) >= CONDUCTEURS_MIN_ZONE:
                lat, lon = h3.cell_to_latlng(cellule)
                zones.append({'cell': cellule, 'lat': lat, 'lon': lon, 'drivers': len(trajets),
                              'boundary': [list(p) for p in h3.cell_to_boundary(cellule)]})

        return Response({
            'updated_at': maintenant,
            'window_min': FENETRE_DIRECT_MIN,
            'incidents': [
                {
                    'id': str(i.id), 'type': i.type, 'subtype': i.sous_type or '', 'lat': i.position.y,
                    'lon': i.position.x, 'severity': risque_itineraire.gravite_de(i),
                    'confidence': round(retards_signalements.confiance(i), 2), 'validated': i.valide_par_staff,
                    'created_at': i.cree_le,
                }
                for i in incidents.order_by('-cree_le')[:1000]
            ],
            'segments': segments,
            'zones': zones,
        })
