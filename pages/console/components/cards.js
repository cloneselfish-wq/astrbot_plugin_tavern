import { copy } from "../copy/catalog.js";
import { createElement, testToken } from "./dom.js";
import { renderStatusBadge } from "./status.js";

export function renderBusinessCard({
  kind = "item",
  title,
  summary = "",
  state = null,
  meta = [],
  className = "",
  kicker = "",
  header = null,
  body = null,
  footer = null,
  actions = [],
  selected = false,
  opaqueKey = "item",
} = {}) {
  const safeMeta = Array.isArray(meta)
    ? meta.filter((item) => item && typeof item === "object")
    : [];
  const safeActions = Array.isArray(actions) ? actions.filter(Boolean) : [];
  const card = createElement("article", {
    class: `tavern-business-card ${className}`.trim(),
    "data-kind": kind,
    "data-selected": String(Boolean(selected)),
    "data-testid": `tavern-card-${testToken(kind)}-${testToken(opaqueKey)}`,
  });
  if (header) card.append(header);
  else {
    card.append(createElement("header", { class: "tavern-card-header" }, [
      createElement("div", {}, [
        kicker ? createElement("span", { class: "tavern-card-kicker", text: kicker }) : null,
        createElement("h3", { text: title }),
      ]),
      state ? renderStatusBadge(state) : null,
    ]));
  }
  if (summary) card.append(createElement("p", { text: summary }));
  if (safeMeta.length) {
    card.append(createElement("dl", {}, safeMeta.flatMap(({ label: term, value }) => [
      createElement("dt", { text: term }),
      createElement("dd", { text: value }),
    ])));
  }
  if (body) card.append(body);
  if (footer) card.append(footer);
  if (safeActions.length) {
    card.append(createElement("div", { class: "tavern-card-actions" }, safeActions));
  }
  return card;
}

export function renderMetricStrip({ workspace, metrics = [], max = 4 } = {}) {
  const limit = Math.max(0, Math.min(4, Number(max) || 0));
  const safeMetrics = Array.isArray(metrics) ? metrics : [];
  const section = createElement("section", {
    class: "tavern-metric-strip",
    "aria-label": copy("components.primitives.renderMetricStrip.message.5c9071894b"),
    "data-testid": `tavern-metrics-${workspace}`,
  });
  for (const metric of safeMetrics.slice(0, limit)) {
    if (!metric || metric.value === undefined || metric.value === null || !metric.label) continue;
    section.append(createElement("article", {
      class: "tavern-metric",
      "data-tone": ["amber", "blue", "danger", "jade"].includes(metric.tone) ? metric.tone : "amber",
      "data-metric-key": metric.key || undefined,
    }, [
      createElement("span", { text: metric.label }),
      createElement("strong", { text: metric.value }),
      metric.detail ? createElement("small", { text: metric.detail }) : null,
    ]));
  }
  return section;
}

export function renderDensityStrip({
  workspace,
  stats = [],
  max = 8,
  onNavigate = null,
} = {}) {
  const limit = Math.max(0, Math.min(8, Number(max) || 0));
  const safeStats = Array.isArray(stats) ? stats : [];
  const section = createElement("section", {
    class: "tavern-density-strip",
    "aria-label": copy("components.cards.density.aria"),
    "data-testid": `tavern-density-${workspace}`,
  });
  for (const stat of safeStats.slice(0, limit)) {
    if (!stat || stat.value === undefined || stat.value === null || !stat.label) continue;
    const canNavigate = Boolean(stat.navigate_to) && typeof onNavigate === "function";
    const node = createElement(canNavigate ? "button" : "article", {
      class: "tavern-density-stat",
      type: canNavigate ? "button" : undefined,
      "data-density-key": stat.key || undefined,
      onclick: canNavigate ? () => onNavigate(String(stat.navigate_to)) : undefined,
    }, [
      createElement("span", { text: stat.label }),
      createElement("strong", { text: stat.value }),
      stat.detail ? createElement("small", { text: stat.detail }) : null,
    ]);
    section.append(node);
  }
  return section;
}
