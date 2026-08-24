import { copy } from "../copy/catalog.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { renderClocks, renderQuestTracks } from "./quest-clock.js";
import { renderRelation } from "./relation.js";
import { renderScenePath } from "./scene-path.js";
import { renderSurfaceHost } from "../components/surface-host.js";

const BASE_LENSES = new Set(["party", "replay", "generation"]);
const SURFACE_KEYS = Object.freeze({
  scene: "scene_path",
  quests: "quest_tracks",
  clocks: "clocks",
  relations: "relations",
  resources: "resources",
  challenge: "challenge",
  progression: "progression",
  world: "world",
});

function rows(value) {
  return Array.isArray(value) ? value : [];
}

export function hasSafeSurfaceActions(envelope = {}) {
  return rows(envelope?.data?.actions).some((action) => (
    action?.transportReady === true
    && typeof action?.intent === "string"
    && action.intent.length > 0
    && Number.isInteger(action?.expected_revision)
    && action.expected_revision >= 0
  ));
}

function node(tag, attributes = {}, children = []) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === undefined || value === null || value === false) continue;
    if (name === "class") element.className = String(value);
    else if (name === "text") element.textContent = String(value);
    else if (name === "dataset") Object.assign(element.dataset, value);
    else element.setAttribute(name, String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) if (child) element.append(child);
  return element;
}

export function worldSurfaceKey(lens) {
  return SURFACE_KEYS[lens] || lens;
}

export function compiledWorldLenses(uiProfile = {}) {
  const declaredSurfaces = rows(uiProfile?.ui_surface_manifest?.surfaces)
    .filter((surface) => rows(surface?.placements).some((placement) => placement === "live_session" || placement === "session_detail"))
    .map((surface) => ({
      key: surface.surface_key,
      surface_key: surface.surface_key,
      label: surface.label,
      order: surface.order,
    }));
  if (declaredSurfaces.length) return declaredSurfaces.sort((left, right) => Number(left.order || 0) - Number(right.order || 0));
  return rows(uiProfile.live_lenses).filter((lens) => {
    const key = lens?.key || lens?.id;
    return key && !BASE_LENSES.has(key);
  });
}

function visualFor(key, envelope, title, visualizers, scenePathNodes, handlers) {
  const data = envelope?.data || {};
  if (data.component_kind && data.data_kind) return renderSurfaceHost(envelope, { handlers });
  if (key === "quest_tracks") return visualizers.renderQuestTracks(rows(data.items));
  if (key === "clocks") return visualizers.renderClocks(rows(data.items));
  if (key === "scene_path") return visualizers.renderScenePath(scenePathNodes || rows(data.nodes || data.items));
  if (key === "relations") return visualizers.renderRelation({ nodes: rows(data.nodes), edges: rows(data.edges) });
  return renderStatePanel({
    phase: "unsupported",
    operation: `显示「${title}」`,
    problem: {
      message: copy("visualizations.world_lens.rc8.c67fcc5465"),
      recovery: copy("visualizations.world_lens.rc8.96e561b54e"),
    },
  });
}

function missingSurface(title) {
  return renderStatePanel({
    phase: "partial",
    operation: title,
    problem: {
      message: copy("visualizations.world_lens.missing", { p0: title }),
      recovery: copy("visualizations.world_lens.rc8.9e2529ae4a"),
      code: "world.lens_projection_unavailable",
      retryable: true,
    },
  });
}

function renderOne(surfaces, lens, visualizers, scenePathNodes, handlers) {
  const key = lens.surface_key || worldSurfaceKey(lens.key || lens.id);
  const title = lens.label || key;
  const envelope = surfaces[key];
  const wrapper = node("section", {
    class: "tavern-world-lens-card",
    dataset: { kind: key },
  });
  if (!envelope) {
    wrapper.append(missingSurface(title));
    return wrapper;
  }
  if (["error", "partial", "permission"].includes(envelope.state)) {
    wrapper.append(renderStatePanel({
      phase: envelope.state,
      operation: title,
      problem: rows(envelope.problems)[0],
    }));
    if (["error", "permission"].includes(envelope.state)) return wrapper;
  }
  if (envelope.state === "empty" && !hasSafeSurfaceActions(envelope)) {
    wrapper.append(renderStatePanel({ phase: "empty", emptyCopy: copy("visualizations.world_lens.empty", { p0: title }) }));
    return wrapper;
  }
  const visualization = visualFor(key, envelope, title, visualizers, scenePathNodes, handlers);
  if (visualization) wrapper.append(visualization);
  else if (envelope.state !== "empty") wrapper.append(missingSurface(title));
  return wrapper;
}

export function renderWorldLens(envelope, {
  lens = "world",
  uiProfile = null,
  visualizers = { renderQuestTracks, renderClocks, renderRelation, renderScenePath },
  scenePathNodes = null,
  handlers = null,
} = {}) {
  const data = envelope?.data || {};
  const profile = uiProfile || data.ui_profile || {};
  const surfaces = data.surfaces || {};
  const section = node("section", { class: "tavern-world-lens" });
  if (lens !== "world") {
    const declared = compiledWorldLenses(profile).find((item) => (item.key || item.id) === lens || item.surface_key === lens)
      || rows(profile.live_lenses).find((item) => (item.key || item.id) === lens);
    section.append(renderOne(surfaces, declared || { key: lens, label: lens }, visualizers, scenePathNodes, handlers));
    return section;
  }
  const declared = compiledWorldLenses(profile);
  if (declared.length) {
    declared.forEach((item) => section.append(renderOne(surfaces, item, visualizers, scenePathNodes, handlers)));
    return section;
  }
  const available = Object.keys(surfaces).filter((key) => {
    const child = surfaces[key];
    const payload = child?.data || {};
    return rows(payload.items || payload.nodes).length > 0;
  });
  if (available.length) {
    available.forEach((key) => section.append(renderOne(surfaces, { key, label: key }, visualizers, scenePathNodes, handlers)));
  } else {
    section.append(renderStatePanel({ phase: "empty", emptyCopy: copy("visualizations.world_lens.rc8.3621e47727") }));
  }
  return section;
}
