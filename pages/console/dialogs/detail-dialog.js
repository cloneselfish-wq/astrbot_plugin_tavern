import { copy } from "../copy/catalog.js";
import {
  canonicalStateFamily,
  renderStatePanel,
} from "../components/empty-problem.js";
import { testToken } from "../components/dom.js";
import { rovingTabs } from "./dialog-manager.js";

let detailSequence = 0;

function detailState(family) {
  if (family === "empty") return "ready";
  return ["ready", "loading", "partial", "permission"].includes(family)
    ? family
    : "error";
}

export function openDetail(manager, {
  objectKey,
  opener,
  title,
  tabs = [],
  activeTab = "",
  lazyPanelLoader,
  permissions = {},
  specialization = "object",
  kicker = "",
  summaryFacts = [],
  footerLabel = "",
  cachePanels = true,
  onTabChange = () => {},
  onClose,
} = {}) {
  const allowedTabs = tabs.filter((item) => item?.id && item?.label && permissions[item.id] !== false);
  const instance = ++detailSequence;
  const content = document.createElement("div");
  content.className = "tavern-detail-dialog";
  content.dataset.detailSpecialization = specialization;
  const summary = document.createElement("aside");
  summary.className = "tavern-dialog-summary";
  summary.setAttribute("aria-label", title);
  const visibleFacts = (Array.isArray(summaryFacts) ? summaryFacts : [])
    .filter((fact) => fact?.label && fact?.value !== undefined && fact?.value !== null && fact?.value !== "");
  summary.append(...visibleFacts.map((fact) => {
    const item = document.createElement("div");
    item.className = "tavern-dialog-summary-fact";
    const label = document.createElement("small");
    label.textContent = fact.label;
    const value = document.createElement("strong");
    value.textContent = String(fact.value);
    item.append(label, value);
    return item;
  }));
  if (!visibleFacts.length) summary.hidden = true;
  const tablist = document.createElement("div");
  tablist.className = "tavern-dialog-tabs";
  tablist.setAttribute("role", "tablist");
  tablist.setAttribute(
    "aria-label",
    copy("dialogs.dialogs.openDetail.message.22922170bd", { p0: title }),
  );
  const panel = document.createElement("section");
  panel.className = "tavern-dialog-panel";
  panel.id = `tavern-detail-panel-${instance}`;
  panel.setAttribute("role", "tabpanel");
  panel.tabIndex = 0;
  const selectedTab = allowedTabs.some((tab) => tab.id === activeTab)
    ? activeTab
    : allowedTabs[0]?.id || "";
  for (const tab of allowedTabs) {
    const tabToken = testToken(tab.id, "tab");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tavern-button tavern-dialog-tab";
    button.setAttribute("role", "tab");
    button.id = `tavern-detail-tab-${instance}-${tabToken}`;
    button.dataset.tab = tab.id;
    if (tab.group) button.dataset.tabGroup = tab.group;
    button.textContent = tab.label;
    button.setAttribute("aria-controls", panel.id);
    button.setAttribute("aria-selected", String(tab.id === selectedTab));
    tablist.append(button);
  }
  content.append(summary, tablist, panel);
  let requestSequence = 0;
  let activeController = null;
  const panelCache = new Map();
  const close = (reason) => {
    activeController?.abort();
    onClose?.(reason);
  };
  const footer = footerLabel ? document.createElement("footer") : null;
  if (footer) {
    footer.className = "tavern-capability-dialog-actions";
    const footerClose = document.createElement("button");
    footerClose.type = "button";
    footerClose.className = "tavern-button";
    footerClose.dataset.variant = "quiet";
    footerClose.textContent = footerLabel;
    footerClose.addEventListener("click", () => manager.close("button"));
    footer.append(footerClose);
  }
  const dialog = manager.openDialog({
    kind: "detail",
    specialization,
    opener,
    title,
    kicker,
    size: "large",
    content,
    footer,
    lazyPanels: true,
    onClose: close,
  });
  dialog.dataset.detailSpecialization = specialization;
  dialog.dataset.detailState = allowedTabs.length ? "loading" : "permission";
  if (!allowedTabs.length) {
    panel.replaceChildren(renderStatePanel({
      phase: "permission",
      operation: copy("dialogs.dialogs.close.operation.e4c16a6055"),
      problem: {
        message: copy("dialogs.dialogs.close.message.9e333cec71"),
        recovery: copy("dialogs.dialogs.close.recovery.e3e1bf0ca8"),
      },
    }));
    return dialog;
  }
  let activateTab = () => {};
  activateTab = rovingTabs(tablist, async (tab) => {
    const sequence = ++requestSequence;
    activeController?.abort();
    activeController = null;
    const selected = allowedTabs.find((item) => item.id === tab);
    const selectedButton = [...tablist.querySelectorAll('[role="tab"]')]
      .find((button) => button.dataset.tab === tab);
    panel.setAttribute("aria-labelledby", selectedButton?.id || "");
    panel.dataset.panel = tab;
    onTabChange(tab, selected, dialog);
    if (cachePanels && panelCache.has(tab)) {
      const cached = panelCache.get(tab);
      panel.replaceChildren(cached.node);
      panel.setAttribute("aria-busy", "false");
      panel.dataset.phase = cached.phase;
      dialog.dataset.detailState = detailState(cached.phase);
      manager.onUrlState({ dialog: "detail", specialization, objectKey, tab });
      return;
    }
    activeController = new AbortController();
    panel.setAttribute("aria-busy", "true");
    panel.dataset.phase = "loading";
    dialog.dataset.detailState = "loading";
    panel.replaceChildren(renderStatePanel({
      phase: "loading",
      operation: copy("dialogs.dialogs.activateTab.operation.e4c16a6055"),
    }));
    try {
      const node = await lazyPanelLoader?.(tab, objectKey, {
        signal: activeController.signal,
        tab: selected,
      });
      if (sequence !== requestSequence || activeController.signal.aborted) return;
      const resolved = node || renderStatePanel({
        phase: "empty",
        emptyCopy: copy("dialogs.dialogs.activateTab.emptyCopy.1d672315b2"),
      });
      const resolvedPhase = node
        ? canonicalStateFamily(node?.dataset?.phase || "ready")
        : "empty";
      if (cachePanels) panelCache.set(tab, { node: resolved, phase: resolvedPhase });
      panel.replaceChildren(resolved);
      panel.setAttribute("aria-busy", "false");
      panel.dataset.phase = resolvedPhase;
      dialog.dataset.detailState = detailState(resolvedPhase);
      manager.onUrlState({ dialog: "detail", specialization, objectKey, tab });
    } catch (error) {
      if (error?.name === "AbortError" || sequence !== requestSequence) return;
      const forbidden = error?.status === 403;
      const conflict = error?.status === 409;
      const family = forbidden
        ? "permission"
        : conflict
          ? "conflict"
          : canonicalStateFamily("error", error);
      panel.replaceChildren(renderStatePanel({
        phase: family,
        operation: copy("dialogs.dialogs.activateTab.operation.e4c16a6055"),
        problem: error,
        retryAction: () => {
          const index = allowedTabs.findIndex((item) => item.id === tab);
          activateTab(index, { focus: false });
        },
      }));
      panel.setAttribute("aria-busy", "false");
      panel.dataset.phase = family;
      dialog.dataset.detailState = detailState(family);
    }
  });
  return dialog;
}
