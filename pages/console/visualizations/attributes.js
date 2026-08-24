import { copy } from "../copy/catalog.js";
function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== "") node.textContent = String(text);
  return node;
}

function finite(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function axesFor(actor, visualization) {
  const source = Array.isArray(actor?.attribute_view?.axes)
    ? actor.attribute_view.axes
    : Array.isArray(actor?.attributes)
      ? actor.attributes
      : [];
  const handles = new Set(visualization?.role_handles || []);
  return source.filter((axis) => {
    if (!axis || typeof axis !== "object") return false;
    if (!handles.size) return true;
    return handles.has(axis.role_handle || axis.handle);
  }).map((axis) => ({
    label: String(axis.label || "").trim(),
    value: finite(axis.value),
    min: finite(axis.min),
    max: finite(axis.max),
    roleHandle: String(axis.role_handle || axis.handle || ""),
  })).filter((axis) => axis.label && axis.value !== null);
}

function sharedScale(axes, scale = {}) {
  const declaredMin = finite(scale.min);
  const declaredMax = finite(scale.max);
  if (declaredMin !== null && declaredMax !== null && declaredMax > declaredMin) {
    if (axes.some((axis) =>
      (axis.min !== null && axis.min !== declaredMin)
      || (axis.max !== null && axis.max !== declaredMax))) return null;
    return { min: declaredMin, max: declaredMax, unit: String(scale.unit || "") };
  }
  const pairs = new Set(axes.map((axis) => `${axis.min}:${axis.max}`));
  if (pairs.size !== 1) return null;
  const first = axes[0];
  if (first.min === null || first.max === null || first.max <= first.min) return null;
  return { min: first.min, max: first.max, unit: String(scale.unit || "") };
}

function valueText(axis, unit = "") {
  return `${axis.value}${unit ? ` ${unit}` : ""}`;
}

function valueList(axes, unit, className = "tavern-attribute-list") {
  const list = element("dl", className);
  for (const axis of axes) {
    const row = element("div");
    row.append(element("dt", "", axis.label), element("dd", "", valueText(axis, unit)));
    list.append(row);
  }
  return list;
}

function bars(axes, scale) {
  const wrapper = element("div", "tavern-attribute-bars");
  for (const axis of axes) {
    const localMin = axis.min ?? scale?.min;
    const localMax = axis.max ?? scale?.max;
    const valid = localMin !== null && localMin !== undefined
      && localMax !== null && localMax !== undefined && localMax > localMin;
    const row = element("div", "tavern-attribute-bar");
    const header = element("div");
    header.append(element("span", "", axis.label), element("strong", "", valueText(axis, scale?.unit)));
    row.append(header);
    if (valid) {
      const meter = element("span", "tavern-resource-meter");
      meter.setAttribute("role", "progressbar");
      meter.setAttribute("aria-label", `${axis.label} ${valueText(axis, scale?.unit)}`);
      meter.setAttribute("aria-valuemin", String(localMin));
      meter.setAttribute("aria-valuemax", String(localMax));
      meter.setAttribute("aria-valuenow", String(axis.value));
      const fill = element("i");
      const ratio = Math.max(0, Math.min(1, (axis.value - localMin) / (localMax - localMin)));
      fill.style.setProperty("--tavern-meter-value", `${Math.round(ratio * 100)}%`);
      meter.append(fill);
      row.append(meter);
    }
    wrapper.append(row);
  }
  return wrapper;
}

function svgNode(name, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

function radar(axes, scale, title) {
  const figure = element("figure", "tavern-attribute-radar");
  const caption = element("figcaption", "", title);
  const svg = svgNode("svg", { viewBox: "0 0 240 240", role: "img", "aria-label": title });
  const center = 120;
  const radius = 76;
  const pointAt = (index, ratio = 1) => {
    const angle = (-Math.PI / 2) + (index * Math.PI * 2 / axes.length);
    return [center + Math.cos(angle) * radius * ratio, center + Math.sin(angle) * radius * ratio];
  };
  for (const ring of [0.33, 0.66, 1]) {
    svg.append(svgNode("polygon", {
      class: "tavern-radar-ring",
      points: axes.map((_axis, index) => pointAt(index, ring).join(",")).join(" "),
    }));
  }
  const values = [];
  axes.forEach((axis, index) => {
    const [x, y] = pointAt(index);
    svg.append(svgNode("line", { class: "tavern-radar-axis", x1: center, y1: center, x2: x, y2: y }));
    const ratio = Math.max(0, Math.min(1, (axis.value - scale.min) / (scale.max - scale.min)));
    values.push(pointAt(index, ratio).join(","));
    const [labelX, labelY] = pointAt(index, 1.27);
    const label = svgNode("text", { x: labelX, y: labelY, "text-anchor": "middle" });
    label.textContent = `${axis.label} ${valueText(axis, scale.unit)}`;
    svg.append(label);
  });
  svg.append(svgNode("polygon", { class: "tavern-radar-value", points: values.join(" ") }));
  figure.append(caption, svg, valueList(axes, scale.unit, "tavern-attribute-fallback"));
  return figure;
}

export function renderActorAttributes(actor, uiProfile = {}) {
  const declared = Array.isArray(uiProfile.visualizations) ? uiProfile.visualizations : [];
  const visualizations = declared.length ? declared : Array.isArray(actor?.attribute_view?.axes)
    ? [{
      kind: actor.attribute_view.kind || "list",
      title: actor.attribute_view.title || copy("pages.designer.rc8.86de52d178"),
      scale: actor.attribute_view.scale || {},
      fallback: actor.attribute_view.fallback || "list",
      role_handles: actor.attribute_view.axes.map((axis) => axis?.role_handle || axis?.handle).filter(Boolean),
    }]
    : [];
  const wrapper = element("div", "tavern-actor-attributes");
  for (const visualization of visualizations) {
    const axes = axesFor(actor, visualization);
    if (!axes.length) continue;
    const title = visualization.title || copy("pages.designer.rc8.86de52d178");
    const section = element("section", "tavern-attribute-view");
    section.append(element("h4", "", title));
    const scale = sharedScale(axes, visualization.scale || {});
    if (visualization.kind === "radar" && axes.length >= 3 && axes.length <= 10 && scale) {
      section.append(radar(axes, scale, title));
    } else if (visualization.fallback === "bars" || actor?.attribute_view?.fallback === "bars") {
      section.append(bars(axes, scale));
    } else {
      section.append(valueList(axes, scale?.unit || ""));
    }
    wrapper.append(section);
  }
  return wrapper.childElementCount ? wrapper : null;
}
