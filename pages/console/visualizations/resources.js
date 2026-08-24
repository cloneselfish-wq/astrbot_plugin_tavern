function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  element.className = className;
  if (text !== "") element.textContent = String(text);
  return element;
}

function finite(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function resourceItems(actor) {
  return Array.isArray(actor?.resources) ? actor.resources.filter((item) =>
    item && typeof item === "object" && String(item.label || "").trim()) : [];
}

export function renderActorResources(actor, uiProfile = {}, { detail = false } = {}) {
  const policy = uiProfile?.party?.resources;
  if (!policy) return null;
  const all = resourceItems(actor);
  const limit = detail ? all.length : Math.max(0, Math.min(3, Number(policy.max_compact) || 3));
  const items = all.slice(0, limit);
  if (!items.length) return null;
  const wrapper = node("section", "tavern-actor-resources");
  for (const item of items) {
    const value = finite(item.value ?? item.current);
    const minimum = finite(item.min) ?? 0;
    const maximum = finite(item.max ?? item.total);
    const unit = String(item.unit || "").trim();
    const valueLabel = item.value_label || [
      value === null ? "" : value,
      maximum === null ? "" : `/ ${maximum}`,
      unit,
    ].filter((part) => part !== "").join(" ");
    const card = node("article", "tavern-resource");
    const heading = node("div");
    heading.append(node("span", "", item.label), node("strong", "", valueLabel));
    card.append(heading);
    if (value !== null && maximum !== null && maximum > minimum) {
      const meter = node("span", "tavern-resource-meter");
      meter.setAttribute("role", "progressbar");
      meter.setAttribute("aria-label", `${item.label} ${valueLabel}`);
      meter.setAttribute("aria-valuemin", String(minimum));
      meter.setAttribute("aria-valuemax", String(maximum));
      meter.setAttribute("aria-valuenow", String(value));
      const fill = node("i");
      const ratio = Math.max(0, Math.min(1, (value - minimum) / (maximum - minimum)));
      fill.style.setProperty("--tavern-meter-value", `${Math.round(ratio * 100)}%`);
      meter.append(fill);
      card.append(meter);
    }
    if (item.summary) card.append(node("small", "", item.summary));
    wrapper.append(card);
  }
  return wrapper;
}
