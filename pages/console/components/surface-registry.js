import { copy } from "../copy/catalog.js";
import { SURFACE_RENDERERS as DEFINITION_RENDERERS } from "./surfaces/definitions.js";
import { SURFACE_RENDERERS as STORY_RENDERERS } from "./surfaces/story.js";
import { SURFACE_RENDERERS as SYSTEM_RENDERERS } from "./surfaces/systems.js";
import { SURFACE_RENDERERS as OUTCOME_RENDERERS } from "./surfaces/outcomes.js";
const CONTRACTS = Object.freeze({
  world_overview: "world_overview_view",
  content_inventory: "content_inventory_view",
  readme_reader: "readme_section_view",
  character_build_catalog: "character_build_view",
  quest_board: "quest_track_view",
  challenge_board: "challenge_view",
  evidence_board: "evidence_ledger_view",
  clock_board: "clock_view",
  route_map: "route_view",
  relation_graph: "relation_view",
  npc_state_board: "npc_state_view",
  resource_ledger: "resource_view",
  assembly_board: "assembly_view",
  accord_ledger: "accord_view",
  rumor_network: "rumor_view",
  element_matrix: "element_view",
  environment_board: "environment_view",
  tactical_board: "tactical_conflict_view",
  ending_outlook: "ending_outlook_view",
  replay_timeline: "replay_view",
});

const RENDERERS = Object.freeze({
  ...DEFINITION_RENDERERS,
  ...STORY_RENDERERS,
  ...SYSTEM_RENDERERS,
  ...OUTCOME_RENDERERS,
});

export function registeredSurfaceKinds() {
  return Object.keys(CONTRACTS);
}

export async function loadSurfaceRenderer(payload = {}) {
  const component = String(payload.component_kind || "");
  const dataKind = String(payload.data_kind || "");
  if (!CONTRACTS[component] || CONTRACTS[component] !== dataKind || !RENDERERS[component]) {
    const error = new Error(copy("components.surface_registry.rc8.5db97b3c91"));
    error.code = "surface.component_unsupported";
    error.recovery = copy("components.surface_registry.rc8.bf39655cb0");
    throw error;
  }
  const expectedRecipe = `${component}.standard`;
  if (payload.visual_recipe && payload.visual_recipe !== expectedRecipe) {
    const error = new Error(copy("components.surface_registry.rc8.310740f5e9"));
    error.code = "surface.recipe_unsupported";
    error.recovery = copy("components.surface_registry.rc8.ffac418346");
    throw error;
  }
  const renderer = RENDERERS[component];
  if (typeof renderer !== "function") {
    const error = new Error(copy("components.surface_registry.rc8.2ddab92961"));
    error.code = "surface.renderer_missing";
    error.recovery = copy("components.surface_registry.rc8.017b11f4fc");
    throw error;
  }
  return renderer;
}
