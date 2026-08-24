import { copy } from "../copy/catalog.js";
import { RecoveringEventStream } from "../app/client.js";
import { renderButton } from "../components/buttons.js";
import { renderBusinessCard } from "../components/cards.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { renderStatusBadge } from "../components/status.js";
import {
  el,
  label,
  pageRoot,
  rows,
  stateNotice,
  summary,
  value,
} from "./shared.js";
import {
  openActorDetail,
  openLiveSummarySheet,
  openSessionConfigurationAction,
  openSessionDetail,
} from "../dialogs/session-detail.js";
import {
  hasStructuredNarrative,
  renderNarrativeDocument,
  renderNarrativeReplay,
} from "../visualizations/narrative-document.js";
import { renderActorCard } from "../visualizations/actor.js";
import { renderTimeline } from "../visualizations/timeline.js";
import { renderGeneration } from "../visualizations/generation.js";
import { renderWorldLens } from "../visualizations/world-lens.js";
import { renderClocks, renderQuestTracks } from "../visualizations/quest-clock.js";
import { renderRelation } from "../visualizations/relation.js";
import { renderScenePath } from "../visualizations/scene-path.js";
import { formatUtc8Minute } from "../components/time.js";
import { actorFateViewModel } from "./live-session-fate.js";
import { renderActorFateConsent } from "./live-session-fate.js";
import {
  actorPanel,
  controlPanel,
  descriptorFor,
  mobileSecondaryDisclosure,
  narrativeModePanel,
  pressurePanel,
} from "./live-session-controls.js";

const CORE_LENSES = Object.freeze({
  party: {
    label: copy("pages.live_session.module.label.6e350af634"),
    endpoint: "dashboard/session-party",
  },
  replay: {
    label: copy("pages.live_session.module.label.27b68ef555"),
    endpoint: "dashboard/session-history",
  },
  generation: {
    label: copy("pages.live_session.module.label.1ad1463fe1"),
    endpoint: "dashboard/session-generation",
    adminOnly: true,
  },
});

const LENS_KINDS = Object.freeze({
  session_party: "party",
  party: "party",
  "party.changed": "party",
  session_world: "world",
  world_visuals: "world",
  "quest.changed": "quests",
  "clock.changed": "clocks",
  "relation.changed": "relations",
  "challenge.changed": "world",
  "tactical.changed": "world",
  session_history: "replay",
  history: "replay",
  "delivery.changed": "replay",
  session_generation: "generation",
  generation: "generation",
  "generation.changed": "generation",
  "story.committed": "replay",
});

const SUMMARY_EVENT_KINDS = new Set([
  "choice.changed",
  "vote.changed",
  "turn.changed",
  "narrative-mode.changed",
  "session.summary",
]);

const SAFE_LENS_KEY = /^[a-z][a-z0-9_-]{0,59}$/;
const STYLE_PRESETS = Object.freeze([
  { id: "dialogue_high", label: copy("pages.live_session.rc8.159add29b4"), summary: copy("pages.live_session.rc8.cf033e0d3f") },
  { id: "dialogue_soft", label: copy("pages.live_session.rc8.8e1b25fa18"), summary: copy("pages.live_session.rc8.a13796cf86") },
  { id: "balanced", label: copy("pages.live_session.rc8.2588b5b9ba"), summary: copy("pages.live_session.rc8.aecea42763") },
  { id: "description_soft", label: copy("pages.live_session.rc8.71e88326c1"), summary: copy("pages.live_session.rc8.7b439f7e96") },
  { id: "description_high", label: copy("pages.live_session.rc8.2ac9779fc0"), summary: copy("pages.live_session.rc8.bbc2c4818c") },
]);
const WORLD_VISUALIZERS = Object.freeze({
  renderQuestTracks,
  renderClocks,
  renderRelation,
  renderScenePath,
});

export function narrativeStylePanel(handlers, sessionKey) {
  const status = el("p", {
    class: "tavern-source-note tavern-narrative-style-status",
    role: "status",
    text: copy("pages.live_session.rc8.01b52b170e"),
  });
  const range = el("input", {
    class: "tavern-narrative-style-range",
    type: "range",
    min: "0",
    max: "4",
    step: "1",
    value: "2",
    "aria-label": copy("pages.live_session.rc8.081591e752"),
    "aria-valuemin": "0",
    "aria-valuemax": "4",
  });
  const currentBadge = renderStatusBadge({ state: "running", label: copy("pages.live_session.rc8.2588b5b9ba") });
  const currentSummary = el("p", {
    class: "tavern-narrative-style-summary",
    text: STYLE_PRESETS[2].summary,
  });
  const marks = el("div", {
    class: "tavern-narrative-style-marks",
    "aria-hidden": "true",
  }, STYLE_PRESETS.map((preset) => el("span", { text: preset.label })));
  const track = el("div", { class: "tavern-narrative-style-track" }, [
    range,
    marks,
  ]);
  let revision = 0;
  let canManage = false;
  let worldDefault = "";
  let worldStyleSha = "";
  let customExpectation = "";
  let useWorldDefault = true;

  const setDisabled = (button, disabled) => {
    button.disabled = Boolean(disabled);
    button.setAttribute("aria-disabled", disabled ? "true" : "false");
  };
  const selectedPreset = () => STYLE_PRESETS[Number(range.value)] || STYLE_PRESETS[2];
  const syncRange = () => {
    const index = Math.max(0, Math.min(4, Number(range.value) || 0));
    const preset = STYLE_PRESETS[index];
    range.style.setProperty("--tavern-style-progress", `${index * 25}%`);
    range.setAttribute("aria-valuenow", String(index));
    range.setAttribute("aria-valuetext", preset.label);
    currentBadge.querySelector("span:last-child").textContent = preset.label;
    currentSummary.textContent = preset.summary;
    marks.querySelectorAll("span").forEach((node, markIndex) => {
      node.dataset.selected = String(markIndex === index);
    });
  };
  range.addEventListener("input", syncRange);

  const saveCard = renderButton({ variant: "primary", label: copy("pages.live_session.rc8.0e851f3125") });
  const detailButton = renderButton({ variant: "secondary", label: copy("pages.live_session.rc8.b3c77e1052") });
  setDisabled(saveCard, true);
  setDisabled(detailButton, true);

  const persist = async ({
    closeDialog = false,
    expectation = customExpectation,
    usesWorldDefault = useWorldDefault,
  } = {}) => {
    setDisabled(saveCard, true);
    try {
      const preset = selectedPreset();
      const idempotencyKey = crypto.randomUUID();
      const envelope = await handlers.client.post("sessions/narrative-style", {
        session_key: sessionKey,
        preset_id: preset.id,
        custom_expectation: usesWorldDefault ? "" : expectation,
        expected_revision: revision,
        idempotency_key: idempotencyKey,
        source_world_style_sha: worldStyleSha,
      }, { idempotencyKey, operation: copy("pages.live_session.rc8.5ed2732ada") });
      const data = envelope.data || {};
      revision = Number(data.revision) || revision;
      customExpectation = data.custom_expectation || "";
      useWorldDefault = !customExpectation;
      status.textContent = `叙事文风已保存为${preset.label}，从下一回合生效 · 修订 ${revision}`;
      if (closeDialog) handlers.dialogs?.close("saved");
      return true;
    } catch (error) {
      status.textContent = `修改叙事文风失败：${error.message || "请求未完成"}。较新内容已保留，请刷新后重新提交草稿。`;
      return false;
    } finally {
      setDisabled(saveCard, !canManage);
    }
  };

  saveCard.addEventListener("click", () => { void persist(); });

  const openDetail = (opener) => {
    if (!handlers.dialogs) return;
    let draftExpectation = customExpectation;
    let draftUsesWorldDefault = useWorldDefault;
    const textarea = el("textarea", {
      class: "tavern-control tavern-narrative-style-textarea",
      rows: "7",
      maxlength: "600",
      placeholder: copy("pages.live_session.rc8.345875d93c"),
      "aria-label": copy("pages.live_session.rc8.c84f80ca8e"),
    });
    textarea.value = draftUsesWorldDefault ? worldDefault : draftExpectation;
    textarea.disabled = !canManage;
    const sourceState = el("strong", {
      text: draftUsesWorldDefault ? copy("pages.live_session.rc8.ed6cbf5862") : copy("pages.live_session.rc8.42f6b6198f"),
    });
    textarea.addEventListener("input", () => {
      draftExpectation = textarea.value;
      draftUsesWorldDefault = textarea.value.trim() === worldDefault.trim();
      sourceState.textContent = draftUsesWorldDefault ? copy("pages.live_session.rc8.ed6cbf5862") : copy("pages.live_session.rc8.42f6b6198f");
    });
    const restore = renderButton({
      variant: "quiet",
      label: copy("pages.live_session.rc8.8254a85081"),
      onActivate: () => {
        textarea.value = worldDefault;
        draftExpectation = "";
        draftUsesWorldDefault = true;
        sourceState.textContent = copy("pages.live_session.rc8.ed6cbf5862");
        textarea.focus();
      },
    });
    const cancel = renderButton({
      variant: "secondary",
      label: copy("pages.settings.editor.cancel"),
      onActivate: () => handlers.dialogs.close("cancel"),
    });
    const submit = renderButton({
      variant: "primary",
      label: copy("pages.live_session.rc8.744743f487"),
      onActivate: () => {
        void persist({
          closeDialog: true,
          expectation: draftExpectation,
          usesWorldDefault: draftUsesWorldDefault,
        });
      },
    });
    setDisabled(restore, !canManage);
    setDisabled(submit, !canManage);
    const content = el("div", { class: "tavern-narrative-style-editor" }, [
      el("section", { class: "tavern-narrative-style-default" }, [
        el("span", { text: copy("pages.live_session.rc8.3de91f02ae") }),
        el("p", { text: worldDefault || copy("pages.live_session.rc8.bb4dd433ce") }),
      ]),
      el("label", { class: "tavern-field tavern-narrative-style-field" }, [
        el("span", { text: copy("pages.live_session.rc8.c84f80ca8e") }),
        textarea,
        el("small", { text: copy("pages.live_session.rc8.f02c4f010a") }),
      ]),
      el("div", { class: "tavern-narrative-style-source" }, [
        sourceState,
        el("span", { text: `最多 600 字 · 当前修订 ${revision}` }),
      ]),
      restore,
    ]);
    const footer = el("footer", { class: "tavern-dialog-footer tavern-editor-actions" }, [cancel, submit]);
    handlers.dialogs.openDialog({
      kind: "editor",
      specialization: "narrative-style",
      title: copy("pages.live_session.rc8.b3c77e1052"),
      kicker: "NARRATIVE STYLE",
      size: "medium",
      opener,
      content,
      footer,
      initialFocus: canManage ? textarea : cancel,
    });
  };
  detailButton.addEventListener("click", (event) => openDetail(event.currentTarget));

  const load = async () => {
    try {
      const envelope = await handlers.client.get("sessions/narrative-style", { query: { session_key: sessionKey }, operation: copy("pages.live_session.rc8.a48b792294") });
      const data = envelope.data || {};
      revision = Number(data.revision) || 0;
      canManage = data.can_manage === true;
      const index = STYLE_PRESETS.findIndex((preset) => preset.id === data.preset_id);
      range.value = String(index < 0 ? 2 : index);
      worldDefault = data.world_default_expectation || data.world_voice || "";
      worldStyleSha = data.world_style_sha || data.source_world_style_sha || "";
      customExpectation = data.custom_expectation || "";
      useWorldDefault = !customExpectation;
      range.disabled = !canManage;
      setDisabled(saveCard, !canManage);
      setDisabled(detailButton, false);
      syncRange();
      status.textContent = `${data.public_summary || "使用世界默认提示"} · ${useWorldDefault ? "世界默认" : "会话自定义"} · 修订 ${revision} · 下次生成冻结生效`;
    } catch (error) {
      range.disabled = true;
      setDisabled(saveCard, true);
      setDisabled(detailButton, true);
      status.textContent = `读取叙事文风失败：${error.message || "服务不可用"}。系统未修改当前设置，请刷新跑团现场重试。`;
    }
  };
  syncRange();
  void load();
  return el("section", {
    class: "tavern-surface tavern-live-narrative-style",
    "data-component": "narrative-style",
    "data-testid": "tavern-live-narrative-style",
  }, [
    el("header", { class: "tavern-section-heading" }, [
      el("div", {}, [
        el("h2", { text: copy("pages.live_session.rc8.b9349cdc6b") }),
        el("p", { text: copy("pages.live_session.rc8.aa052e17d0") }),
      ]),
      currentBadge,
    ]),
    el("div", { class: "tavern-narrative-style-control" }, [
      currentSummary,
      track,
    ]),
    status,
    el("footer", { class: "tavern-narrative-style-actions" }, [detailButton, saveCard]),
  ]);
}

function safeLensKey(value) {
  const key = String(value || "").trim().toLowerCase();
  return SAFE_LENS_KEY.test(key) ? key : "";
}

function lensEndpoint(key) {
  return CORE_LENSES[key]?.endpoint || "dashboard/session-world-visuals";
}

function lensSpecs(uiProfile, principalRoles) {
  const audienceScope = principalRoles.has("admin") || principalRoles.has("dm") || principalRoles.has("host") ? "host" : "player";
  const declaredSurfaces = rows(uiProfile?.ui_surface_manifest?.surfaces)
    .filter((surface) => rows(surface?.placements).includes("live_session") && rows(surface?.audience_scopes).includes(audienceScope))
    .map((surface, index) => ({
      key: safeLensKey(surface?.surface_key),
      label: String(surface?.label || "").trim(),
      order: Number.isFinite(Number(surface?.order)) ? Number(surface.order) : index + 10,
      required: Boolean(surface?.required),
      surfaceKey: String(surface?.surface_key || ""),
    }))
    .filter((item) => item.key && item.label && item.surfaceKey);
  const compiled = (declaredSurfaces.length ? declaredSurfaces : rows(uiProfile?.live_lenses)
    .map((item, index) => ({
      key: safeLensKey(item?.key || item?.id),
      label: String(item?.label || "").trim(),
      order: Number.isFinite(Number(item?.order)) ? Number(item.order) : index + 10,
      required: Boolean(item?.required),
    }))
    .filter((item) => item.key && item.label && item.key !== "generation"));
  const byKey = new Map(compiled.map((item) => [item.key, item]));
  for (const key of ["party", "replay"]) {
    if (!byKey.has(key)) byKey.set(key, { key, label: CORE_LENSES[key].label, order: key === "party" ? 0 : 900, required: true });
  }
  if (principalRoles.has("admin")) {
    byKey.set("generation", { key: "generation", label: CORE_LENSES.generation.label, order: 1000, required: false, adminOnly: true });
  }
  return [...byKey.values()]
    .map((item) => ({ ...item, endpoint: lensEndpoint(item.key) }))
    .sort((left, right) => left.order - right.order || left.key.localeCompare(right.key));
}

export function renderSessionSelection(handlers = {}) {
  const target = handlers.canNavigate?.("sessions") ? "sessions" : "dashboard";
  return el("section", {
    class: "tavern-page tavern-page-enter tavern-live-session tavern-session-selection",
    "data-testid": "tavern-page-session_detail",
    "data-phase": "empty",
  }, [
    el("article", { class: "tavern-state-panel", "data-phase": "empty" }, [
      el("p", { class: "tavern-page-eyebrow", text: copy("pages.live_session.selection.eyebrow") }),
      el("h2", { text: copy("pages.live_session.selection.title") }),
      el("p", { text: copy("pages.live_session.selection.summary") }),
      renderButton({
        variant: "primary",
        label: target === "sessions"
          ? copy("pages.live_session.selection.sessions")
          : copy("pages.live_session.selection.dashboard"),
        onActivate: () => handlers.navigate?.(target),
      }),
    ]),
  ]);
}

export class LiveEventCoordinator {
  constructor({ activeLens = "party", after = 0, availableLenses = null, worldLenses = null } = {}) {
    this.availableLenses = new Set(availableLenses || ["party", "replay"]);
    this.worldLenses = new Set(worldLenses || []);
    this.activeLens = this.availableLenses.has(activeLens) ? activeLens : "party";
    this.lastSequence = Math.max(0, Number(after) || 0);
  }

  setActiveLens(lens) {
    if (this.availableLenses.has(lens)) this.activeLens = lens;
  }

  confirmSequence(sequence) {
    const confirmed = Math.max(0, Number(sequence) || 0);
    if (confirmed > this.lastSequence) this.lastSequence = confirmed;
    return this.lastSequence;
  }

  lensMatches(targetLens) {
    if (!targetLens) return false;
    if (targetLens === this.activeLens) return true;
    if (targetLens === "world") return this.worldLenses.has(this.activeLens);
    return false;
  }

  accept(event = {}) {
    const sequence = Math.max(0, Number(event.sequence) || 0);
    if (sequence && sequence <= this.lastSequence) {
      return { accepted: false, duplicateOrOld: true, refresh: false, gap: false };
    }
    const previousSequence = this.lastSequence;
    const gap = Boolean(sequence && previousSequence && sequence > previousSequence + 1);
    if (sequence && !gap) this.lastSequence = sequence;
    const kind = String(event.kind || "");
    const targetLens = LENS_KINDS[kind] || "";
    return {
      accepted: true,
      duplicateOrOld: false,
      refresh: this.lensMatches(targetLens),
      refreshStory: kind === "story.committed",
      refreshSummary: SUMMARY_EVENT_KINDS.has(kind),
      gap,
      needsCatchUp: gap,
      targetLens,
      previousSequence,
      sequence,
    };
  }
}

function decisionDescriptor(item, model) {
  return rows(model?.actions).find((action) => {
    const target = String(action?.object_key || action?.target_key || "");
    return Boolean(action?.action || action?.intent)
      && action.transportReady === true
      && Number.isInteger(action.expected_revision)
      && Boolean(target)
      && target === String(item.key || "");
  }) || null;
}

function decisionCard(item, index, model, handlers) {
  const descriptor = decisionDescriptor(item, model);
  const limitations = [
    item.limitation,
    item.collective ? copy("pages.live_session.decisionCard.message.66b7063ae5") : "",
    item.requires_check ? copy("pages.live_session.decisionCard.message.710b087fb3") : "",
  ].filter(Boolean);
  return renderBusinessCard({
    kind: "decision",
    opaqueKey: item.key || String(index),
    title: copy("pages.live_session.decisionCard.title.0ff30f0c47", {
      p0: String.fromCharCode(65 + index),
      p1: label(item, copy("pages.live_session.decisionCard.message.9e9957a56d")),
    }),
    summary: item.description || summary(item, copy("pages.live_session.decisionCard.message.85b6ac1495")),
    state: item.risk ? { state: "warning", label: item.risk } : null,
    meta: limitations.map((text) => ({ label: copy("pages.live_session.decisionCard.label.688f892378"), value: text })),
    actions: descriptor ? [renderButton({
      variant: index === 0 ? "primary" : "secondary",
      label: descriptor.label || copy("pages.live_session.decisionCard.message.e167292e68"),
      intent: { id: descriptor.action || descriptor.intent },
      onActivate: (_intent, event) => handlers.actions?.execute({
        ...descriptor,
        object_key: item.key,
        target_key: item.key,
      }, { opener: event.currentTarget }),
    })] : [],
  });
}

function decisionSections(decision, model, handlers) {
  const options = rows(decision?.options || decision?.choices);
  const primary = el("section", {
    class: "tavern-surface tavern-decision-workspace tavern-decision-primary",
    "data-testid": "tavern-live-decisions",
  }, [
    el("header", { class: "tavern-section-heading" }, [
      el("div", {}, [
        el("h2", { text: decision?.question || copy("pages.live_session.decisionSections.message.98b6a61f9c") }),
        el("p", { text: copy("pages.live_session.decisionSections.text.9a3d4dde0a") }),
      ]),
    ]),
  ]);
  const first = options.slice(0, 2);
  if (first.length) {
    primary.append(el("div", { class: "tavern-decision-grid" },
      first.map((item, index) => decisionCard(item, index, model, handlers))));
    if (!options.some((item) => decisionDescriptor(item, model))) {
      primary.append(el("div", { class: "tavern-source-note tavern-decision-boundary", role: "status" }, [
        el("strong", { text: copy("pages.live_session.decisionSections.message.393a70043d") }),
      ]));
    }
  } else {
    primary.append(renderStatePanel({
      phase: "empty",
      emptyCopy: decision?.message || copy("pages.live_session.decisionSections.message.393a70043d"),
    }));
  }
  const remaining = el("section", { class: "tavern-decision-workspace tavern-decision-remaining" });
  if (options.length > 2) {
    remaining.append(el("div", { class: "tavern-decision-grid" },
      options.slice(2).map((item, offset) => decisionCard(item, offset + 2, model, handlers))));
  }
  return { primary, remaining };
}

function partyInventory(items, profile) {
  if (!profile?.party?.inventory) return "";
  const totals = new Map();
  for (const actor of items) {
    const inventory = Array.isArray(actor?.inventory)
      ? actor.inventory : rows(actor?.inventory?.items);
    for (const item of inventory) {
      const itemLabel = String(item?.label || item?.name || "").trim();
      if (!itemLabel) continue;
      const quantity = Number(item?.quantity);
      totals.set(itemLabel, (totals.get(itemLabel) || 0) + (Number.isFinite(quantity) ? quantity : 1));
    }
  }
  return [...totals.entries()].slice(0, 3).map(([itemLabel, quantity]) => `${itemLabel} ×${quantity}`).join(" · ");
}

function partyBoundaryCard(kicker, heading, description) {
  return el("article", {}, [
    el("small", { text: kicker }),
    el("strong", { text: heading }),
    el("span", { text: description }),
  ]);
}

function renderPartyBoundaries(data, items, blocked, profile) {
  const inventory = partyInventory(items, profile);
  const inventoryDeclared = Boolean(profile?.party?.inventory);
  const fate = actorFateViewModel(data);
  const fateCount = fate?.items?.length || 0;
  return el("div", { class: "tavern-party-boundary-grid" }, [
    partyBoundaryCard(
      "小队可见物品",
      inventory || (inventoryDeclared ? "当前没有公开物品" : "当前世界未公开汇总物品"),
      inventoryDeclared ? "仅汇总当前身份可见的角色携行物品。" : "世界未声明该公开视图，页面不会猜测资源。",
    ),
    partyBoundaryCard(
      "开演阻塞",
      blocked ? `${blocked} 名角色需要处理` : "当前没有阻塞",
      blocked ? "请打开对应角色查看行动状态。" : "角色行动资格当前可用。",
    ),
    partyBoundaryCard(
      "命运窗口",
      fateCount ? `${fateCount} 个窗口待处理` : "当前没有可结算窗口",
      fateCount ? "下方会展示触发原因、期限与可执行操作。" : "出现时会展示触发条件和处理期限。",
    ),
  ]);
}

function renderPartyLens(envelope, context) {
  const source = envelope?.data || {}; const data = { ...source, actor_fate_consent: context.actorFateConsent || source.actor_fate_consent, available_actions: context.actions || source.available_actions };
  const profile = data.ui_profile || context.uiProfile || {};
  const section = el("section", { class: "tavern-team-strip" });
  const items = rows(data.items);
  const blocked = items.filter((item) => ["blocked", "paused", "recovering"].includes(item?.action_state)).length;
  section.append(el("header", { class: "tavern-party-heading" }, [
    el("div", {}, [
      el("h3", { text: "真人与 AI 队友" }),
      el("p", { text: "角色类型、行动状态、资源、背包和公开状态使用同一张业务卡。" }),
    ]),
    el("span", { class: "tavern-party-count", text: `${items.length} 人 · ${blocked} 阻塞` }),
  ]));
  const grid = el("div", { class: "tavern-actor-grid" });
  if (!items.length) {
    grid.append(renderStatePanel({ phase: "empty", emptyCopy: copy("pages.live_session.renderPartyLens.emptyCopy.1911e5de82") }));
  }
  for (const item of items) {
    grid.append(renderActorCard(item, {
      uiProfile: profile,
      kind: "teammate",
      onOpen: (actor, opener) => openActorDetail({
        actor,
        uiProfile: profile,
        handlers: context.handlers,
        opener,
      }),
    }));
  }
  section.append(grid);
  if (items.length) section.append(renderPartyBoundaries(data, items, blocked, profile));
  const fate = renderActorFateConsent(data, context.handlers); if (fate) section.append(fate);
  return section;
}

function renderReplayLens(envelope) {
  const data = envelope?.data || {};
  const timeline = rows(data.timeline?.items);
  const section = hasStructuredNarrative(timeline)
    ? renderNarrativeReplay(timeline)
    : renderTimeline(timeline, { title: copy("pages.live_session.renderReplayLens.title.66e55d7614") });
  section.classList.add("tavern-replay-lens");
  return section;
}

function renderGenerationLens(envelope, context) {
  const items = rows(envelope?.data?.items);
  const section = renderGeneration(items, {
    onAction: (descriptor, opener) => openSessionConfigurationAction(descriptor, context.handlers, opener),
  });
  section.classList.add("tavern-generation-lens");
  return section;
}

function renderLensEnvelope(lens, envelope, context = {}) {
  const normalized = envelope?.data ? envelope : { data: envelope || {} };
  if (lens === "party") return renderPartyLens(normalized, context);
  if (lens === "replay") return renderReplayLens(normalized);
  if (lens === "generation") return renderGenerationLens(normalized, context);
  const scenePath = normalized?.data?.surfaces?.scene_path?.data || {};
  return renderWorldLens(normalized, {
    lens,
    uiProfile: context.uiProfile,
    visualizers: WORLD_VISUALIZERS,
    scenePathNodes: rows(scenePath.nodes),
    handlers: context.handlers,
  });
}

function connectionLabels(state, detail) {
  const labels = {
    connecting: copy("pages.live_session.renderLiveSession.message.2db1d44988"),
    healthy: copy("pages.live_session.renderLiveSession.message.3b564e0e33"),
    reconnecting: copy("pages.live_session.renderLiveSession.message.360f969c99"),
    degraded: detail?.nextRetryMs
      ? copy("pages.live_session.renderLiveSession.message.b18322598b", { p0: Math.ceil(detail.nextRetryMs / 1000) })
      : copy("pages.live_session.renderLiveSession.message.c25eacdf72"),
  };
  return labels[state] || copy("pages.live_session.renderLiveSession.message.41d0ea1212");
}

function densityStat(term, description, detail = "") {
  return el("div", { class: "tavern-density-stat" }, [
    el("small", { text: term }),
    el("strong", { text: description || copy("pages.live_session.summary.not_reported") }),
    detail ? el("span", { text: detail }) : null,
  ]);
}

function liveSummary({ sessionData, turn, story, narrativeMode, reminder, pressure, uiProfile }) {
  const actors = rows(turn.order);
  const nearestTimer = rows(pressure.items).find((item) => item?.remaining_label);
  const density = uiProfile?.density === "rich"
    ? copy("pages.designer.rc8.f54bab7a88")
    : uiProfile?.density === "standard"
      ? copy("pages.designer.rc8.6bea77acef")
      : uiProfile?.density === "minimal"
        ? copy("pages.designer.rc8.75fc0214e4")
        : copy("pages.live_session.summary.not_reported");
  const reminderDetail = reminder?.enabled
    ? copy("pages.live_session.summary.next_generation")
    : copy("pages.live_session.rc8.6744b4c6a9");
  return el("section", {
    class: "tavern-density-strip tavern-live-summary",
    "aria-label": copy("pages.live_session.summary.label"),
    "data-testid": "tavern-live-summary",
  }, [
    densityStat(copy("pages.live_session.summary.session_state"), sessionData.state_label || sessionData.state, copy("pages.live_session.summary.current_snapshot")),
    densityStat(copy("pages.live_session.summary.world"), sessionData.world_label, copy("pages.live_session.summary.density", { p0: density })),
    densityStat(copy("pages.live_session.summary.round"), turn.round !== undefined ? copy("pages.live_session.renderLiveSession.text.9db1804608", { p0: turn.round }) : "", story.turn !== undefined ? copy("pages.live_session.summary.story_turn", { p0: story.turn }) : ""),
    densityStat(copy("pages.live_session.summary.current_actor"), turn.current_name ? `「${turn.current_name}」` : "", Number.isFinite(Number(turn.remaining_seconds)) ? copy("pages.live_session.actorPanel.message.3a4c61693e", { p0: turn.remaining_seconds }) : ""),
    densityStat(copy("pages.live_session.summary.present_characters"), actors.length ? copy("pages.live_session.summary.people", { p0: actors.length }) : "", copy("pages.live_session.summary.visible_order")),
    densityStat(copy("pages.live_session.summary.host_mode"), sessionData.host_mode_label || sessionData.host_mode, copy("pages.live_session.summary.host_mode_unreported")),
    densityStat(copy("pages.live_session.controlPanel.text.16e8f15cb9"), narrativeMode.label, reminderDetail),
    densityStat(copy("pages.live_session.summary.input_lock"), sessionData.input_locked ? copy("pages.live_session.controlPanel.message.0c65416f4a") : copy("pages.live_session.controlPanel.message.35f47f9f98"), copy("pages.live_session.summary.scope_current")),
    densityStat(copy("pages.live_session.summary.active_timers"), Number.isFinite(Number(pressure.active_timers)) ? String(pressure.active_timers) : "", nearestTimer?.remaining_label || copy("pages.live_session.summary.no_timer_detail")),
  ]);
}

function runtimeDisclosure(uiProfile, availableSpecs) {
  const page = rows(uiProfile?.pages).find((item) => item?.key === "live_session") || {};
  const sections = rows(page.sections).map((item) => item?.kind).filter(Boolean);
  const labels = availableSpecs.map((item) => item.label).filter(Boolean);
  const declared = sections.length || labels.length;
  return el("details", { class: "tavern-live-runtime-disclosure" }, [
    el("summary", {}, [
      el("span", { text: copy("pages.live_session.runtime.title") }),
      el("span", { class: "tavern-status-badge", text: declared ? copy("pages.live_session.runtime.count", { p0: declared }) : copy("pages.live_session.summary.not_reported") }),
    ]),
    el("div", { class: "tavern-live-runtime-body" }, [
      labels.length ? el("div", { class: "tavern-live-runtime-chips" }, labels.map((item) => el("span", { text: item }))) : null,
      el("p", { text: declared ? copy("pages.live_session.runtime.summary") : copy("pages.live_session.runtime.empty") }),
    ]),
  ]);
}

function recentEventPanel(story, turn, latestSequence) {
  const entries = [];
  if (story && (story.title || story.summary)) {
    entries.push(el("article", { class: "tavern-live-event-row" }, [
      el("span", { class: "tavern-event-marker", "aria-hidden": "true" }),
      el("div", {}, [
        el("strong", { text: story.title || copy("pages.live_session.storyStage.message.4771a6ddfd") }),
        el("small", { text: story.source_label || copy("pages.live_session.recent.story_updated") }),
      ]),
      story.updated_at ? el("time", { datetime: story.updated_at, text: formatUtc8Minute(story.updated_at) }) : null,
    ]));
  }
  if (turn?.current_name) {
    entries.push(el("article", { class: "tavern-live-event-row" }, [
      el("span", { class: "tavern-event-marker", "aria-hidden": "true" }),
      el("div", {}, [
        el("strong", { text: copy("pages.live_session.recent.current_actor", { p0: turn.current_name }) }),
        el("small", { text: turn.round !== undefined
          ? copy("pages.live_session.renderLiveSession.text.9db1804608", { p0: turn.round })
          : copy("pages.live_session.summary.current_snapshot") }),
      ]),
      latestSequence ? el("span", { text: copy("pages.live_session.connection.sequence", { p0: latestSequence }) }) : null,
    ]));
  }
  const section = el("section", {
    class: "tavern-surface tavern-live-recent",
    "data-testid": "tavern-live-recent",
  }, [
    el("header", { class: "tavern-section-heading" }, [el("div", {}, [
      el("h2", { text: copy("pages.live_session.recent.title") }),
      el("p", { text: copy("pages.live_session.recent.summary") }),
    ])]),
  ]);
  section.append(entries.length
    ? el("div", { class: "tavern-live-event-list" }, entries)
    : renderStatePanel({ phase: "empty", emptyCopy: copy("pages.live_session.recent.empty") }));
  return section;
}

function visibleTimeLabel(input) {
  const text = String(input || "").trim();
  if (!text) return copy("pages.live_session.summary.not_reported");
  const timestamp = new Date(text).valueOf();
  return Number.isFinite(timestamp) ? formatUtc8Minute(text) : text;
}

function liveLowerGrid({ sessionData, turn, story, decision, pressure, uiProfile, selectLens }) {
  const party = rows(turn?.order);
  const declared = rows(uiProfile?.live_lenses).map((item) => item?.label).filter(Boolean);
  const publicPressure = rows(pressure?.items);
  const partyCard = el("section", { class: "tavern-surface tavern-live-summary-card" }, [
    el("header", { class: "tavern-section-heading" }, [el("div", {}, [
      el("h2", { text: copy("pages.live_session.lower.party.title") }),
      el("p", { text: copy("pages.live_session.lower.party.summary") }),
    ]), renderStatusBadge({ state: "ready", label: copy("pages.live_session.summary.people", { p0: party.length }) })]),
    party.length
      ? el("div", { class: "tavern-live-info-list" }, party.map((item) => el("div", {}, [
        el("strong", { text: `「${String(item.label || "").replace(/^「|」$/g, "")}」` }),
        el("span", { text: item.current
          ? copy("pages.live_session.actorPanel.label.ef26096871")
          : item.state || copy("pages.live_session.renderPartyLens.message.814b6a6c04") }),
      ])))
      : renderStatePanel({ phase: "empty", emptyCopy: copy("pages.live_session.renderPartyLens.emptyCopy.1911e5de82") }),
  ]);
  const worldFacts = [
    [copy("pages.live_session.lower.world.world"), sessionData?.world_label],
    [copy("pages.live_session.lower.world.story"), story?.title],
    [copy("pages.live_session.lower.world.objective"), decision?.question || rows(decision?.options)[0]?.label],
    [copy("pages.live_session.lower.world.time"), visibleTimeLabel(sessionData?.time_label)],
  ];
  const worldCard = el("section", { class: "tavern-surface tavern-live-summary-card" }, [
    el("header", { class: "tavern-section-heading" }, [el("div", {}, [
      el("h2", { text: copy("pages.live_session.lower.world.title") }),
      el("p", { text: copy("pages.live_session.lower.world.summary") }),
    ])]),
    el("dl", { class: "tavern-live-definition-grid" }, worldFacts.map(([term, detail]) => el("div", {}, [
      el("dt", { text: term }),
      el("dd", { text: detail || copy("pages.live_session.summary.not_reported") }),
    ]))),
  ]);
  const worldModuleCard = el("section", { class: "tavern-surface tavern-live-summary-card" }, [
    el("header", { class: "tavern-section-heading" }, [el("div", {}, [
      el("h2", { text: copy("pages.live_session.lower.modules.title") }),
      el("p", { text: copy("pages.live_session.lower.modules.summary") }),
    ])]),
    publicPressure.length
      ? el("div", { class: "tavern-live-info-list" }, publicPressure.map((item) => el("div", {}, [
        el("strong", { text: label(item, copy("pages.live_session.pressurePanel.message.d28c889060")) }),
        el("span", { text: item.remaining_label || item.state || copy("pages.live_session.pressurePanel.message.cdea037991") }),
      ])))
      : el("p", { class: "tavern-live-card-note", text: declared.length
        ? copy("pages.live_session.lower.modules.declared", { p0: declared.join("、") })
        : copy("pages.live_session.lower.modules.empty") }),
  ]);
  const resourceCard = el("section", { class: "tavern-surface tavern-live-summary-card" }, [
    el("header", { class: "tavern-section-heading" }, [el("div", {}, [
      el("h2", { text: copy("pages.live_session.lower.resources.title") }),
      el("p", { text: copy("pages.live_session.lower.resources.summary") }),
    ])]),
    el("p", { class: "tavern-live-card-note", text: copy("pages.live_session.lower.resources.boundary") }),
    renderButton({
      variant: "secondary",
      label: copy("pages.live_session.lower.resources.open"),
      onActivate: () => selectLens("party"),
    }),
  ]);
  return el("div", { class: "tavern-live-lower-grid", "data-testid": "tavern-live-lower-grid" }, [
    partyCard,
    worldCard,
    worldModuleCard,
    resourceCard,
  ]);
}

function pacingDisclosure({ story, pressure, liveRuntime, model, handlers, selectLens }) {
  const pacingDescriptor = descriptorFor(model, "session.pacing.preview");
  const actions = [];
  if (pacingDescriptor) {
    actions.push(renderButton({
      variant: "secondary",
      label: pacingDescriptor.label,
      onActivate: (_intent, event) => openSessionConfigurationAction(pacingDescriptor, handlers, event.currentTarget),
    }));
  }
  actions.push(renderButton({
    variant: "secondary",
    label: copy("pages.live_session.pacing.replay"),
    onActivate: () => selectLens("replay"),
  }));
  return el("details", { class: "tavern-live-pacing-disclosure" }, [
    el("summary", {}, [
      el("span", { text: copy("pages.live_session.pacing.title") }),
      el("span", { text: copy("pages.live_session.pacing.blocks") }),
    ]),
    el("div", { class: "tavern-live-pacing-body" }, [
      el("div", { class: "tavern-density-strip tavern-live-pacing-stats" }, [
        densityStat(copy("pages.live_session.pacing.story_turn"), story?.turn ? String(story.turn) : "", copy("pages.live_session.summary.current_snapshot")),
        densityStat(copy("pages.live_session.pacing.timers"), String(Number(pressure?.active_timers) || 0), rows(pressure?.items)[0]?.remaining_label || copy("pages.live_session.summary.no_timer_detail")),
        densityStat(copy("pages.live_session.pacing.events"), String(Number(liveRuntime?.latest_sequence) || 0), copy("pages.live_session.connection.preserve")),
        densityStat(copy("pages.live_session.pacing.snapshot"), story?.updated_at ? formatUtc8Minute(story.updated_at) : "", copy("pages.live_session.pacing.snapshot_boundary")),
      ]),
      el("div", { class: "tavern-live-pacing-actions" }, actions),
    ]),
  ]);
}

function retentionNote() {
  return el("div", { class: "tavern-source-note tavern-live-retention" }, [
    el("strong", { text: copy("pages.live_session.retention.label") }),
    el("span", { text: copy("pages.live_session.retention.summary") }),
  ]);
}

export function mergeLiveSummaryModel(model, envelope = {}) {
  const data = envelope?.data && typeof envelope.data === "object" ? envelope.data : {};
  const sectionValues = {
    story: data.story,
    decision: data.decision,
    turn: data.turn,
    pressure: data.pressure,
  };
  const sections = rows(model?.sections).map((section) => {
    if (Object.hasOwn(sectionValues, section.id) && Object.hasOwn(data, section.id)) {
      return { ...section, value: sectionValues[section.id] };
    }
    if (section.id !== "lens") return section;
    return {
      ...section,
      value: {
        ...(section.value || {}),
        runtime: {
          ...(section.value?.runtime || {}),
          session: data.session || {},
          narrative_mode: data.narrative_mode || {},
          generation_reminder: data.generation_reminder || {},
          ui_profile: data.ui_profile || {}, actor_fate_consent: data.actor_fate_consent || {},
          latest_sequence: data.latest_sequence,
          pressure: data.pressure || {},
          turn: data.turn || {},
        },
      },
    };
  });
  return {
    ...model,
    phase: envelope.state || model?.phase,
    readonly: Boolean(envelope.readonly),
    permissions: envelope.permissions || model?.permissions || {},
    sections,
    actions: Object.hasOwn(data, "available_actions") ? rows(data.available_actions) : [],
    problems: rows(envelope.problems),
  };
}

export function renderLiveSession(model, handlers = {}) {
  let currentModel = model;
  const root = pageRoot(model, "tavern-live-session");
  root.append(stateNotice(model, copy("pages.live_session.renderLiveSession.message.5343606193")));
  let story = value(model, "story") || {};
  let decision = value(model, "decision") || {};
  let turn = value(model, "turn") || {};
  let pressure = value(model, "pressure") || {};
  const initial = value(model, "lens") || {};
  let liveRuntime = initial.runtime || {};
  let sessionData = liveRuntime.session || {};
  let narrativeMode = liveRuntime.narrative_mode || {};
  let reminder = liveRuntime.generation_reminder || {};
  let uiProfile = liveRuntime.ui_profile || {};
  const principal = handlers.store?.app?.principal || {};
  const principalRoles = new Set(principal.roles || []);
  if (principal.is_admin === true) principalRoles.add("admin");
  const availableSpecs = lensSpecs(uiProfile, principalRoles);
  const specsByKey = new Map(availableSpecs.map((spec) => [spec.key, spec]));
  let activeLens = specsByKey.has(initial.active) ? initial.active : "party";
  let selectLens = () => {};
  const renderToolbarCopy = () => {
    const liveTitle = sessionData.label || currentModel.title || copy("pages.live_session.renderLiveSession.message.fee03c832e");
    return el("div", { class: "tavern-page-toolbar-copy" }, [
      el("p", { class: "tavern-page-eyebrow", text: sessionData.world_label || copy("pages.live_session.renderLiveSession.message.5d8bdcd315") }),
      el("h2", { text: copy("pages.live_session.toolbar.title") }),
      el("p", { class: "tavern-live-toolbar-summary", text: copy("pages.live_session.toolbar.summary") }),
      el("div", { class: "tavern-live-chips" }, [
        el("span", { text: liveTitle }),
        sessionData.state ? el("span", { text: sessionData.state_label || sessionData.state }) : null,
        turn.round !== undefined ? el("span", { text: copy("pages.live_session.renderLiveSession.text.9db1804608", { p0: turn.round }) }) : null,
        narrativeMode.label ? el("span", { text: narrativeMode.label }) : null,
      ]),
    ]);
  };
  const connection = el("span", {
    class: "tavern-live-connection",
    "data-state": "connecting",
    role: "status",
    text: copy("pages.live_session.renderLiveSession.text.2db1d44988"),
  });
  const refreshButton = renderButton({
    variant: "secondary",
    label: copy("pages.live_session.renderLiveSession.label.7d707d62a7"),
    onActivate: () => handlers.refresh?.(),
  });
  const detailButton = renderButton({
    variant: "secondary",
    label: copy("pages.live_session.renderLiveSession.label.9f97f2b63a"),
    onActivate: (_intent, event) => openSessionDetail({ model: currentModel, handlers, opener: event.currentTarget }),
  });
  const mobileSummaryButton = renderButton({
    variant: "quiet",
    label: copy("pages.live_session.renderLiveSession.label.74f3b1203a"),
    onActivate: (_intent, event) => openLiveSummarySheet({ model: currentModel, handlers, opener: event.currentTarget }),
  });
  mobileSummaryButton.classList.add("tavern-mobile-only");
  let decisionTarget = null;
  const mobileDecisionButton = renderButton({
    variant: "primary",
    label: copy("pages.live_session.decisionSections.message.98b6a61f9c"),
    onActivate: () => decisionTarget?.scrollIntoView?.({ block: "start", behavior: "smooth" }),
  });
  mobileDecisionButton.classList.add("tavern-mobile-only", "tavern-mobile-decision-jump");
  let toolbarCopy = renderToolbarCopy();
  root.append(el("header", {
    class: "tavern-page-toolbar tavern-live-toolbar",
    "data-testid": "tavern-live-toolbar",
  }, [
    toolbarCopy,
    el("div", { class: "tavern-live-toolbar-actions" }, [mobileDecisionButton, mobileSummaryButton, refreshButton, detailButton]),
  ]));
  let summaryNode = liveSummary({ sessionData, turn, story, narrativeMode, reminder, pressure, uiProfile });
  root.append(summaryNode);
  root.append(el("div", { class: "tavern-live-connection-bar", role: "status" }, [
    el("div", { class: "tavern-live-connection-state" }, [
      el("span", { class: "tavern-live-pulse", "aria-hidden": "true" }),
      connection,
      el("small", { class: "tavern-live-sequence", text: copy("pages.live_session.connection.sequence", { p0: liveRuntime.latest_sequence || 0 }) }),
    ]),
    el("div", { class: "tavern-live-connection-meta" }, [
      el("span", { text: copy("pages.live_session.connection.preserve") }),
      el("span", { text: copy("pages.live_session.rc8.bc2539b148") }),
      el("span", { text: copy("pages.live_session.connection.privacy") }),
    ]),
  ]));
  root.append(runtimeDisclosure(uiProfile, availableSpecs));

  const storySlot = el("div", { class: "tavern-story-slot", "data-component": "tavern-story-stage" });
  const storyMedia = globalThis.matchMedia?.("(max-width: 760px)");
  const replaceStory = () => {
    const expanded = storySlot.querySelector('.tavern-story-expand[aria-expanded="true"]') !== null;
    storySlot.replaceChildren(renderNarrativeDocument(story, {
      session: sessionData,
      narrativeMode,
      initialBlocks: storyMedia?.matches ? 1 : 6,
      onReplay: () => selectLens("replay"),
    }));
    if (expanded) storySlot.querySelector('.tavern-story-expand[aria-expanded="false"]')?.click();
  };
  replaceStory();
  const onStoryBreakpoint = () => replaceStory();
  storyMedia?.addEventListener?.("change", onStoryBreakpoint);
  let decisions = decisionSections(decision, currentModel, handlers);
  decisionTarget = decisions.primary;
  let actor = actorPanel(turn);
  let narrativeModes = mobileSecondaryDisclosure(
    narrativeModePanel(narrativeMode, currentModel, handlers),
    copy("pages.live_session.controlPanel.text.16e8f15cb9"),
    "tavern-live-narrative-disclosure",
  );
  let controls = mobileSecondaryDisclosure(
    controlPanel(sessionData, reminder, currentModel, handlers),
    copy("pages.live_session.controlPanel.text.fee03c832e"),
    "tavern-live-control-disclosure",
  );
  const narrativeStyle = narrativeStylePanel(handlers, handlers.navigation?.objectKey || "");
  let pressureDisclosure = mobileSecondaryDisclosure(
    pressurePanel(pressure, uiProfile),
    copy("pages.live_session.pressurePanel.text.9ef22ba928"),
    "tavern-live-pressure-disclosure",
  );
  let recent = mobileSecondaryDisclosure(
    recentEventPanel(story, turn, liveRuntime.latest_sequence),
    copy("pages.live_session.recent.title"),
    "tavern-live-recent-disclosure",
  );
  const sideItems = () => [
    pressureDisclosure,
    recent,
    narrativeModes,
    narrativeStyle,
    controls,
  ].filter(Boolean);
  const main = el("main", { class: "tavern-live-main" }, [storySlot, actor, decisions.primary, decisions.remaining]);
  const side = el("aside", { class: "tavern-live-side" }, sideItems());
  root.append(el("div", { class: "tavern-live-grid tavern-live-layout" }, [
    main,
    side,
  ]));

  const tabs = el("nav", {
    class: "tavern-live-lens-tabs",
    "aria-label": copy("pages.live_session.renderLiveSession.message.0e3228bf40"),
    role: "tablist",
  });
  const panel = el("section", {
    id: "tavern-live-lens-panel",
    class: "tavern-lens-panel",
    "data-lens": activeLens,
    "data-phase": initial.phase || "loading",
    "data-testid": `tavern-lens-${activeLens}`,
    role: "tabpanel",
    "aria-labelledby": `tavern-lens-tab-${activeLens}`,
  });
  const lensWorkspace = el("section", { class: "tavern-live-lens-workspace" }, [
    el("header", { class: "tavern-lens-workspace-head" }, [
      el("div", {}, [
        el("p", { class: "tavern-page-eyebrow", text: "LIVE LENSES" }),
        el("h2", { text: copy("pages.live_session.renderLiveSession.message.0e3228bf40") }),
        el("p", { text: copy("pages.live_session.rc8.1a680b9abf") }),
      ]),
    ]),
    tabs,
    panel,
  ]);
  const lensDisclosure = mobileSecondaryDisclosure(
    lensWorkspace,
    copy("pages.live_session.renderLiveSession.message.0e3228bf40"),
    "tavern-live-lens-disclosure",
  );
  root.append(lensDisclosure);
  let lowerDisclosure = mobileSecondaryDisclosure(
    liveLowerGrid({ sessionData, turn, story, decision, pressure, uiProfile, selectLens: (lens) => selectLens(lens) }),
    copy("pages.live_session.lower.mobile_summary"),
    "tavern-live-lower-disclosure",
  );
  root.append(lowerDisclosure);
  let pacingNode = pacingDisclosure({ story, pressure, liveRuntime, model: currentModel, handlers, selectLens: (lens) => selectLens(lens) });
  root.append(pacingNode);
  root.append(retentionNote());

  const replaceSummaryDependencies = () => {
    const focusedKey = root.contains?.(document.activeElement)
      ? document.activeElement?.dataset?.liveFocus || ""
      : "";
    const nextToolbarCopy = renderToolbarCopy();
    toolbarCopy.replaceWith(nextToolbarCopy);
    toolbarCopy = nextToolbarCopy;
    const nextSummary = liveSummary({ sessionData, turn, story, narrativeMode, reminder, pressure, uiProfile });
    summaryNode.replaceWith(nextSummary);
    summaryNode = nextSummary;
    replaceStory();

    const nextActor = actorPanel(turn);
    actor.replaceWith(nextActor);
    actor = nextActor;
    const nextDecisions = decisionSections(decision, currentModel, handlers);
    decisions.primary.replaceWith(nextDecisions.primary);
    decisions.remaining.replaceWith(nextDecisions.remaining);
    decisions = nextDecisions;
    decisionTarget = decisions.primary;

    for (const disclosure of [narrativeModes, controls, pressureDisclosure, recent]) disclosure?.dispose?.();
    narrativeModes = mobileSecondaryDisclosure(
      narrativeModePanel(narrativeMode, currentModel, handlers),
      copy("pages.live_session.controlPanel.text.16e8f15cb9"),
      "tavern-live-narrative-disclosure",
    );
    controls = mobileSecondaryDisclosure(
      controlPanel(sessionData, reminder, currentModel, handlers),
      copy("pages.live_session.controlPanel.text.fee03c832e"),
      "tavern-live-control-disclosure",
    );
    pressureDisclosure = mobileSecondaryDisclosure(
      pressurePanel(pressure, uiProfile),
      copy("pages.live_session.pressurePanel.text.9ef22ba928"),
      "tavern-live-pressure-disclosure",
    );
    recent = mobileSecondaryDisclosure(
      recentEventPanel(story, turn, liveRuntime.latest_sequence),
      copy("pages.live_session.recent.title"),
      "tavern-live-recent-disclosure",
    );
    side.replaceChildren(...sideItems());

    const nextLower = mobileSecondaryDisclosure(
      liveLowerGrid({ sessionData, turn, story, decision, pressure, uiProfile, selectLens: (lens) => selectLens(lens) }),
      copy("pages.live_session.lower.mobile_summary"),
      "tavern-live-lower-disclosure",
    );
    lowerDisclosure?.dispose?.();
    lowerDisclosure.replaceWith(nextLower);
    lowerDisclosure = nextLower;
    const nextPacing = pacingDisclosure({ story, pressure, liveRuntime, model: currentModel, handlers, selectLens: (lens) => selectLens(lens) });
    pacingNode.replaceWith(nextPacing);
    pacingNode = nextPacing;
    const sequenceNode = root.querySelector(".tavern-live-sequence");
    if (sequenceNode) sequenceNode.textContent = copy("pages.live_session.connection.sequence", { p0: liveRuntime.latest_sequence || 0 });

    if (focusedKey) {
      const target = [...root.querySelectorAll("[data-live-focus]")]
        .find((item) => item.dataset.liveFocus === focusedKey);
      target?.focus?.({ preventScroll: true });
    }
  };

  const worldLenses = availableSpecs.filter((spec) => !["party", "replay", "generation"].includes(spec.key)).map((spec) => spec.key);
  const coordinator = new LiveEventCoordinator({
    activeLens,
    after: liveRuntime.latest_sequence || 0,
    availableLenses: availableSpecs.map((spec) => spec.key),
    worldLenses,
  });
  let activeController = null;
  let storyController = null;
  let disposed = false;
  let catchUpPromise = null;

  const replaceLens = (...children) => {
    panel.firstElementChild?.dispose?.();
    panel.replaceChildren(...children);
  };

  const loadLens = async (lens, { preserve = false } = {}) => {
    const spec = specsByKey.get(lens);
    if (disposed || !spec || !handlers.client) return false;
    activeLens = lens;
    coordinator.setActiveLens(lens);
    activeController?.abort();
    panel.setAttribute("aria-labelledby", `tavern-lens-tab-${lens}`);
    panel.dataset.lens = lens;
    panel.dataset.testid = `tavern-lens-${lens}`;
    panel.dataset.phase = preserve && panel.childNodes.length ? "refreshing" : "loading";
    if (!preserve || !panel.childNodes.length) {
      replaceLens(renderStatePanel({
        phase: preserve ? "refreshing" : "loading",
        operation: copy("pages.live_session.renderLiveSession.operation.704d132455", { p0: spec.label }),
      }));
    }
    for (const button of tabs.querySelectorAll('[role="tab"]')) {
      const selected = button.dataset.lens === lens;
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
    }
    const requestScope = { objectKey: handlers.navigation?.objectKey || "", filters: { lens } };
    const request = handlers.store?.begin(`session-lens:${lens}`, requestScope);
    const controller = request?.controller || new AbortController();
    activeController = controller;
    const requestId = request?.requestId || "";
    try {
      const envelope = await handlers.client.get(spec.endpoint, {
        query: {
          session_key: handlers.navigation?.objectKey || "",
          ...(spec.surfaceKey ? { surface_key: spec.surfaceKey, placement: "live_session" } : {}),
        },
        signal: controller.signal,
        operation: copy("pages.live_session.renderLiveSession.operation.704d132455", { p0: spec.label }),
      });
      if (disposed || controller.signal.aborted || lens !== activeLens) return false;
      const accepted = handlers.store?.resolve(`session-lens:${lens}`, envelope, requestId, requestScope);
      if (accepted === false) return false;
      panel.dataset.phase = envelope.state || "ready";
      panel.firstElementChild?.dispose?.();
      panel.replaceChildren(renderLensEnvelope(lens, envelope, { handlers, uiProfile, actorFateConsent: liveRuntime.actor_fate_consent, actions: currentModel.actions }));
      return true;
    } catch (error) {
      if (error?.name === "AbortError" || disposed || lens !== activeLens) return false;
      handlers.store?.reject(`session-lens:${lens}`, error, requestId, requestScope);
      const state = handlers.store?.surface(`session-lens:${lens}`, requestScope);
      panel.dataset.phase = state?.lastGood ? "stale" : error?.status === 403 ? "permission" : "error";
      const children = [renderStatePanel({
        phase: panel.dataset.phase,
        operation: copy("pages.live_session.renderLiveSession.operation.704d132455", { p0: spec.label }),
        problem: error,
        lastGood: state?.lastGood,
        retryAction: () => loadLens(lens, { preserve: Boolean(state?.lastGood) }),
      })];
      if (state?.lastGood) children.push(renderLensEnvelope(lens, state.lastGood, { handlers, uiProfile, actorFateConsent: liveRuntime.actor_fate_consent, actions: currentModel.actions }));
      replaceLens(...children);
      return false;
    }
  };

  selectLens = (lens) => {
    if (!specsByKey.has(lens)) return false;
    handlers.updateLocation?.({ lens });
    void loadLens(lens);
    tabs.querySelector(`[data-lens="${lens}"]`)?.focus();
    return true;
  };

  for (const spec of availableSpecs) {
    const button = el("button", {
      id: `tavern-lens-tab-${spec.key}`,
      class: "tavern-live-lens-tab",
      type: "button",
      role: "tab",
      "data-lens": spec.key,
      "aria-controls": "tavern-live-lens-panel",
      "aria-selected": String(spec.key === activeLens),
      tabindex: spec.key === activeLens ? "0" : "-1",
      text: spec.label,
    });
    button.addEventListener("click", () => selectLens(spec.key));
    tabs.append(button);
  }
  tabs.addEventListener("keydown", (event) => {
    const buttons = [...tabs.querySelectorAll('[role="tab"]')];
    const index = buttons.indexOf(document.activeElement);
    if (index < 0) return;
    let next = null;
    if (["ArrowRight", "ArrowDown"].includes(event.key)) next = (index + 1) % buttons.length;
    if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = (index - 1 + buttons.length) % buttons.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = buttons.length - 1;
    if (next !== null) {
      event.preventDefault();
      buttons[next].focus();
      buttons[next].click();
    }
  });

  const loadStory = async () => {
    if (disposed || !handlers.client) return false;
    storyController?.abort();
    storyController = new AbortController();
    const controller = storyController;
    const scrollY = globalThis.window?.scrollY || 0;
    try {
      const envelope = await handlers.client.get("dashboard/session-summary", {
        query: { session_key: handlers.navigation?.objectKey || "" },
        signal: controller.signal,
        operation: copy("pages.live_session.renderLiveSession.message.5343606193"),
      });
      if (disposed || controller.signal.aborted) return false;
      const data = envelope?.data || {};
      currentModel = mergeLiveSummaryModel(currentModel, envelope);
      if (Object.hasOwn(data, "story")) story = data.story || {};
      if (Object.hasOwn(data, "decision")) decision = data.decision || {};
      if (Object.hasOwn(data, "turn")) turn = data.turn || {};
      if (Object.hasOwn(data, "pressure")) pressure = data.pressure || {};
      if (Object.hasOwn(data, "session")) sessionData = data.session || {};
      if (Object.hasOwn(data, "narrative_mode")) narrativeMode = data.narrative_mode || {};
      if (Object.hasOwn(data, "generation_reminder")) reminder = data.generation_reminder || {};
      if (Object.hasOwn(data, "ui_profile")) uiProfile = data.ui_profile || {};
      liveRuntime = {
        ...liveRuntime,
        session: sessionData,
        narrative_mode: narrativeMode,
        generation_reminder: reminder,
         ui_profile: uiProfile, actor_fate_consent: data.actor_fate_consent ?? liveRuntime.actor_fate_consent,
        latest_sequence: data.latest_sequence ?? liveRuntime.latest_sequence,
        pressure,
        turn,
      };
      replaceSummaryDependencies();
      globalThis.requestAnimationFrame?.(() => globalThis.window?.scrollTo?.({ top: scrollY }));
      return true;
    } catch (error) {
      if (error?.name === "AbortError" || disposed) return false;
      storySlot.prepend(renderStatePanel({
        phase: "stale",
        operation: copy("pages.live_session.renderLiveSession.message.5343606193"),
        problem: error,
        lastGood: story,
        retryAction: loadStory,
      }));
      return false;
    }
  };

  const stream = handlers.client && handlers.navigation?.objectKey
    ? new RecoveringEventStream({
      client: handlers.client,
      sessionKey: handlers.navigation.objectKey,
      getAfter: () => coordinator.lastSequence,
      onEvent: (event) => {
        const result = coordinator.accept(event);
        if (!result.accepted) return null;
        if (result.needsCatchUp) {
          if (!catchUpPromise) {
            catchUpPromise = Promise.all([
              loadLens(activeLens, { preserve: true }),
              loadStory(),
            ]).then((outcomes) => {
              if (outcomes.some(Boolean)) coordinator.confirmSequence(result.sequence);
            }).finally(() => { catchUpPromise = null; });
          }
          return catchUpPromise;
        }
        if (result.refreshSummary) return handlers.refresh?.();
        const work = [];
        if (result.refreshStory) work.push(loadStory());
        if (result.refresh) work.push(loadLens(activeLens, { preserve: true }));
        return work.length > 1 ? Promise.all(work) : work[0] || null;
      },
      onError: () => {},
      onState: (state, detail) => {
        connection.dataset.state = state;
        connection.textContent = connectionLabels(state, detail);
      },
    })
    : null;

  queueMicrotask(() => {
    if (initial.envelope) {
      panel.dataset.phase = "ready";
       panel.replaceChildren(renderLensEnvelope(activeLens, initial.envelope, { handlers, uiProfile, actorFateConsent: liveRuntime.actor_fate_consent, actions: currentModel.actions }));
    }
    void loadLens(activeLens, { preserve: Boolean(initial.envelope) });
    stream?.start();
    if (handlers.navigation?.dialog === "detail") {
      openSessionDetail({ model: currentModel, handlers, opener: detailButton, activeTab: handlers.navigation.detailTab });
    }
  });

  root.refresh = () => handlers.refresh?.();
  root.dispose = () => {
    disposed = true;
    activeController?.abort();
    storyController?.abort();
    panel.firstElementChild?.dispose?.();
    stream?.stop();
    storyMedia?.removeEventListener?.("change", onStoryBreakpoint);
    narrativeModes?.dispose?.();
    controls?.dispose?.();
    pressureDisclosure?.dispose?.();
    recent?.dispose?.();
    lensDisclosure?.dispose?.();
    lowerDisclosure?.dispose?.();
  };
  return root;
}
