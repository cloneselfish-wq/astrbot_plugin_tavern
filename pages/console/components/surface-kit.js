import { copy } from "../copy/catalog.js";
import { renderButton } from "./buttons.js";
import { renderStatePanel } from "./empty-problem.js";

export function surfaceNode(tag, attributes = {}, children = []) {
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

export function surfaceRows(value) {
  return Array.isArray(value) ? value : [];
}

function publicValue(value) {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value;
  return "";
}

export function itemCards(items, { empty = copy("components.surface_kit.rc8.4bc1812ff0"), fields = [] } = {}) {
  const rows = surfaceRows(items);
  if (!rows.length) return renderStatePanel({ phase: "empty", emptyCopy: empty });
  return surfaceNode("div", { class: "tavern-declared-card-grid" }, rows.map((item) => {
    const details = fields
      .map(([key, label]) => [label, publicValue(item?.[key])])
      .filter(([, value]) => value);
    return surfaceNode("article", { class: "tavern-declared-item-card", dataset: { objectKey: item?.key || "" } }, [
      surfaceNode("h4", { text: item?.label || copy("components.surface_kit.rc8.be981aa51f") }),
      item?.summary ? surfaceNode("p", { text: item.summary }) : null,
      details.length ? surfaceNode("dl", { class: "tavern-declared-facts" }, details.flatMap(([label, value]) => [
        surfaceNode("dt", { text: label }),
        surfaceNode("dd", { text: value }),
      ])) : null,
    ]);
  }));
}

export function surfaceRecipe(payload, main, { status = null, notes = [], actions = null } = {}) {
  const copySlots = payload?.copy || {};
  const actionNodes = actions || surfaceRows(payload?.actions).map((action) => renderButton({
    variant: action?.danger ? "danger" : "secondary",
    label: action?.label || copy("components.surface_kit.rc8.56a7d060d3"),
    intent: action,
  }));
  return surfaceNode("section", {
    class: "tavern-declared-surface",
    dataset: {
      component: payload?.component_kind || "",
      recipe: payload?.visual_recipe || "",
      mobile: payload?.mobile_presentation || "",
    },
  }, [
    surfaceNode("header", { class: "tavern-declared-surface-head" }, [
      surfaceNode("div", {}, [
      surfaceNode("h3", { text: copySlots.title || copy("components.surface_kit.rc8.fc2afb99f3") }),
        surfaceNode("p", { text: copySlots.summary || copySlots.help || copy("components.surface_kit.rc8.42f85d2e02") }),
      ]),
      status,
    ]),
    surfaceNode("div", { class: "tavern-declared-surface-main" }, main),
    surfaceNode("aside", { class: "tavern-declared-surface-notes" }, [
      copySlots.help ? surfaceNode("p", {}, [surfaceNode("strong", { text: copy("components.surface_kit.rc8.40a0299a14") }), document.createTextNode(copySlots.help)]) : null,
      copySlots.impact ? surfaceNode("p", {}, [surfaceNode("strong", { text: copy("components.surface_kit.rc8.8a82deea73") }), document.createTextNode(copySlots.impact)]) : null,
      copySlots.boundary ? surfaceNode("p", {}, [surfaceNode("strong", { text: copy("components.surface_kit.rc8.3c1e01ba1c") }), document.createTextNode(copySlots.boundary)]) : null,
      ...notes,
    ]),
    actionNodes.length ? surfaceNode("footer", { class: "tavern-declared-surface-actions" }, actionNodes) : null,
  ]);
}

export function collectionSurface(payload, options = {}) {
  const data = payload?.data || {};
  return surfaceRecipe(payload, itemCards(data.items, {
    empty: payload?.copy?.empty || options.empty,
    fields: options.fields || [],
  }));
}
