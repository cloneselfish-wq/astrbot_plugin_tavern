import { copy } from "../copy/catalog.js";
export function renderScenePath(items = []) {
  const section = document.createElement("section");
  section.className = "tavern-scene-path";
  section.dataset.visualization = "scene-path";
  const heading = document.createElement("h3");
  heading.textContent = copy("visualizations.scene_path.renderScenePath.message.676abfcdaf");
  section.setAttribute("aria-label", heading.textContent);
  section.append(heading);
  const safeItems = Array.isArray(items) ? items : [];
  if (!safeItems.length) {
    const empty = document.createElement("p");
    empty.textContent = copy("visualizations.scene_path.renderScenePath.message.d8a4fd8278");
    section.append(empty);
    return section;
  }
  const list = document.createElement("ol");
  for (const item of safeItems) {
    const row = document.createElement("li");
    const state = ["past", "current", "candidate"].includes(item.state) ? item.state : "unknown";
    row.dataset.state = state;
    if (state === "current") row.setAttribute("aria-current", "step");
    const label = document.createElement("strong");
    label.textContent = item.label || item.name || copy("visualizations.scene_path.renderScenePath.message.7ba1517bc1");
    const stateLabel = document.createElement("span");
    stateLabel.textContent = item.state_label || {
      past: copy("visualizations.scene_path.renderScenePath.message.347abeab8e"),
      current: copy("visualizations.scene_path.renderScenePath.message.cb62ebd689"),
      candidate: copy("visualizations.scene_path.renderScenePath.message.64af28c772"),
      unknown: copy("visualizations.scene_path.state_unavailable"),
    }[state];
    row.append(label, stateLabel);
    if (item.summary) {
      const summary = document.createElement("p");
      summary.textContent = item.summary;
      row.append(summary);
    }
    list.append(row);
  }
  section.append(list);
  return section;
}
