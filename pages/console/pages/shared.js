import { copy } from "../copy/catalog.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { testToken } from "../components/dom.js";
import { formatUtc8Minute } from "../components/time.js";

export function el(tag, attributes = {}, children = []) {
  const node = document.createElement(tag);
  const isTime = String(tag || "").toLowerCase() === "time";
  for (const [key, value] of Object.entries(attributes)) {
    if (value === undefined || value === null || value === false) continue;
    if (typeof value === "function" && /^on[A-Z]/.test(key)) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "text") node.textContent = isTime ? formatUtc8Minute(value) : String(value);
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  node.append(...children.filter(Boolean));
  return node;
}

export function value(model, id, fallback = null) {
  const sections = Array.isArray(model?.sections) ? model.sections : [];
  return sections.find((candidate) => candidate?.id === id)?.value ?? fallback;
}

export function section(model, id) {
  const sections = Array.isArray(model?.sections) ? model.sections : [];
  return sections.find((candidate) => candidate?.id === id) || null;
}

export function label(item, fallback = copy("pages.shared.label.message.99032465e3")) {
  if (typeof item === "string") return item;
  return item?.label || item?.title || item?.name || item?.summary || fallback;
}

export function summary(item, fallback = "") { return item?.summary || item?.description || item?.text || item?.reason || fallback; }

export function pageRoot(model, className) {
  const workspace = testToken(model?.workspace, "workspace");
  const phase = testToken(model?.phase, "loading");
  return el("section", {
    class: `tavern-page tavern-page-enter ${String(className || "").trim()}`.trim(),
    "data-testid": `tavern-page-${workspace}`,
    "data-phase": phase,
    "aria-busy": phase === "loading" ? "true" : "false",
  });
}

export function stateNotice(model, operation) {
  if (model?.phase === "ready") return document.createDocumentFragment();
  return renderStatePanel({ phase: model?.phase, operation, problem: model?.problems?.[0], emptyCopy: copy("pages.shared.stateNotice.emptyCopy.692090a77f", {p0: operation}) });
}

export function rows(items, empty = []) { return Array.isArray(items) ? items : empty; }
