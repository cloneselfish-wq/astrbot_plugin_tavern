import { renderButton } from "../components/buttons.js";
import { renderBusinessCard } from "../components/cards.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { copy } from "../copy/catalog.js";
import { openSessionConfigurationAction } from "../dialogs/session-detail.js";
import { el, rows } from "./shared.js";

const FATE_INTENTS = new Set([
  "actor_fate.preview.accept",
  "actor_fate.preview.refuse",
]);

function text(value, fallback = "") {
  const normalized = String(value ?? "").trim();
  return normalized || fallback;
}

function acknowledgementReady(descriptor) {
  return rows(descriptor?.fields).some((field) => (
    field?.name === "acknowledge_fate"
    && field?.type === "checkbox"
    && field?.required === true
  ));
}

export function actorFateActionDescriptors(data = {}) {
  const consent = data?.actor_fate_consent || {};
  const candidates = [
    ...rows(data?.available_actions),
    ...rows(consent?.available_actions),
  ];
  const unique = new Map();
  for (const descriptor of candidates) {
    const intent = text(descriptor?.intent || descriptor?.action);
    const targetKey = text(descriptor?.target_key || descriptor?.object_key);
    const revision = Number(descriptor?.expected_revision);
    if (
      !FATE_INTENTS.has(intent)
      || descriptor?.transportReady !== true
      || descriptor?.target_kind !== "fate-preview"
      || !targetKey
      || !Number.isInteger(revision)
      || revision < 0
      || !acknowledgementReady(descriptor)
    ) continue;
    const key = `${intent}:${targetKey}`;
    if (!unique.has(key)) unique.set(key, descriptor);
  }
  return [...unique.values()];
}

export function actorFateViewModel(data = {}) {
  const consent = data?.actor_fate_consent;
  if (!consent || typeof consent !== "object" || Array.isArray(consent)) return null;
  const actions = actorFateActionDescriptors(data);
  const items = rows(consent.items).map((item) => ({
    key: text(item?.key),
    actor: text(item?.actor, copy("pages.live_session.fate.actor_fallback")),
    source: text(item?.source, copy("pages.live_session.fate.source_fallback")),
    reason: text(item?.reason, copy("pages.live_session.fate.reason_fallback")),
    alternatives: rows(item?.alternatives).map((value) => text(value)).filter(Boolean).slice(0, 8),
    rescueWindow: text(item?.rescue_window),
    expiresOn: text(item?.expires_on),
    status: text(item?.status, copy("pages.live_session.fate.pending")),
  })).filter((item) => item.key);
  return {
    state: text(consent.state, items.length ? "pending" : "empty"),
    message: text(consent.message),
    items,
    actions,
    actionTargets: new Set(actions.map((descriptor) => text(descriptor.target_key || descriptor.object_key))),
  };
}

function problemPanel(state, message) {
  const permission = state === "permission" || state === "forbidden";
  return renderStatePanel({
    phase: permission ? "permission" : "error",
    operation: copy("pages.live_session.fate.operation"),
    problem: {
      message: message || (permission
        ? copy("pages.live_session.fate.permission")
        : copy("pages.live_session.fate.failure")),
      automatic: copy("pages.live_session.fate.failure_automatic"),
      recovery: copy("pages.live_session.fate.failure_recovery"),
    },
  });
}

function previewBody(item) {
  const alternatives = item.alternatives.length
    ? el("ul", { class: "tavern-fate-alternatives" }, item.alternatives.map((value) => el("li", { text: value })))
    : el("p", { class: "tavern-source-note", text: copy("pages.live_session.fate.alternatives_empty") });
  return el("div", { class: "tavern-fate-body" }, [
    el("dl", { class: "tavern-fate-facts" }, [
      el("div", {}, [el("dt", { text: copy("pages.live_session.fate.source") }), el("dd", { text: item.source })]),
      el("div", {}, [el("dt", { text: copy("pages.live_session.fate.reason") }), el("dd", { text: item.reason })]),
      item.rescueWindow ? el("div", {}, [el("dt", { text: copy("pages.live_session.fate.rescue") }), el("dd", { text: item.rescueWindow })]) : null,
      item.expiresOn ? el("div", {}, [el("dt", { text: copy("pages.live_session.fate.expires") }), el("dd", { text: item.expiresOn })]) : null,
    ]),
    el("section", { class: "tavern-fate-alternative-block" }, [
      el("h4", { text: copy("pages.live_session.fate.alternatives") }),
      alternatives,
    ]),
  ]);
}

export function renderActorFateConsent(data = {}, handlers = {}) {
  const model = actorFateViewModel(data);
  if (!model) return null;
  const root = el("section", {
    class: "tavern-fate-consent",
    "data-testid": "tavern-live-actor-fate-consent",
    "aria-labelledby": "tavern-live-fate-title",
  });
  root.append(el("header", { class: "tavern-fate-heading" }, [
    el("div", {}, [
      el("p", { class: "tavern-page-eyebrow", text: copy("pages.live_session.fate.eyebrow") }),
      el("h3", { id: "tavern-live-fate-title", text: copy("pages.live_session.fate.title") }),
      el("p", { text: model.message || copy("pages.live_session.fate.summary") }),
    ]),
  ]));
  if (["permission", "forbidden", "error", "unavailable"].includes(model.state)) {
    root.append(problemPanel(model.state, model.message));
    return root;
  }
  if (!model.items.length) {
    root.append(renderStatePanel({
      phase: "empty",
      operation: copy("pages.live_session.fate.operation"),
      emptyCopy: model.message || copy("pages.live_session.fate.empty"),
    }));
    return root;
  }
  const cards = model.items.map((item, index) => {
    const actions = model.actions
      .filter((descriptor) => text(descriptor.target_key || descriptor.object_key) === item.key)
      .map((descriptor) => renderButton({
        variant: descriptor.intent === "actor_fate.preview.accept" ? "primary" : "secondary",
        label: descriptor.label,
        intent: { id: descriptor.intent },
        onActivate: (_intent, event) => openSessionConfigurationAction(descriptor, handlers, event.currentTarget),
      }));
    return renderBusinessCard({
      kind: "actor-fate-consent",
      className: "tavern-fate-card",
      opaqueKey: `preview-${index + 1}`,
      kicker: copy("pages.live_session.fate.card_kicker"),
      title: `「${item.actor}」`,
      summary: copy("pages.live_session.fate.card_summary"),
      state: { state: "warning", label: item.status },
      body: previewBody(item),
      actions,
    });
  });
  root.append(el("div", { class: "tavern-fate-grid" }, cards));
  const missingActions = model.items.some((item) => !model.actionTargets.has(item.key));
  if (missingActions) {
    root.append(renderStatePanel({
      phase: "partial",
      operation: copy("pages.live_session.fate.operation"),
      problem: {
        message: copy("pages.live_session.fate.action_missing"),
        automatic: copy("pages.live_session.fate.failure_automatic"),
        recovery: copy("pages.live_session.fate.failure_recovery"),
      },
    }));
  }
  return root;
}
