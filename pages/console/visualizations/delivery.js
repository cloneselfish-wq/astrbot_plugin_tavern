import { copy } from "../copy/catalog.js";
const DELIVERY_STATES = new Set(["ready", "running", "waiting", "warning", "failed", "completed"]);

function safeState(value) {
  const state = String(value || "");
  return DELIVERY_STATES.has(state) ? state : "unknown";
}

function partRow(part, index) {
  const row = document.createElement("li");
  row.dataset.state = safeState(part.state || part.status);
  const label = document.createElement("strong");
  label.textContent = part.label || copy("visualizations.delivery.partRow.message.c81ad1e1da", {p0: index + 1});
  const detail = document.createElement("span");
  const attempts = part.attempts ?? part.attempt_count;
  detail.textContent = [
    part.state_label || copy("visualizations.delivery.partRow.message.cdea037991"),
    attempts !== undefined ? copy("visualizations.delivery.partRow.message.77f89963b8", {p0: attempts}) : "",
    part.receipt_label || (part.received ? copy("visualizations.delivery.partRow.message.dbd5835a9b") : ""),
  ].filter(Boolean).join(" · ");
  row.append(label, detail);
  return row;
}

export function renderDelivery(items = []) {
  const section = document.createElement("section");
  section.className = "tavern-delivery-lanes";
  section.dataset.visualization = "delivery-lanes";
  const heading = document.createElement("h3");
  heading.textContent = copy("visualizations.delivery.renderDelivery.message.22c404d449");
  section.setAttribute("aria-label", heading.textContent);
  section.append(heading);
  const safeItems = Array.isArray(items) ? items : [];
  if (!safeItems.length) {
    const empty = document.createElement("p");
    empty.textContent = copy("visualizations.delivery.renderDelivery.message.2c664b60e2");
    section.append(empty);
    return section;
  }
  for (const item of safeItems) {
    const article = document.createElement("article");
    article.className = "tavern-delivery-lane";
    const title = document.createElement("h4");
    title.textContent = item.label || item.title || copy("visualizations.delivery.renderDelivery.message.15e23afda7");
    const summary = document.createElement("p");
    summary.textContent = [
      item.audience_label || copy("visualizations.delivery.renderDelivery.message.afcfe89395"),
      item.state_label || copy("visualizations.delivery.renderDelivery.message.cdea037991"),
      item.summary || "",
    ].filter(Boolean).join(" · ");
    const parts = Array.isArray(item.parts) && item.parts.length ? item.parts : [item];
    const list = document.createElement("ol");
    list.className = "tavern-delivery-parts";
    list.append(...parts.map(partRow));
    article.append(title, summary, list);
    section.append(article);
  }
  return section;
}

export const renderDeliveryLanes = renderDelivery;
