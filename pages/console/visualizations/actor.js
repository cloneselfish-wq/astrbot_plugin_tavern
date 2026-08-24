import { copy } from "../copy/catalog.js";
import { renderActorAttributes } from "./attributes.js";
import { renderActorResources, resourceItems } from "./resources.js";
import { renderStatusEffects } from "./status-effects.js";

const ACTION_STATES = Object.freeze({
  current: copy("pages.live_session.actorPanel.label.ef26096871"),
  acting: copy("visualizations.actor.rc8.3371480c47"),
  ready: copy("visualizations.actor.rc8.04f3d181ba"),
  waiting: copy("dialogs.session_detail.partyPanel.message.814b6a6c04"),
  awaiting_confirmation: copy("visualizations.actor.rc8.e5d2184f2f"),
  paused: copy("visualizations.actor.rc8.7accc34240"),
  recovering: copy("pages.dashboard.publicState.message.7266e439cc"),
  blocked: copy("visualizations.actor.rc8.ed5448dfae"),
});

function node(tag, attributes = {}, children = []) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === undefined || value === null || value === false) continue;
    if (name === "class") element.className = String(value);
    else if (name === "text") element.textContent = String(value);
    else if (name === "dataset") Object.assign(element.dataset, value);
    else element.setAttribute(name, String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) if (child) element.append(child);
  return element;
}

function actorKind(item) {
  return item?.kind === "ai_companion" || item?.kind === "ai"
    ? copy("pages.live_session.renderPartyLens.message.1d97be5795")
    : copy("pages.live_session.renderPartyLens.message.3c0e873ef0");
}

function actionState(item) {
  return ACTION_STATES[item?.action_state]
    || (item?.is_current ? ACTION_STATES.current : copy("pages.live_session.renderPartyLens.message.814b6a6c04"));
}

function identityFacets(item, uiProfile, detail) {
  if (!Array.isArray(uiProfile?.party?.identity_facets) || !uiProfile.party.identity_facets.length) return [];
  const facets = Array.isArray(item?.identity_facets) ? item.identity_facets : [];
  return facets.slice(0, detail ? facets.length : 3).filter((facet) =>
    facet && facet.label && facet.value !== undefined && String(facet.value).trim());
}

function inventoryItems(item) {
  const inventory = item?.inventory;
  if (Array.isArray(inventory)) return inventory;
  return Array.isArray(inventory?.items) ? inventory.items : [];
}

function capabilityItems(item) {
  const capabilities = item?.capabilities;
  if (Array.isArray(capabilities)) return capabilities;
  return Array.isArray(capabilities?.items) ? capabilities.items : [];
}

function finite(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function framed(value, left, right) {
  const text = String(value || "").trim();
  if (!text) return "";
  return /^[「〔【『〈《]/.test(text) ? text : `${left}${text}${right}`;
}

function compactVital(resource, index) {
  const current = finite(resource?.value ?? resource?.current);
  const maximum = finite(resource?.max ?? resource?.total);
  if (current === null || maximum === null || maximum <= 0) return null;
  const ratio = Math.max(0, Math.min(100, Math.round(current / maximum * 100)));
  return node("div", {
    class: `tavern-actor-vital${index === 1 ? " tavern-is-stress" : ""}`,
    style: `--tavern-vital-value:${ratio}`,
    role: "img",
    "aria-label": `${resource.label} ${current} / ${maximum}`,
  }, [
    node("div", {}, [node("strong", { text: current }), node("small", { text: `/ ${maximum}` })]),
    node("span", { text: resource.label }),
  ]);
}

function renderCompactVitals(item, uiProfile) {
  const resourceVisible = Boolean(uiProfile?.party?.resources);
  const inventoryVisible = Boolean(uiProfile?.party?.inventory);
  const capabilityVisible = Boolean(
    uiProfile?.party?.capabilities
    || (uiProfile?.actor_detail?.sections || []).includes("capabilities")
  );
  if (!resourceVisible && !inventoryVisible && !capabilityVisible) return null;
  const vitals = resourceVisible
    ? resourceItems(item).map(compactVital).filter(Boolean).slice(0, 2) : [];
  const ability = capabilityVisible ? capabilityItems(item)[0] : null;
  const equipment = inventoryVisible ? inventoryItems(item)[0] : null;
  const primary = [
    ability ? framed(ability.label || ability.name, "〈", "〉") : "",
    equipment ? `${framed(equipment.label || equipment.name, "『", "』")}${equipment.quantity ? ` ×${equipment.quantity}` : ""}` : "",
  ].filter(Boolean).join(" · ") || "暂无公开关键能力或装备";
  const capabilities = item?.capabilities;
  const secondary = (
    (item?.kind === "ai_companion" || item?.kind === "ai")
    && capabilities && !Array.isArray(capabilities) && capabilities.requires_confirmation
  ) ? "AI 策略：确认后行动" : String(item?.participation_label || item?.participation_state || "").trim();
  return node("section", { class: "tavern-actor-vitals" }, [
    ...vitals,
    node("div", { class: "tavern-actor-quick-fact" }, [
      node("small", { text: "关键能力 / 装备" }),
      node("strong", { text: primary }),
      secondary ? node("span", { text: secondary }) : null,
    ]),
  ]);
}

function tagSection(className, items, limit, valueFor) {
  const selected = items.slice(0, limit).filter((item) => item && (item.label || item.name));
  if (!selected.length) return null;
  return node("section", { class: className }, selected.map((item) =>
    node("span", { text: valueFor(item) })));
}

function renderIdentity(item, uiProfile, detail) {
  const facets = identityFacets(item, uiProfile, detail);
  if (!facets.length) return null;
  return node("dl", { class: "tavern-actor-facets" }, facets.map((facet) =>
    node("div", {}, [
      node("dt", { text: facet.label }),
      node("dd", { text: facet.value }),
    ])));
}

function renderInventory(item, uiProfile, detail) {
  const policy = uiProfile?.party?.inventory;
  if (!policy) return null;
  const items = inventoryItems(item);
  const limit = detail ? items.length : Math.max(0, Math.min(3, Number(policy.max_compact) || 3));
  if (detail) {
    const visible = items.slice(0, limit).filter((entry) => entry && (entry.label || entry.name));
    if (!visible.length) return null;
    return node("div", { class: "tavern-actor-equipment-grid" }, visible.map((entry) => {
      const itemLabel = String(entry.label || entry.name);
      return node("article", { class: "tavern-actor-equipment" }, [
        node("span", { text: itemLabel.slice(0, 1), "aria-hidden": "true" }),
        node("div", {}, [
          node("strong", { text: `${framed(itemLabel, "『", "』")}${entry.quantity ? ` ×${entry.quantity}` : ""}` }),
          node("small", { text: entry.summary || entry.kind || entry.type || "公开携行物品" }),
        ]),
      ]);
    }));
  }
  return tagSection("tavern-actor-inventory", items, limit, (entry) =>
    `${entry.label || entry.name}${entry.quantity ? ` ×${entry.quantity}` : ""}`);
}

function renderCapabilities(item, uiProfile, detail) {
  const declared = uiProfile?.party?.capabilities
    || (uiProfile?.actor_detail?.sections || []).includes("capabilities");
  if (!declared) return null;
  const items = capabilityItems(item);
  const limit = detail ? items.length : Math.min(3, items.length);
  if (detail) {
    const visible = items.slice(0, limit).filter((entry) => entry && (entry.label || entry.name));
    if (!visible.length) return null;
    return node("div", { class: "tavern-actor-capability-list" }, visible.map((entry) =>
      node("article", {}, [
        node("strong", { text: framed(entry.label || entry.name, "〈", "〉") }),
        node("span", { text: entry.summary || entry.value || "已公开能力" }),
      ])));
  }
  return tagSection("tavern-actor-capabilities", items, limit, (entry) =>
    [entry.label || entry.name, entry.summary || entry.value].filter(Boolean).join(" · "));
}

export function renderActorCard(item, {
  uiProfile = {},
  onOpen = null,
  kind = "teammate",
} = {}) {
  const name = item?.name || item?.label || copy("pages.live_session.renderPartyLens.message.6be2b9ac7d");
  const displayName = name.replace(/^[「〔【『<〈《]+|[」〕】』>〉》]+$/g, "");
  const ai = item?.kind === "ai_companion" || item?.kind === "ai";
  const article = node("article", {
    class: `tavern-actor-card${item?.is_current ? " tavern-is-current" : ""}`,
    dataset: {
      kind,
      actorKind: ai ? "ai_companion" : "human",
      selected: String(Boolean(item?.is_current)),
    },
  });
  const avatar = node("span", {
    class: `tavern-actor-avatar${ai ? " tavern-is-ai" : ""}`,
    text: displayName.slice(0, 1),
    "aria-hidden": "true",
  });
  const title = node("div", { class: "tavern-actor-title" }, [
    node("h4", { text: `「${displayName}」` }),
    node("p", { text: [actorKind(item), identityFacets(item, uiProfile, false).at(-1)?.value].filter(Boolean).join(" · ") }),
  ]);
  const state = node("span", {
    class: "tavern-actor-state",
    text: actionState(item),
    dataset: { state: item?.action_state || (item?.is_current ? "current" : "waiting") },
  });
  article.append(node("header", {}, [avatar, title, state]));
  for (const section of [
    renderIdentity(item, uiProfile, false),
    renderCompactVitals(item, uiProfile),
    renderStatusEffects(item, uiProfile),
  ]) if (section) article.append(section);
  if (typeof onOpen === "function" && uiProfile?.party?.open_detail !== false) {
    const button = node("button", {
      class: "tavern-button tavern-actor-open",
      type: "button",
      "data-variant": "quiet",
      text: "查看完整角色",
      "aria-label": copy("visualizations.actor.detail_label", { p0: name }),
    });
    button.addEventListener("click", (event) => onOpen(item, event.currentTarget));
    article.append(button);
  }
  return article;
}

function detailAllowed(uiProfile, section) {
  const declared = uiProfile?.actor_detail?.sections;
  return !Array.isArray(declared) || !declared.length || declared.includes(section);
}

function detailSection(title, id, content) {
  if (!content) return null;
  return node("section", { class: "tavern-actor-detail-section", dataset: { detailSection: id } }, [
    node("h4", { text: title }),
    content,
  ]);
}

function renderDetailState(item, uiProfile) {
  const content = node("div", { class: "tavern-actor-detail-state" });
  const resources = renderActorResources(item, uiProfile, { detail: true });
  const statuses = renderStatusEffects(item, uiProfile, { detail: true });
  if (resources) content.append(resources);
  if (statuses) content.append(statuses);
  return content.childElementCount ? content : null;
}

export function renderActorDetail(item, uiProfile = {}) {
  const name = item?.name || item?.label || copy("pages.live_session.renderPartyLens.message.6be2b9ac7d");
  const displayName = name.replace(/^[「〔【『<〈《]+|[」〕】』>〉》]+$/g, "");
  const root = node("div", { class: "tavern-actor-detail" });
  root.append(node("header", { class: "tavern-actor-detail-hero" }, [
    node("span", {
      class: `tavern-actor-avatar${item?.kind === "ai_companion" || item?.kind === "ai" ? " tavern-is-ai" : ""}`,
      text: displayName.slice(0, 1),
      "aria-hidden": "true",
    }),
    node("div", {}, [node("h3", { text: `「${displayName}」` }), node("p", { text: actorKind(item) })]),
    node("span", { class: "tavern-actor-state", text: actionState(item), dataset: { state: item?.action_state || "waiting" } }),
  ]));
  const identity = detailAllowed(uiProfile, "identity") ? renderIdentity(item, uiProfile, true) : null;
  if (identity) root.append(node("section", { class: "tavern-actor-detail-identity", dataset: { detailSection: "identity" } }, [identity]));
  const overview = [
    detailAllowed(uiProfile, "attributes")
      ? detailSection("公开属性轮廓", "attributes", renderActorAttributes(item, uiProfile)) : null,
    (detailAllowed(uiProfile, "resources") || detailAllowed(uiProfile, "statuses"))
      ? detailSection("资源与状态效果", "state", renderDetailState(item, uiProfile)) : null,
  ].filter(Boolean);
  if (overview.length) root.append(node("div", { class: "tavern-actor-detail-overview" }, overview));
  const lower = [
    detailAllowed(uiProfile, "inventory")
      ? detailSection("装备与携行", "inventory", renderInventory(item, uiProfile, true)) : null,
    detailAllowed(uiProfile, "capabilities")
      ? detailSection("能力与限制", "capabilities", renderCapabilities(item, uiProfile, true)) : null,
  ].filter(Boolean);
  if (lower.length) root.append(node("div", { class: "tavern-actor-detail-grid" }, lower));
  return root;
}
