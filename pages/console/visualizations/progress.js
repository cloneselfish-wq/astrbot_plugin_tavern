import { copy } from "../copy/catalog.js";
const SEGMENT_STATES = new Set(["ready", "running", "waiting", "warning", "failed", "completed"]);

function segmentState(value) {
  const state = String(value || "");
  return SEGMENT_STATES.has(state) ? state : "unknown";
}

function node(tag, attrs = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") element.textContent = String(value);
    else if (key === "class") element.className = value;
    else element.setAttribute(key, value === true ? "" : String(value));
  }
  element.append(...children.filter(Boolean));
  return element;
}

export function progressValue(current, total) {
  const value = Number(current);
  const maximum = Number(total);
  return Number.isFinite(value) && Number.isFinite(maximum) && maximum > 0
    ? Math.max(0, Math.min(1, value / maximum))
    : null;
}

export function renderProgress({
  label = copy("visualizations.progress.renderProgress.message.f81ff55de1"),
  current,
  total,
  state = "",
  detail = "",
  segments = [],
} = {}) {
  const ratio = progressValue(current, total);
  const hasSegments = Array.isArray(segments) && segments.length > 0;
  const mode = hasSegments ? "segmented" : ratio !== null ? "determinate" : "indeterminate";
  const wrapper = node("section", {
    class: "tavern-progress-block",
    "data-progress-mode": mode,
  });
  const valueLabel = ratio !== null ? `${Number(current)} / ${Number(total)}` : copy("visualizations.progress.renderProgress.message.2a039a28a8");
  wrapper.append(node("header", { class: "tavern-progress-heading" }, [
    node("strong", { text: label }),
    node("span", { text: valueLabel }),
  ]));
  if (hasSegments) {
    wrapper.append(node("ol", { class: "tavern-progress-segments", "aria-label": copy("visualizations.progress.renderProgress.message.c8985b6a97", {p0: label}) },
      segments.filter((segment) => segment && typeof segment === "object").map((segment, index) => node("li", {
        "data-state": segmentState(segment.state),
        text: segment.label || copy("visualizations.progress.renderProgress.message.eca56636b9", {p0: index + 1}),
      }))));
  } else {
    wrapper.append(node("div", {
      class: "tavern-progress",
      role: "progressbar",
      "aria-label": label,
      "aria-valuemin": ratio !== null ? 0 : undefined,
      "aria-valuemax": ratio !== null ? Number(total) : undefined,
      "aria-valuenow": ratio !== null ? ratio * Number(total) : undefined,
      "aria-valuetext": valueLabel,
      "aria-busy": ratio === null ? "true" : "false",
    }, [node("span", { style: ratio !== null ? `inline-size:${(ratio * 100).toFixed(2)}%` : undefined })]));
  }
  if (state || detail) wrapper.append(node("p", { class: "tavern-progress-detail", text: [state, detail].filter(Boolean).join(" · ") }));
  return wrapper;
}
