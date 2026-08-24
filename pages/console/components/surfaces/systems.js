import { copy } from "../../copy/catalog.js";
import { collectionSurface, itemCards, surfaceNode, surfaceRecipe, surfaceRows } from "../surface-kit.js";

function elementMatrix(payload) {
  const data = payload.data || {};
  return surfaceRecipe(payload, surfaceNode("div", { class: "tavern-element-matrix" }, [
    itemCards(data.items, { empty: payload.copy?.empty, fields: [["state", copy("components.capability_hub.state_label")], ["summary", copy("components.systems.rc8.6be2d0b7bc")]] }),
    surfaceRows(data.interactions).length ? surfaceNode("section", { class: "tavern-element-reactions" }, [
      surfaceNode("h4", { text: copy("components.systems.rc8.55bb794611") }),
      itemCards(data.interactions, { empty: copy("components.systems.rc8.96371f493d") }),
    ]) : null,
  ]));
}

function environment(payload) {
  return surfaceRecipe(payload, surfaceNode("div", { class: "tavern-environment-stack" }, [
    itemCards(payload.data?.items, { empty: payload.copy?.empty, fields: [["state", copy("components.systems.rc8.13c9247be2")], ["risk", copy("components.systems.rc8.dfdcd4e5e7")]] }),
    surfaceRows(payload.data?.mitigations).length ? itemCards(payload.data.mitigations, { empty: copy("components.systems.rc8.fc7fff5e82") }) : null,
  ]));
}

export const SURFACE_RENDERERS = Object.freeze({
  resource_ledger: (payload) => collectionSurface(payload, { fields: [["current", copy("components.systems.rc8.60ddd8958b")], ["total", copy("components.systems.rc8.8e7ddbeee3")], ["recent_change", copy("components.systems.rc8.e329c44530")]] }),
  assembly_board: (payload) => collectionSurface(payload, { fields: [["state", copy("components.systems.rc8.c134e7b325")], ["deadline", copy("components.systems.rc8.59f0bf4775")], ["risk", copy("components.systems.rc8.ab5963af5c")]] }),
  accord_ledger: (payload) => collectionSurface(payload, { fields: [["state", copy("components.systems.rc8.397de6ed65")], ["deadline", copy("components.systems.rc8.59f0bf4775")], ["limitation", copy("pages.characters.card.limits")]] }),
  rumor_network: (payload) => collectionSurface(payload, { fields: [["state", copy("components.systems.rc8.175ca90aea")], ["risk", copy("components.systems.rc8.73ccdc3adc")], ["recent_change", copy("pages.dashboard.recentTimeline.text.cb29bae4c8")]] }),
  element_matrix: elementMatrix,
  environment_board: environment,
});

