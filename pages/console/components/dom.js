import { formatUtc8Minute } from "./time.js";

export function createElement(tag, attributes = {}, children = []) {
  const node = document.createElement(String(tag || "div"));
  const isTime = String(tag || "").toLowerCase() === "time";
  const safeAttributes = attributes && typeof attributes === "object" ? attributes : {};
  for (const [key, value] of Object.entries(safeAttributes)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = isTime ? formatUtc8Minute(value) : String(value);
    else if (key === "class") node.className = String(value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "value") {
      node.value = String(value);
    } else {
      node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  const safeChildren = Array.isArray(children) ? children : [children];
  node.append(...safeChildren.filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

export function testToken(value, fallback = "item") {
  const text = String(value || fallback);
  return encodeURIComponent(text).replaceAll("%", "_").slice(0, 120) || fallback;
}
