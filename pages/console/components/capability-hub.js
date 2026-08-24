import { copy } from "../copy/catalog.js";
import { openCapabilityDialog } from "../dialogs/capability-dialog.js";
import { renderButton } from "./buttons.js";

function rows(value) { return Array.isArray(value) ? value : []; }

const GROUP_KICKERS = Object.freeze({
  session: copy("components.capability_hub.kicker.session"),
  author: copy("components.capability_hub.kicker.author"),
  system: copy("components.capability_hub.kicker.system"),
});

function textNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(text ?? "");
  return node;
}

function visibleFacts(panel) {
  return rows(panel?.facts).filter((fact) => fact?.label && fact?.value !== undefined && fact?.value !== null);
}

function displayValue(value) {
  return value === undefined || value === null || value === ""
    ? copy("components.capability_hub.not_available")
    : String(value);
}

function kickerFor(group) {
  return GROUP_KICKERS[group] || copy("components.capability_hub.kicker.default");
}

function capabilitySection(title, summary, facts, variant = "list") {
  const section = document.createElement("article");
  section.className = `tavern-capability-section tavern-capability-section-${variant}`;
  const header = document.createElement("header");
  header.className = "tavern-capability-section-head";
  const heading = document.createElement("div");
  heading.append(textNode("h4", "", title), textNode("p", "", summary));
  header.append(heading);
  const body = document.createElement("div");
  body.className = variant === "flow" ? "tavern-capability-flow" : "tavern-capability-list";
  for (const fact of facts) {
    const item = document.createElement("div");
    item.append(textNode(variant === "flow" ? "small" : "span", "", fact.label), textNode("strong", "", displayValue(fact.value)));
    body.append(item);
  }
  section.append(header, body);
  return section;
}

function localStateSection(title, summary, value) {
  return capabilitySection(title, summary, [{ label: copy("components.capability_hub.state_label"), value }], "state");
}

function independentSections(panel, facts) {
  const first = facts.slice(0, 1);
  const rest = facts.slice(1);
  const primary = (items = facts, variant = "list") => capabilitySection(panel.label, panel.summary, items, variant);
  const state = (value = panel.state) => localStateSection(
    copy("components.capability_hub.facts_title"),
    copy("components.capability_hub.facts_summary"),
    value,
  );
  switch (panel.key) {
    case "opening": return [primary(facts, "flow"), state()];
    case "companions": return [primary(first), state(rest[0]?.value)];
    case "growth": return [primary(), state()];
    case "economy": return [primary(first), capabilitySection(panel.label, panel.boundary, rest, "state")];
    case "recovery": return [primary(facts, "flow"), state(panel.score)];
    case "group-policy": return [primary(), state()];
    case "world-package": return [primary(first), capabilitySection(panel.label, panel.boundary, rest, "flow")];
    case "author-edit": return [primary(facts, "flow"), state()];
    case "author-artifact": return [primary(first), capabilitySection(panel.label, panel.boundary, rest, "flow")];
    case "resolution": return [primary(), state()];
    case "providers": return [primary(facts, "flow"), state()];
    case "panel-status": return [primary(first), capabilitySection(panel.label, panel.boundary, rest, "state")];
    case "extensions": return [primary(first), capabilitySection(panel.label, panel.boundary, rest, "flow")];
    case "maintenance": return [primary(), state()];
    default: return [localStateSection(panel.label, panel.summary, panel.state)];
  }
}

function panelBody(panel, handlers) {
  const root = document.createElement("section");
  root.className = "tavern-capability-panel-body";
  root.dataset.phase = "ready";
  const facts = visibleFacts(panel);
  const leadingFact = facts[0] || null;

  const hero = document.createElement("header");
  hero.className = "tavern-capability-hero";
  const heroCopy = document.createElement("div");
  heroCopy.className = "tavern-capability-hero-copy";
  heroCopy.append(
    textNode("span", "tavern-story-kicker", panel.kicker || kickerFor(panel.group)),
    textNode("h3", "", panel.label),
    textNode("p", "", panel.summary),
  );
  const score = document.createElement("div");
  score.className = "tavern-capability-score";
  const scoreCopy = document.createElement("span");
  scoreCopy.append(
    textNode("strong", "", displayValue(panel.score ?? leadingFact?.value ?? panel.state)),
    textNode("small", "", panel.scoreLabel || leadingFact?.label || copy("components.capability_hub.state_label")),
  );
  score.append(scoreCopy);
  hero.append(heroCopy, score);

  const sectionGrid = document.createElement("div");
  sectionGrid.className = "tavern-capability-section-grid";
  const renderedFacts = facts.length
    ? facts
    : [{ label: copy("components.capability_hub.state_label"), value: panel.state }];
  sectionGrid.append(...independentSections(panel, renderedFacts));

  const boundary = document.createElement("div");
  boundary.className = "tavern-capability-boundary";
  boundary.append(
    textNode("strong", "", copy("components.capability_hub.boundary_title")),
    textNode("span", "", panel.boundary || copy("components.capability_hub.boundary_text")),
  );
  root.append(hero, sectionGrid, boundary);

  if (panel.workspace && handlers.navigate) {
    const actions = document.createElement("div");
    actions.className = "tavern-capability-dialog-actions";
    actions.append(renderButton({
      variant: "primary",
      label: panel.actionLabel || copy("components.capability_hub.open_workspace"),
      onActivate: () => {
        handlers.dialogs?.close?.("capability-navigate");
        handlers.navigate(panel.workspace);
      },
    }));
    root.append(actions);
  }
  return root;
}

export function renderCapabilityHub({ panels, group, title, summary = "", handlers = {} } = {}) {
  const visible = rows(panels).filter((panel) => panel?.key && panel?.label && (!group || panel.group === group));
  if (!visible.length) return null;
  const section = document.createElement("section");
  section.className = "tavern-capability-hub";
  section.dataset.capabilityGroup = group || "all";
  section.setAttribute("aria-label", title || copy("components.capability_hub.default_title"));

  const header = document.createElement("header");
  header.className = "tavern-capability-hub-head";
  const headingCopy = document.createElement("div");
  headingCopy.append(
    textNode("span", "tavern-story-kicker", kickerFor(group)),
    textNode("h2", "", title || copy("components.capability_hub.default_title")),
    textNode("p", "", summary),
  );
  header.append(
    headingCopy,
    textNode("span", "tavern-capability-hub-note", copy("components.capability_hub.count", { p0: visible.length })),
  );

  const grid = document.createElement("div");
  grid.className = "tavern-capability-hub-grid";
  const canOpen = typeof handlers.dialogs?.openDialog === "function";
  const open = (panel, opener) => canOpen ? openCapabilityDialog(handlers.dialogs, {
    opener,
    objectKey: `capability:${group || "all"}`,
    title: title || copy("components.capability_hub.default_title"),
    kicker: kickerFor(group),
    footerLabel: copy("components.capability_hub.close_details"),
    panels: visible.map((item) => ({ id: item.key, label: item.label, group: item.group })),
    activePanel: panel.key,
    permissions: Object.fromEntries(visible.map((item) => [item.key, true])),
    lazyPanelLoader: async (panelId) => panelBody(visible.find((item) => item.key === panelId) || {}, handlers),
  }) : null;

  for (const panel of visible) {
    const card = document.createElement("article");
    card.className = "tavern-capability-hub-card";
    card.dataset.capability = panel.key;
    const cardHead = document.createElement("header");
    cardHead.className = "tavern-capability-card-head";
    const cardCopy = document.createElement("div");
    cardCopy.append(textNode("h3", "", panel.label), textNode("p", "", panel.summary));
    const signal = displayValue(panel.signal || panel.label).slice(0, 3);
    cardHead.append(cardCopy, textNode("span", "tavern-capability-signal", signal));
    const meta = document.createElement("div");
    meta.className = "tavern-capability-meta";
    const facts = visibleFacts(panel).slice(0, 3);
    if (facts.length) {
      for (const fact of facts) meta.append(textNode("span", "", `${fact.label} ${displayValue(fact.value)}`));
    } else {
      meta.append(textNode("span", "", panel.state || copy("components.capability_hub.not_available")));
    }
    const button = renderButton({
      variant: "secondary",
      label: panel.openLabel || copy("components.capability_hub.open_details"),
      disabledReason: canOpen ? "" : copy("components.capability_hub.dialog_unavailable"),
      onActivate: (_intent, event) => open(panel, event.currentTarget),
    });
    card.append(cardHead, meta, button);
    grid.append(card);
  }
  section.append(header, grid);
  return section;
}

export function renderCapabilityEntry({ panels, title, summary = "", handlers = {} } = {}) {
  const visible = rows(panels).filter((panel) => panel?.key && panel?.label);
  if (!visible.length || typeof handlers.dialogs?.openDialog !== "function") return null;
  const section = document.createElement("section");
  section.className = "tavern-capability-entry";
  const entryCopy = document.createElement("div");
  entryCopy.append(
    textNode("h2", "", title || copy("components.capability_hub.default_title")),
    textNode("p", "", summary),
  );
  const button = renderButton({
    variant: "secondary",
    label: copy("components.capability_hub.view_count", { p0: visible.length }),
    onActivate: (_intent, event) => openCapabilityDialog(handlers.dialogs, {
      opener: event.currentTarget,
      objectKey: "capability:all",
      title: title || copy("components.capability_hub.default_title"),
      kicker: copy("components.capability_hub.kicker.default"),
      footerLabel: copy("components.capability_hub.close_details"),
      panels: visible.map((panel) => ({ id: panel.key, label: panel.label, group: panel.group })),
      activePanel: visible[0].key,
      permissions: Object.fromEntries(visible.map((panel) => [panel.key, true])),
      lazyPanelLoader: async (panelId) => panelBody(visible.find((panel) => panel.key === panelId) || {}, handlers),
    }),
  });
  section.append(entryCopy, button);
  return section;
}
