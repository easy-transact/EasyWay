"""
GET /api/road-conditions/?cells=              carte (public), tronçons DEGRADE/MAUVAIS + inondables
GET /api/staff/road-conditions/ranking/       classement pour les autorites (json, csv, geojson)
GET/PATCH /api/staff/road-conditions/settings/  valeurs de depart (back-office)
"""

import csv
import hashlib
import io
from datetime import timedelta

import h3
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, BasePermission, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from ads_admin.services import journaliser
from places.models import Ville
from places.utils import normaliser
from trips.polyline import encoder_polyline6

from .models import ClasseEtat, HistoriqueNote, NoteTroncon, ReglagesEtatRoutes
from .services.reglages import DEFAUTS, reglages

MAX_CELLULES = 50
DUREE_CACHE_S = 3600  # les notes ne changent qu'une fois par nuit
RAYON_VILLE_DEG = 0.2  # ~22 km autour du centre de la ville
CLASSES_AFFICHEES = (ClasseEtat.DEGRADE, ClasseEtat.MAUVAIS)


def _geometrie(troncon):
    return encoder_polyline6(list(troncon.geometrie.coords))


def _ligne_carte(note):
    return {
        'segment_id': note.troncon_id,
        'geometry': _geometrie(note.troncon),
        'score': note.note,
        'class': note.classe if note.classe in CLASSES_AFFICHEES else None,
        'confidence': note.confiance,
        'causes': note.causes,
        'flood_prone': note.inondable,
        'updated_at': note.mis_a_jour_le,
    }


@extend_schema(
    tags=['Road conditions'],
    summary='Etat des routes sur la carte',
    description=(
        f"cells = cellules H3 resolution 8 (comme /api/incidents/nearby/), {MAX_CELLULES} maximum. "
        "Ne renvoie que les tronçons DEGRADE ou MAUVAIS, et les tronçons inondables "
        "(class null s'ils ne sont pas degrades). geometry : polyligne precision 6. "
        'Reponse stable une heure (calcul nocturne).'
    ),
    parameters=[OpenApiParameter('cells', OpenApiTypes.STR, required=True)],
)
class EtatRoutesCarteView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        cellules = sorted({c.strip().lower() for c in request.query_params.get('cells', '').split(',') if c.strip()})
        if not cellules:
            return Response({'detail': "'cells' is required."}, status=status.HTTP_400_BAD_REQUEST)
        if len(cellules) > MAX_CELLULES:
            return Response(
                {'detail': f'Too many cells requested ({len(cellules)}), maximum {MAX_CELLULES}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        invalides = [c for c in cellules if not h3.is_valid_cell(c) or h3.get_resolution(c) != 8]
        if invalides:
            return Response(
                {'detail': f'Invalid or non resolution-8 H3 cells: {", ".join(invalides[:5])}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        cle = 'etat-routes:carte:' + hashlib.sha256(','.join(cellules).encode()).hexdigest()
        donnees = cache.get(cle)
        if donnees is None:
            notes = NoteTroncon.objects.filter(
                Q(classe__in=CLASSES_AFFICHEES) | Q(inondable=True),
                troncon__cellule_h3_res8__in=[h3.str_to_int(c) for c in cellules],
            ).select_related('troncon')
            donnees = [_ligne_carte(n) for n in notes]
            cache.set(cle, donnees, DUREE_CACHE_S)
        return Response(donnees)


class PeutExporterClassement(BasePermission):
    """Staff ET permission Django dediee (etat_routes.exporter_classement),
    jamais un compte ordinaire."""

    message = "Permission 'etat_routes.exporter_classement' required."

    def has_permission(self, request, view):
        utilisateur = request.user
        return bool(
            utilisateur and utilisateur.is_authenticated and utilisateur.is_staff
            and utilisateur.has_perm('etat_routes.exporter_classement')
        )


def _tendance(troncon_id, note_actuelle, maintenant):
    """IMPROVING / STABLE / WORSENING sur ~90 jours, null sans historique."""
    ancienne = (
        HistoriqueNote.objects.filter(troncon_id=troncon_id, semaine__lte=(maintenant - timedelta(days=84)).date())
        .order_by('-semaine').values_list('note', flat=True).first()
    )
    if ancienne is None or note_actuelle is None:
        return None
    ecart = note_actuelle - ancienne
    return 'IMPROVING' if ecart >= 5 else 'WORSENING' if ecart <= -5 else 'STABLE'


@extend_schema(
    tags=['Road conditions'],
    summary='Classement des pires tronçons (autorites)',
    description=(
        "Reserve au staff avec la permission etat_routes.exporter_classement. Tronçons DEGRADE "
        "et MAUVAIS observes par au moins N conducteurs (anonymat), tries par "
        "(100 - score) x longueur x confiance. city = nom d'une ville (rayon ~22 km). "
        'format = json (defaut), csv ou geojson. Aucun identifiant ni trajet : des agregats.'
    ),
    parameters=[
        OpenApiParameter('city', OpenApiTypes.STR),
        OpenApiParameter('format', OpenApiTypes.STR, description='json | csv | geojson'),
        OpenApiParameter('limit', OpenApiTypes.INT, description='Defaut 200, max 2000.'),
    ],
)
class ClassementView(APIView):
    permission_classes = [PeutExporterClassement]

    def perform_content_negotiation(self, request, force=False):
        # ?format= est reserve par DRF (renderer) : csv/geojson inconnus
        # donneraient 404. Ici format choisit l'export, gere par get().
        return super().perform_content_negotiation(request, force=True)

    def get(self, request):
        reg = reglages()
        notes = NoteTroncon.objects.filter(
            classe__in=CLASSES_AFFICHEES, nb_conducteurs__gte=reg['conducteurs_min_classement'],
        ).select_related('troncon')
        ville = request.query_params.get('city', '').strip()
        if ville:
            centre = Ville.objects.filter(nom_normalise=normaliser(ville)).order_by('-population').first()
            if centre is None:
                return Response({'detail': f'Unknown city: {ville}.'}, status=status.HTTP_400_BAD_REQUEST)
            notes = notes.filter(
                troncon__geometrie__dwithin=(Point(centre.position.x, centre.position.y, srid=4326), RAYON_VILLE_DEG),
            )
        try:
            limite = min(max(int(request.query_params.get('limit', 200)), 1), 2000)
        except ValueError:
            limite = 200
        maintenant = timezone.now()
        lignes = sorted(
            notes, key=lambda n: -(100 - (n.note or 0)) * n.troncon.longueur_m * n.confiance,
        )[:limite]
        resultats = [
            {
                'segment_id': n.troncon_id, 'way_id': n.troncon.way_id, 'street': n.troncon.nom_voie,
                'score': n.note, 'class': n.classe, 'causes': n.causes, 'confidence': n.confiance,
                'length_m': round(n.troncon.longueur_m), 'confirmed_reports': n.nb_signalements_confirmes,
                'flood_prone': n.inondable, 'trend_90d': _tendance(n.troncon_id, n.note, maintenant),
                'last_observation': n.derniere_observation,
                'lat': n.troncon.geometrie.centroid.y, 'lon': n.troncon.geometrie.centroid.x,
                '_coords': list(n.troncon.geometrie.coords),
            }
            for n in lignes
        ]
        format_ = request.query_params.get('format', 'json')
        if format_ == 'csv':
            return self._csv(resultats)
        if format_ == 'geojson':
            return Response({
                'type': 'FeatureCollection',
                'features': [
                    {
                        'type': 'Feature',
                        'geometry': {'type': 'LineString', 'coordinates': r.pop('_coords')},
                        'properties': r,
                    }
                    for r in resultats
                ],
            })
        for r in resultats:
            r['geometry'] = encoder_polyline6(r.pop('_coords'))
        return Response({'count': len(resultats), 'results': resultats})

    @staticmethod
    def _csv(resultats):
        tampon = io.StringIO()
        colonnes = [
            'segment_id', 'street', 'class', 'score', 'causes', 'confidence', 'length_m',
            'confirmed_reports', 'flood_prone', 'trend_90d', 'last_observation', 'lat', 'lon', 'way_id',
        ]
        ecrivain = csv.DictWriter(tampon, fieldnames=colonnes, extrasaction='ignore', delimiter=';')
        ecrivain.writeheader()
        for r in resultats:
            ecrivain.writerow({**r, 'causes': ','.join(r['causes'])})
        reponse = HttpResponse(tampon.getvalue(), content_type='text/csv; charset=utf-8')
        reponse['Content-Disposition'] = 'attachment; filename="etat-des-routes.csv"'
        return reponse


@extend_schema(
    tags=['Road conditions'],
    summary='Valeurs de depart de la note (back-office)',
    description=(
        'GET : valeurs en vigueur (values), valeurs par defaut (defaults) et resume par classe. '
        'PATCH {values: {...}} : remplace les surcharges (cles inconnues refusees). '
        'Pris en compte au prochain calcul nocturne.'
    ),
)
class ReglagesView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        resume = {c: NoteTroncon.objects.filter(classe=c).count() for c in ClasseEtat.values}
        resume['flood_prone'] = NoteTroncon.objects.filter(inondable=True).count()
        return Response({'values': reglages(), 'defaults': DEFAUTS, 'summary': resume})

    def patch(self, request):
        valeurs = request.data.get('values')
        if not isinstance(valeurs, dict):
            return Response({'values': ["Expected an object."]}, status=status.HTTP_400_BAD_REQUEST)
        inconnues = sorted(set(valeurs) - set(DEFAUTS))
        if inconnues:
            return Response({'values': [f'Unknown keys: {", ".join(inconnues)}.']}, status=status.HTTP_400_BAD_REQUEST)
        ligne = ReglagesEtatRoutes.objects.order_by('id').first() or ReglagesEtatRoutes()
        avant = dict(ligne.valeurs or {})
        ligne.valeurs, ligne.modifie_par = valeurs, request.user
        ligne.save()
        journaliser(request.user, 'road_conditions.settings', ligne, libelle='Etat des routes', avant=avant, apres=valeurs)
        return Response({'values': reglages(), 'defaults': DEFAUTS})
