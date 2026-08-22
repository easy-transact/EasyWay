"""Encodage/decodage du polyline Valhalla (precision 6). Port Python du meme
algorithme que test-ui/src/polyline.js -- garde les deux synchronises si l'un
des deux change."""


def decoder_polyline6(encode: str) -> list[tuple[float, float]]:
    """Retourne une liste de (lon, lat) -- ordre attendu par GEOSGeometry."""
    index = 0
    lat = 0
    lon = 0
    points = []

    while index < len(encode):
        shift, resultat = 0, 0
        while True:
            octet = ord(encode[index]) - 63
            index += 1
            resultat |= (octet & 0x1f) << shift
            shift += 5
            if octet < 0x20:
                break
        delta_lat = ~(resultat >> 1) if resultat & 1 else (resultat >> 1)
        lat += delta_lat

        shift, resultat = 0, 0
        while True:
            octet = ord(encode[index]) - 63
            index += 1
            resultat |= (octet & 0x1f) << shift
            shift += 5
            if octet < 0x20:
                break
        delta_lon = ~(resultat >> 1) if resultat & 1 else (resultat >> 1)
        lon += delta_lon

        points.append((lon / 1e6, lat / 1e6))
    return points


def encoder_polyline6(points: list[tuple[float, float]]) -> str:
    """points : liste de (lon, lat). Utilise par le repli du circuit breaker
    pour produire une geometrie dans le meme format que Valhalla."""
    def encoder_valeur(valeur):
        valeur = ~(valeur << 1) if valeur < 0 else (valeur << 1)
        resultat = ''
        while valeur >= 0x20:
            resultat += chr((0x20 | (valeur & 0x1f)) + 63)
            valeur >>= 5
        resultat += chr(valeur + 63)
        return resultat

    resultat = []
    lat_precedente = 0
    lon_precedente = 0
    for lon, lat in points:
        lat_e6 = round(lat * 1e6)
        lon_e6 = round(lon * 1e6)
        resultat.append(encoder_valeur(lat_e6 - lat_precedente))
        resultat.append(encoder_valeur(lon_e6 - lon_precedente))
        lat_precedente = lat_e6
        lon_precedente = lon_e6
    return ''.join(resultat)
