import type { Recommendation } from "../types";
import { str } from "../i18n/strings";
import MapView from "./MapView";

interface Props {
  recommendation: Recommendation | null;
  imbl: Array<[number, number]> | null;
}

/**
 * Sector chart tab: the real MapLibre map (OSM tiles, Coromandel box,
 * origin + PFZ markers from the answer) inside the chart-styled frame,
 * with an honest legend and a readout fed ONLY by spatial_context.
 * No cursor-depth fiction: without bathymetry in the answer there is
 * no depth to report.
 */
export default function SectorMap({ recommendation, imbl }: Props) {
  const maybe = recommendation?.spatial_context?.origin;
  // Never trust a coordinate's type: backend JSON and old localStorage can
  // both hand us strings. No valid origin means no map, not a crash.
  const origin =
    maybe && typeof maybe.lat === "number" && Number.isFinite(maybe.lat) &&
    typeof maybe.lon === "number" && Number.isFinite(maybe.lon)
      ? maybe
      : null;
  const readout = origin
    ? `${origin.lat.toFixed(3)}°, ${origin.lon.toFixed(3)}${origin.name ? ` · ${origin.name}` : ""}`
    : str.side.readoutNoPosition;

  return (
    <div id="mapPane" className="pane">
      <MapView recommendation={origin ? recommendation : null} imbl={imbl} />
      <div className="map-readout">{readout}</div>
      <div className="map-key">
        <div style={{ ["--k" as string]: "var(--ink)" }}>
          <i className="dot" />
          {str.side.legendOrigin}
        </div>
        <div style={{ ["--k" as string]: "var(--green)" }}>
          <i className="dot" />
          {str.side.legendZone}
        </div>
        <div style={{ ["--k" as string]: "var(--blue)" }}>
          <i className="dash" />
          {str.side.legendBox}
        </div>
        {imbl && imbl.length > 1 && (
          <div style={{ ["--k" as string]: "var(--red)" }}>
            <i className="dash" />
            {str.side.legendBoundary}
          </div>
        )}
        {recommendation?.route && recommendation.route.waypoints.length > 1 && (
          <div style={{ ["--k" as string]: "var(--blue)" }}>
            <i className="dash" />
            {str.side.legendRoute}
          </div>
        )}
      </div>
    </div>
  );
}
