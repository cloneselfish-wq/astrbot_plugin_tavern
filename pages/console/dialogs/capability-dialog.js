import { openDetail } from "./detail-dialog.js";

export const CAPABILITY_GROUPS = Object.freeze({
  session: Object.freeze([
    "opening", "companions", "growth", "economy", "recovery", "group-policy",
  ]),
  author: Object.freeze([
    "world-package", "world-setting", "author-edit", "author-artifact", "resolution",
  ]),
  system: Object.freeze([
    "providers", "panel-status", "extensions", "maintenance",
  ]),
});

export const CAPABILITY_PANEL_IDS = Object.freeze(
  Object.values(CAPABILITY_GROUPS).flat(),
);

const CAPABILITY_SET = new Set(CAPABILITY_PANEL_IDS);
const GROUP_BY_PANEL = new Map(
  Object.entries(CAPABILITY_GROUPS).flatMap(([group, ids]) =>
    ids.map((id) => [id, group])),
);

function normalizedPanels(panels) {
  const seen = new Set();
  const byId = new Map(
    (Array.isArray(panels) ? panels : [])
      .filter((panel) => panel?.id && panel?.label)
      .map((panel) => [String(panel.id), panel]),
  );
  return CAPABILITY_PANEL_IDS.flatMap((id) => {
    const panel = byId.get(id);
    if (!panel || seen.has(id) || !CAPABILITY_SET.has(id)) return [];
    seen.add(id);
    return [{ ...panel, id, group: GROUP_BY_PANEL.get(id) }];
  });
}

export function openCapabilityDialog(manager, {
  opener,
  title,
  kicker = "",
  footerLabel = "",
  objectKey = "",
  panels = [],
  activePanel = "",
  permissions = {},
  lazyPanelLoader,
  onClose,
} = {}) {
  const allowedPanels = normalizedPanels(panels);
  const visiblePanels = allowedPanels.filter((panel) => permissions[panel.id] !== false);
  const dialog = openDetail(manager, {
    objectKey,
    opener,
    title,
    kicker,
    footerLabel,
    tabs: visiblePanels,
    activeTab: activePanel,
    permissions,
    specialization: "capability",
    cachePanels: true,
    lazyPanelLoader: (panelId, key, context) => lazyPanelLoader?.(
      panelId,
      key,
      {
        ...context,
        panel: visiblePanels.find((item) => item.id === panelId) || null,
      },
    ),
    onTabChange: (_panelId, panel, currentDialog) => {
      currentDialog.dataset.capabilityGroup = panel?.group || "";
      const heading = currentDialog.querySelector(".tavern-dialog-header h2");
      if (heading && panel?.label) heading.textContent = panel.label;
    },
    onClose,
  });
  dialog.classList.add("tavern-capability-dialog");
  dialog.dataset.capabilityDialog = "true";
  dialog.dataset.capabilityCount = String(visiblePanels.length);
  return dialog;
}
