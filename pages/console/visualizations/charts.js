import { copy } from "../copy/catalog.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { renderDataList } from "../components/lists.js";

const SVG_NS = "http://www.w3.org/2000/svg";

function fallbackVisualization(kind, label, columns, rows) {
  const fallback = document.createElement("section");
  fallback.className = "tavern-visualization-fallback";
  fallback.dataset.visualization = String(kind || "unavailable");
  fallback.append(
    renderStatePanel({
      phase: "partial",
      problem: {
        message: copy("visualizations.charts.renderHonestVisualization.message.e3313838ff", { p0: label }),
        recovery: copy("visualizations.charts.renderHonestVisualization.recovery.38262a4009"),
      },
    }),
    renderDataList({
      id: `${kind || "chart"}-fallback`,
      columns: Array.isArray(columns) ? columns : [],
      rows: Array.isArray(rows) ? rows : [],
      emptyCopy: copy("visualizations.charts.renderHonestVisualization.emptyCopy.e8aa98be4d"),
    }),
  );
  return fallback;
}

function chartPoints(points) {
  if (!Array.isArray(points) || points.length < 2 || points.length > 48) return null;
  const values = points.map((point) => Number(point?.value));
  return values.every(Number.isFinite) ? values : null;
}

export function renderHonestVisualization({
  kind,
  points,
  label = copy("visualizations.charts.renderHonestVisualization.message.9b59e637c8"),
  columns = [],
  rows = [],
} = {}) {
  const values = chartPoints(points);
  if (!values) return fallbackVisualization(kind, label, columns, rows);

  const minimum = Math.min(0, ...values);
  const maximum = Math.max(0, ...values);
  const span = Math.max(1, maximum - minimum);
  const coordinates = values.map((value, index) => {
    const x = (index * 100) / (values.length - 1);
    const y = 38 - ((value - minimum) * 36) / span;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });

  const figure = document.createElement("figure");
  figure.className = "tavern-visualization";
  figure.dataset.visualization = String(kind || "numeric-series");
  const caption = document.createElement("figcaption");
  caption.textContent = label;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 100 40");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", label);
  const polyline = document.createElementNS(SVG_NS, "polyline");
  polyline.setAttribute("points", coordinates.join(" "));
  polyline.setAttribute("fill", "none");
  polyline.setAttribute("stroke", "currentColor");
  polyline.setAttribute("vector-effect", "non-scaling-stroke");
  svg.append(polyline);
  figure.append(caption, svg);
  return figure;
}
