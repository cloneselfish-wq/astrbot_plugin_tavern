import { copy } from "../copy/catalog.js";
import { createElement } from "./dom.js";

const PUBLIC_STATES = new Set([
  "ready", "running", "waiting", "recovering", "warning", "error",
  "readonly", "stale", "conflict", "permission", "partial", "empty",
  "loading", "refreshing", "rate_limited", "unavailable", "timeout",
  "disconnect",
]);

function statusIcon(state) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  const paths = {
    ready: "M3 8.5 6.2 12 13 4.5",
    running: "M5 3.5 12 8 5 12.5Z",
    waiting: "M8 2.5a5.5 5.5 0 1 0 0 11 5.5 5.5 0 0 0 0-11Zm0 2v4l2.5 1.5",
    recovering: "M12.5 6A5 5 0 1 0 13 9M12.5 3v3h-3",
    warning: "M8 2 14 13H2Zm0 4v3.5m0 1.8v.2",
    error: "M4 4 12 12M12 4 4 12",
    readonly: "M3 7h10v7H3Zm2 0V5a3 3 0 0 1 6 0v2",
    stale: "M3 8a5 5 0 0 1 8-4M13 8a5 5 0 0 1-8 4M11 2v3H8M5 14v-3h3",
    conflict: "M3 4h7l-2-2m2 2L8 6m5 6H6l2 2m-2-2 2-2",
    permission: "M3 7h10v7H3Zm2 0V5a3 3 0 0 1 6 0v2",
    partial: "M8 2.5a5.5 5.5 0 1 0 0 11Zm0 0v11",
    empty: "M3 4h10v9H3Zm2 3h6",
    loading: "M8 2.5a5.5 5.5 0 1 0 0 11M8 2.5v2",
    refreshing: "M12.5 6A5 5 0 1 0 13 9M12.5 3v3h-3",
    rate_limited: "M4 2.5h8M4 13.5h8M5 3c0 3 2 3 3 5-1 2-3 2-3 5m5-10c0 3-2 3-3 5 1 2 3 2 3 5",
    unavailable: "M4 4 12 12M12 4 4 12M2.5 8a5.5 5.5 0 0 0 11 0",
    timeout: "M8 2.5a5.5 5.5 0 1 0 0 11Zm0 2v4l2.5 1.5",
    disconnect: "M5.5 5.5 3 8l2.5 2.5M10.5 5.5 13 8l-2.5 2.5M6.5 8h3M3 3l10 10",
    unknown: "M8 2.5a5.5 5.5 0 1 0 0 11Zm0 8.5v.2m0-6.7c1.5 0 2.5.8 2.5 2 0 1.8-2.5 1.7-2.5 3.2",
  };
  path.setAttribute("d", paths[state] || paths.unknown);
  path.setAttribute("fill", state === "running" ? "currentColor" : "none");
  path.setAttribute("stroke", "currentColor");
  path.setAttribute("stroke-width", "1.6");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.append(path);
  return svg;
}

export function renderStatusBadge({
  state = "unknown",
  label = "",
  labelKey = "",
  detail = "",
} = {}) {
  const requested = String(state).replaceAll("-", "_");
  const safeState = PUBLIC_STATES.has(requested) ? requested : "unknown";
  const text = String(
    label
    || (labelKey ? copy(labelKey) : "")
    || copy("components.primitives.renderStatusBadge.message.cdea037991"),
  );
  return createElement("span", {
    class: "tavern-status-badge",
    "data-state": safeState,
    "data-testid": `tavern-status-${safeState}`,
    title: detail || undefined,
  }, [statusIcon(safeState), createElement("span", { text })]);
}
