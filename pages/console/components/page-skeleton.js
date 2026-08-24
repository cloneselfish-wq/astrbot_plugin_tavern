import { copy } from "../copy/catalog.js";
import { createElement } from "./dom.js";

const bar = (size = "medium") => createElement("span", {
  class: "tavern-skeleton-bar",
  "data-size": size,
});

const metric = () => createElement("div", { class: "tavern-skeleton-metric" }, [
  bar("short"),
  bar("value"),
  bar("medium"),
]);

const panel = (wide = false) => createElement("div", {
  class: `tavern-skeleton-panel${wide ? " tavern-skeleton-panel-wide" : ""}`,
}, [bar("heading"), bar("long"), bar("medium"), bar("long")]);

export function renderWorkspaceSkeleton(label = "") {
  const accessibleLabel = String(label || copy("client.operation.read"));
  return createElement("section", {
    class: "tavern-page-skeleton",
    role: "status",
    "aria-label": accessibleLabel,
    "aria-busy": "true",
    "aria-live": "polite",
    "aria-atomic": "true",
    "data-testid": "tavern-page-skeleton",
  }, [
    createElement("div", { class: "tavern-skeleton-heading" }, [
      bar("title"),
      bar("medium"),
    ]),
    createElement("div", { class: "tavern-skeleton-metrics" }, [
      metric(), metric(), metric(), metric(),
    ]),
    createElement("div", { class: "tavern-skeleton-layout" }, [
      panel(true),
      panel(false),
      panel(true),
    ]),
  ]);
}
