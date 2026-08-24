import { COPY_ENTRIES as core } from "./catalog-core.js";
import { COPY_ENTRIES as dialogs } from "./catalog-dialogs.js";
import { COPY_ENTRIES as pages_runtime } from "./catalog-pages-runtime.js";
import { COPY_ENTRIES as pages_creation } from "./catalog-pages-creation.js";
import { COPY_ENTRIES as pages_system } from "./catalog-pages-system.js";
import { COPY_ENTRIES as visualizations } from "./catalog-visualizations.js";

const CATALOG = Object.create(null);
for (const group of [core, dialogs, pages_runtime, pages_creation, pages_system, visualizations]) {
  for (const [key, value] of Object.entries(group)) {
    if (Object.hasOwn(CATALOG, key)) throw new Error(`Duplicate copy key: ${key}`);
    CATALOG[key] = value;
  }
}
Object.freeze(CATALOG);
const MISSING_COPY = CATALOG["copy.catalog.missing"];

export function copy(key, values = {}) {
  let text = Object.hasOwn(CATALOG, key) ? CATALOG[key] : MISSING_COPY;
  const safeValues = values && typeof values === "object" ? values : {};
  for (const [name, value] of Object.entries(safeValues)) {
    text = text.replaceAll(`{${name}}`, String(value));
  }
  return text;
}

export function applyDocumentCopy() {
  document.documentElement.lang = "zh-CN";
  document.documentElement.dir = "ltr";
}
