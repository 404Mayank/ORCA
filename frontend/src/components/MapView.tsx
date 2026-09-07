import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import type { Recommendation } from "../types";

/**
 * The map. Draws only what the answer actually contains.
 *
 * Every marker here comes from `spatial_context` or from a claim's slots --
 * never from a coordinate invented in the browser. If the answer carries no
 * position, the map shows the study box and nothing else, which is the honest
 * rendering of "we do not know where you are".
 */

const BBOX: [number, number, number, number] = [78.5, 8.0, 82.0, 12.0];

const VERDICT_COLOUR: Record<string, string> = {
  go: "#2fbf71",
  marginal: "#e6b422",
  no_go: "#e5484d",
};

interface Props {
  recommendation: Recommendation | null;
  /** IMBL polyline from GET /geo/boundaries: the same treaty line the
      geofence tool tests, drawn so the chart and the verdict agree. */
  imbl: Array<[number, number]> | null;
}

export default function MapView({ recommendation, imbl }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const markers = useRef<maplibregl.Marker[]>([]);
  const imblDrawn = useRef(false);
  const imblRef = useRef(imbl);
  imblRef.current = imbl;
  const routeDrawn = useRef(false);
  const recRef = useRef<Recommendation | null>(null);

  const clearRoute = () => {
    const m = map.current;
    if (!m) return;
    // getLayer/getSource guards: style may not be loaded on this tick.
    try {
      if (m.getLayer("route-line")) m.removeLayer("route-line");
      if (m.getSource("route")) m.removeSource("route");
    } catch {
      /* style not ready -- nothing drawn yet */
    }
    routeDrawn.current = false;
  };

  const drawRoute = (rec: Recommendation | null) => {
    const m = map.current;
    const points = rec?.route?.waypoints;
    if (!m || !points || points.length < 2 || routeDrawn.current) return;
    if (!m.isStyleLoaded()) return;
    // Coordinates come ONLY from rec.route.waypoints -- the verified tool
    // output -- never reconstructed, never from visual_layers.
    const coords = points.map((p) => [p.lon, p.lat]);
    m.addSource("route", {
      type: "geojson",
      data: {
        type: "Feature",
        properties: {},
        geometry: { type: "LineString", coordinates: coords },
      },
    });
    m.addLayer({
      id: "route-line", type: "line", source: "route",
      // Neutral blue solid: verdict hues stay exclusive to the origin
      // marker (VERDICT_COLOUR above). A shelter leg is guidance, not a
      // verdict, so it must not borrow the red/green vocabulary.
      paint: { "line-color": "#4a9eff", "line-width": 2.5 },
    });
    routeDrawn.current = true;
  };

  const drawImbl = () => {
    const m = map.current;
    const line = imblRef.current;
    if (!m || !line || line.length < 2 || imblDrawn.current) return;
    if (!m.isStyleLoaded()) return;
    m.addSource("imbl", {
      type: "geojson",
      data: {
        type: "Feature",
        properties: {},
        geometry: { type: "LineString", coordinates: line },
      },
    });
    m.addLayer({
      id: "imbl-line", type: "line", source: "imbl",
      paint: { "line-color": "#e5484d", "line-width": 2, "line-dasharray": [7, 5] },
    });
    imblDrawn.current = true;
  };

  useEffect(() => {
    if (!container.current || map.current) return;
    map.current = new maplibregl.Map({
      container: container.current,
      // Raster OSM rather than a vector style: no API key, and a demo that
      // needs a token is a demo that fails when the token expires.
      style: {
        version: 8,
        sources: {
          osm: {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            attribution: "© OpenStreetMap contributors",
          },
        },
        layers: [{ id: "osm", type: "raster", source: "osm" }],
      },
      center: [80.2, 10.2],
      zoom: 6.6,
    });
    map.current.addControl(new maplibregl.NavigationControl(), "top-left");
    map.current.on("load", () => {
      const m = map.current!;
      m.addSource("bbox", {
        type: "geojson",
        data: {
          type: "Feature",
          properties: {},
          geometry: {
            type: "Polygon",
            coordinates: [[
              [BBOX[0], BBOX[1]], [BBOX[2], BBOX[1]],
              [BBOX[2], BBOX[3]], [BBOX[0], BBOX[3]], [BBOX[0], BBOX[1]],
            ]],
          },
        },
      });
      m.addLayer({
        id: "bbox-line", type: "line", source: "bbox",
        paint: { "line-color": "#4a9eff", "line-width": 1.5, "line-dasharray": [3, 3] },
      });
      drawImbl();
      drawRoute(recRef.current);
    });
    // Unmount (panel closed, thread left) must destroy the map, or every
    // open/close leaks a WebGL context plus tile-fetch listeners.
    return () => {
      markers.current.forEach((marker) => marker.remove());
      markers.current = [];
      map.current?.remove();
      map.current = null;
      imblDrawn.current = false;
      routeDrawn.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The boundary arrives later than the map (one cached GET at boot), so
  // drawing retries on every imbl change until it sticks.
  useEffect(() => {
    drawImbl();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imbl]);

  useEffect(() => {
    const m = map.current;
    if (!m) return;
    markers.current.forEach((marker) => marker.remove());
    markers.current = [];
    recRef.current = recommendation;
    clearRoute();
    drawRoute(recommendation);
    if (!recommendation) return;

    const origin = recommendation.spatial_context?.origin;
    if (!origin) return;

    const colour = recommendation.verdict
      ? VERDICT_COLOUR[recommendation.verdict.value] ?? "#4a9eff"
      : "#4a9eff";

    const marker = new maplibregl.Marker({ color: colour })
      .setLngLat([origin.lon, origin.lat])
      .setPopup(
        new maplibregl.Popup({ offset: 18 }).setHTML(
          `<strong>${origin.name ?? "Origin"}</strong><br/>` +
            `${origin.lat.toFixed(3)}, ${origin.lon.toFixed(3)}<br/>` +
            `<small>${origin.source}</small>`,
        ),
      )
      .addTo(m);
    markers.current.push(marker);

    // PFZ candidates carry distance + bearing, so their positions are
    // reconstructed from the numbers in the claims rather than guessed.
    for (const claim of recommendation.claims ?? []) {
      const distance = Number(claim.slots?.["distance_km"]);
      const bearing = Number(claim.slots?.["bearing_deg"]);
      if (!Number.isFinite(distance) || !Number.isFinite(bearing)) continue;
      const rad = (bearing * Math.PI) / 180;
      const dLat = (distance * Math.cos(rad)) / 111.19;
      const dLon =
        (distance * Math.sin(rad)) / (111.19 * Math.cos((origin.lat * Math.PI) / 180));
      const zone = new maplibregl.Marker({ color: "#2fbf71", scale: 0.7 })
        .setLngLat([origin.lon + dLon, origin.lat + dLat])
        .setPopup(
          new maplibregl.Popup({ offset: 14 }).setHTML(
            `<strong>${claim.id}</strong><br/>${distance} km at ${bearing}°`,
          ),
        )
        .addTo(m);
      markers.current.push(zone);
    }

    m.flyTo({ center: [origin.lon, origin.lat], zoom: 8.2, duration: 900 });
  }, [recommendation]);

  return <div className="map" ref={container} />;
}
