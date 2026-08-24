import { copy } from "../../copy/catalog.js";
import { collectionSurface, itemCards, surfaceNode, surfaceRecipe, surfaceRows } from "../surface-kit.js";

function worldOverview(payload) {
  const data = payload.data || {};
  return surfaceRecipe(payload, surfaceNode("article", { class: "tavern-world-overview-hero" }, [
    surfaceNode("h4", { text: data.title || copy("components.definitions.rc8.230be35ec0") }),
    surfaceNode("p", { text: data.summary || payload.copy?.empty || copy("components.definitions.rc8.64216013fc") }),
    data.players ? surfaceNode("p", { text: `建议人数：${data.players}` }) : null,
    surfaceNode("ul", { class: "tavern-declared-tag-list" }, surfaceRows(data.tags).map((tag) => surfaceNode("li", { text: tag }))),
  ]));
}

function contentInventory(payload) {
  const items = surfaceRows(payload.data?.items);
  const main = items.length ? surfaceNode("div", { class: "tavern-content-inventory" }, items.map((item) => surfaceNode("article", {}, [
    surfaceNode("strong", { text: item.count ?? 0 }),
    surfaceNode("span", { text: item.label || copy("components.definitions.rc8.797e80379c") }),
  ]))) : itemCards([], { empty: payload.copy?.empty });
  return surfaceRecipe(payload, main);
}

export const SURFACE_RENDERERS = Object.freeze({
  world_overview: worldOverview,
  content_inventory: contentInventory,
  readme_reader: (payload) => surfaceRecipe(payload, itemCards(payload.data?.sections, { empty: payload.copy?.empty })),
  character_build_catalog: (payload) => collectionSurface(payload, { fields: [["limitation", copy("pages.characters.card.limits")], ["risk", copy("components.definitions.rc8.af75e78cac")]] }),
});

