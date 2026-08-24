import { copy } from "../copy/catalog.js";
import { formatUtc8Day, formatUtc8Minute } from "../components/time.js";
const TIMELINE_STATES = new Set(["ready", "running", "waiting", "warning", "failed", "completed"]);

function safeState(value) {
  const state = String(value || "");
  return TIMELINE_STATES.has(state) ? state : "unknown";
}

function dayLabel(value) {
  return formatUtc8Day(value, { fallback:copy("visualizations.timeline.dayLabel.message.26fa989f4d") });
}

export function renderTimeline(items = [], { onSelect = null, limit = 50, title = copy("visualizations.timeline.renderTimeline.message.d861589965") } = {}) {
  const safeLimit = Math.max(1, Math.min(50, Number(limit) || 50));
  const section = document.createElement("section");
  section.className = "tavern-timeline";
  section.dataset.visualization = "timeline";
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.append(heading);
  if (!Array.isArray(items) || !items.length) {
    const empty = document.createElement("p");
    empty.textContent = copy("visualizations.timeline.renderTimeline.message.6264a6860a");
    section.append(empty);
    return section;
  }
  const list = document.createElement("ol");
  list.className = "tavern-timeline-list";
  section.append(list);
  let previousDay = "";
  for (const item of items.slice(0, safeLimit)) {
    const day = dayLabel(item.created_at || item.timestamp || item.time);
    if (day !== previousDay) {
      const group = document.createElement("li");
      group.className = "tavern-timeline-day";
      group.setAttribute("role", "presentation");
      group.textContent = day;
      list.append(group);
      previousDay = day;
    }
    const article = document.createElement("li");
    article.className = "tavern-timeline-item";
    article.dataset.state = safeState(item.state || item.status);
    const marker = document.createElement("span");
    marker.className = "tavern-timeline-marker";
    marker.setAttribute("aria-hidden", "true");
    const body = document.createElement("div");
    const label = item.label || item.title || copy("visualizations.timeline.renderTimeline.message.2efeffd5a2");
    if (typeof onSelect === "function") {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "tavern-timeline-select";
      button.textContent = label;
      button.addEventListener("click", () => onSelect(item, button));
      body.append(button);
    } else {
      const strong = document.createElement("strong");
      strong.textContent = label;
      body.append(strong);
    }
    if (item.summary || item.description) {
      const summary = document.createElement("p");
      summary.textContent = item.summary || item.description;
      body.append(summary);
    }
    const state = document.createElement("span");
    state.className = "tavern-timeline-state";
    state.textContent = item.state_label || copy("visualizations.timeline.renderTimeline.message.cdea037991");
    body.append(state);
    const time = document.createElement("time");
    const rawTime = item.created_at || item.timestamp || item.time || "";
    const parsedTime = new Date(rawTime);
    if (!Number.isNaN(parsedTime.valueOf())) time.dateTime = parsedTime.toISOString();
    time.textContent = formatUtc8Minute(rawTime, { fallback:item.time_label || copy("visualizations.timeline.renderTimeline.message.26fa989f4d") });
    article.append(marker, body, time);
    list.append(article);
  }
  if (items.length > safeLimit) {
    const boundary = document.createElement("p");
    boundary.className = "tavern-visualization-boundary";
    boundary.textContent = copy("visualizations.timeline.renderTimeline.message.5281263e90");
    section.append(boundary);
  }
  return section;
}
