// Decodeur du polyline encode par Valhalla (precision 6, cf. trip.legs[].shape).
// Algorithme standard "Google polyline" avec un facteur de precision 1e6 au
// lieu de 1e5 -- aucune lib externe necessaire pour ca.
export function decoderPolyline6(encode) {
  let index = 0;
  let lat = 0;
  let lon = 0;
  const points = [];

  while (index < encode.length) {
    let shift = 0;
    let resultat = 0;
    let octet;
    do {
      octet = encode.charCodeAt(index++) - 63;
      resultat |= (octet & 0x1f) << shift;
      shift += 5;
    } while (octet >= 0x20);
    lat += (resultat & 1) ? ~(resultat >> 1) : (resultat >> 1);

    shift = 0;
    resultat = 0;
    do {
      octet = encode.charCodeAt(index++) - 63;
      resultat |= (octet & 0x1f) << shift;
      shift += 5;
    } while (octet >= 0x20);
    lon += (resultat & 1) ? ~(resultat >> 1) : (resultat >> 1);

    points.push([lat / 1e6, lon / 1e6]);
  }
  return points;
}
