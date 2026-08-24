import { renderStatePanel } from "./empty-problem.js";
import { loadSurfaceRenderer } from "./surface-registry.js";

export function renderSurfaceHost(envelope = {}, context = {}) {
  const payload = envelope?.data || {};
  const root = document.createElement("section");
  root.className = "tavern-surface-host";
  root.dataset.phase = "loading";
  root.append(renderStatePanel({
    phase: "loading",
    operation: `加载「${payload?.copy?.title || envelope?.summary?.label || "世界板块"}」`,
  }));
  let active = true;
  loadSurfaceRenderer(payload).then((renderer) => {
    if (!active) return;
    root.dataset.phase = "ready";
    root.replaceChildren(renderer(payload, context));
  }).catch((problem) => {
    if (!active) return;
    root.dataset.phase = "unsupported";
    root.replaceChildren(renderStatePanel({
      phase: "unsupported",
      operation: `显示「${payload?.copy?.title || envelope?.summary?.label || "世界板块"}」`,
      problem,
    }));
  });
  root.dispose = () => { active = false; };
  return root;
}
