import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderBusinessCard } from "../components/cards.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { renderStatusBadge } from "../components/status.js";
import { el } from "../pages/shared.js";
import { openConfirm } from "./confirm-dialog.js";
import { openDetail } from "./detail-dialog.js";
import { openEditor } from "./editor-dialog.js";
import { openSheet } from "./mobile-sheet.js";
import { renderDelivery } from "../visualizations/delivery.js";
import { renderActorCard, renderActorDetail } from "../visualizations/actor.js";
import { renderNarrativeDocument } from "../visualizations/narrative-document.js";
import { renderWorldLens } from "../visualizations/world-lens.js";

export const SESSION_DETAIL_TABS = Object.freeze([
  { id: "overview", label: copy("dialogs.session_detail.module.label.a33db57305") },
  { id: "party", label: copy("dialogs.session_detail.module.label.31f13f6b64") },
  { id: "turn", label: copy("dialogs.session_detail.module.label.79dd2eecf2") },
  { id: "world", label: copy("dialogs.session_detail.module.label.fc13582263") },
  { id: "delivery", label: copy("dialogs.session_detail.module.label.22c404d449") },
  { id: "management", label: copy("dialogs.session_detail.module.label.bb6d995724") },
]);

const FIELD_LABELS = Object.freeze({
  action: copy("dialogs.session_detail.module.message.61eb3bfc5c"),
  reason: copy("dialogs.session_detail.module.message.d30c83cc43"),
  confirmation_name: copy("dialogs.session_detail.module.message.41074b7909"),
  acknowledge_archive: copy("dialogs.session_detail.module.message.93f6f96393"),
  acknowledge_pacing: copy("dialogs.dialogs.module.message.e9e9a42d54"),
  pacing_action: copy("dialogs.session_detail.module.message.61eb3bfc5c"),
  mode: copy("pages.live_session.controlPanel.text.16e8f15cb9"),
  enabled: copy("visualizations.generation.rc8.26644959a6"),
  interval_seconds: copy("pages.live_session.rc8.32c493ff09"),
  inherit_global: copy("pages.live_session.rc8.bbfd71b211"),
});

const FIELD_LABEL_KEYS = Object.freeze({
  "action.field.narrative_mode": copy("pages.live_session.controlPanel.text.16e8f15cb9"),
  "action.field.generation_reminder_enabled": copy("visualizations.generation.rc8.26644959a6"),
  "action.field.generation_reminder_interval": copy("pages.live_session.rc8.32c493ff09"),
  "action.field.generation_reminder_inherit_global": copy("pages.live_session.rc8.bbfd71b211"),
  "action.field.pacing_action": copy("dialogs.session_detail.module.message.61eb3bfc5c"),
  "action.field.acknowledge_pacing": copy("dialogs.dialogs.module.message.e9e9a42d54"),
});

function list(value) {
  return Array.isArray(value) ? value : [];
}

function text(value, fallback = "") {
  const result = String(value ?? "").trim();
  return result || fallback;
}

function numericLabel(value, suffix = "") {
  if (value === null || value === undefined || value === "") return copy("dialogs.session_detail.numericLabel.message.a8b0bd9c65");
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toLocaleString()}${suffix}` : copy("dialogs.session_detail.numericLabel.message.a8b0bd9c65");
}

function itemLabel(item, fallback = copy("dialogs.session_detail.itemLabel.message.99032465e3")) {
  return text(item?.label || item?.title || item?.name, fallback);
}

function semanticList(items, emptyCopy) {
  if (!items.length) return renderStatePanel({ phase: "empty", emptyCopy });
  return el("ul", { class: "tavern-detail-list" }, items.map((item) =>
    el("li", {}, [
      el("div", {}, [
        el("strong", { text: itemLabel(item) }),
        item?.summary || item?.description
          ? el("p", { text: item.summary || item.description })
          : null,
      ]),
      item?.state || item?.status
        ? renderStatusBadge({ state: item.state || item.status, label: item.state_label || item.state || item.status })
        : null,
    ])));
}

function definitions(items) {
  return el("dl", { class: "tavern-detail-definitions" }, items
    .filter((item) => item.value !== undefined && item.value !== "")
    .map((item) => el("div", {}, [
      el("dt", { text: item.label }),
      el("dd", { text: item.value === null ? copy("dialogs.session_detail.definitions.message.756762e293") : item.value }),
    ])));
}

function detailSection(title, children = [], className = "") {
  return el("section", { class: `tavern-detail-section ${className}`.trim() }, [
    el("h3", { text: title }),
    ...children,
  ]);
}

function densityStrip(items) {
  const visible = items.filter((item) => item?.label && item?.value !== undefined && item?.value !== null && item?.value !== "");
  return el("div", { class: "tavern-detail-density" }, visible.map((item) =>
    el("div", { class: "tavern-detail-density-stat" }, [
      el("small", { text: item.label }),
      el("strong", { text: item.value }),
      item.detail ? el("span", { text: item.detail }) : null,
    ])));
}

function detailDisclosure(label, value, content) {
  return el("details", { class: "tavern-detail-disclosure" }, [
    el("summary", {}, [el("span", { text: label }), value ? el("span", { text: value }) : null]),
    el("div", {}, [content]),
  ]);
}

function summaryData(envelope) {
  return envelope?.data && typeof envelope.data === "object" ? envelope.data : {};
}

function modelRuntime(model, objectKey = "") {
  const runtime = model?.sections?.find((section) => section.id === "lens")?.value?.runtime || {};
  const groups = list(model?.sections?.find((section) => section.id === "groups")?.value);
  const items = groups.flatMap((group) => Array.isArray(group?.items) ? group.items : [group]);
  const item = items.find((candidate) => text(candidate?.key) === objectKey) || {};
  return {
    session: { label: item.label, state: item.state, world_label: item.world_label, ...runtime.session },
    story: runtime.story || {},
    decision: runtime.decision || {},
    vote: runtime.vote || {},
    token_summary: runtime.token_summary || runtime.quota_summary || {},
    turn: { round: item.round, current_name: item.actor_label, ...runtime.turn },
    pressure: { active_timers: item.active_timers, ...runtime.pressure },
    scene_label: runtime.scene_label || item.scene_label,
    progress_label: runtime.progress_label || item.progress_label,
    narrative_mode: runtime.narrative_mode || {},
    generation_reminder: runtime.generation_reminder || {},
    ui_profile: runtime.ui_profile || {},
    latest_sequence: runtime.latest_sequence,
  };
}

function overviewPanel(envelope, handlers) {
  const data = summaryData(envelope);
  const session = data.session || {};
  const story = data.story || {};
  const pressure = data.pressure || {};
  const tokens = data.token_summary || null;
  const tokenRows = tokens ? [
    { label: copy("dialogs.session_detail.overviewPanel.label.2ba93e0831"), value: numericLabel(tokens.hour, " tokens") },
    { label: copy("dialogs.session_detail.overviewPanel.label.5c9cf5e0dd"), value: numericLabel(tokens.day, " tokens") },
    { label: copy("dialogs.session_detail.overviewPanel.label.23585a2033"), value: numericLabel(tokens.all, " tokens") },
    { label: copy("dialogs.session_detail.overviewPanel.label.9251ea0bed"), value: tokens.quota_label || copy("dialogs.session_detail.overviewPanel.message.0ae3985a3c") },
  ] : [];
  const configuration = list(data.configuration_actions || data.available_actions)
    .map((item) => normalizedAction(item, handlers?.navigation?.objectKey || ""))
    .filter(Boolean);
  const primary = el("div", { class: "tavern-detail-overview-primary" }, [
    detailSection(copy("dialogs.session_detail.overviewPanel.message.4771a6ddfd"), [
      renderNarrativeDocument(story, {
        compact: true,
        controls: false,
        narrativeMode: data.narrative_mode || {},
      }),
    ], "tavern-detail-story-panel"),
    detailSection(copy("dialogs.session_detail.overviewPanel.message.9473e64e8f"), [definitions([
      { label: copy("dialogs.session_detail.overviewPanel.label.792ff497ce"), value: data.decision?.state_label || data.decision?.state || copy("dialogs.session_detail.overviewPanel.message.4411e1ec8c") },
      { label: copy("dialogs.session_detail.overviewPanel.label.5d554b7ce6"), value: data.vote?.state_label || data.vote?.state || copy("dialogs.session_detail.overviewPanel.message.2b6f9f7e1f") },
      { label: copy("dialogs.session_detail.overviewPanel.label.d28c889060"), value: numericLabel(pressure.active_timers, copy("dialogs.session_detail.overviewPanel.message.f9d529eacd")) },
    ])]),
  ]);
  const density = densityStrip([
    { label: copy("dialogs.session_detail.overviewPanel.label.6b6c3f66ac"), value: session.state_label || session.state || copy("dialogs.session_detail.overviewPanel.message.cdea037991"), detail: session.world_label },
    { label: copy("pages.live_session.summary.world"), value: session.world_label, detail: story.title },
    { label: copy("dialogs.session_detail.overviewPanel.label.2ac8d3648a"), value: data.turn?.round !== undefined ? copy("dialogs.session_detail.overviewPanel.message.9db1804608", { p0: data.turn.round }) : copy("dialogs.session_detail.overviewPanel.message.1d29ffecf0"), detail: data.turn?.current_name },
    { label: copy("dialogs.session_detail.turnPanel.label.568c1e74fb"), value: data.turn?.remaining_seconds !== null && data.turn?.remaining_seconds !== undefined ? copy("dialogs.session_detail.turnPanel.message.67d54f9c74", { p0: data.turn.remaining_seconds }) : copy("dialogs.session_detail.turnPanel.message.e461e29890"), detail: data.session?.input_locked ? copy("dialogs.session_detail.turnPanel.message.56cee90958") : copy("dialogs.session_detail.turnPanel.message.35f47f9f98") },
    { label: copy("dialogs.session_detail.overviewPanel.label.9251ea0bed"), value: tokens?.quota_label || copy("dialogs.session_detail.overviewPanel.message.0ae3985a3c"), detail: data.narrative_mode?.label },
  ]);
  const secondary = el("div", { class: "tavern-detail-grid tavern-detail-overview-secondary" }, [
    detailSection(copy("dialogs.session_detail.overviewPanel.message.7588edbf92"), [tokens
      ? definitions(tokenRows)
      : renderStatePanel({ phase: "empty", emptyCopy: copy("dialogs.session_detail.overviewPanel.emptyCopy.5e056799f0") })]),
    configuration.length ? detailSection(copy("pages.live_session.controlPanel.text.fee03c832e"), [
      el("div", { class: "tavern-management-actions" }, configuration.map((descriptor) =>
        renderBusinessCard({
          kind: "session-configuration",
          opaqueKey: descriptor.action,
          title: descriptor.label,
          summary: descriptor.description,
          actions: [renderButton({
            variant: "secondary",
            label: descriptor.label,
            intent: { id: descriptor.action },
            onActivate: (_intent, event) => runManagementAction(descriptor, handlers, event.currentTarget),
          })],
        }))),
    ]) : null,
  ]);
  return el("div", { class: "tavern-detail-panel-stack", "data-detail-panel": "overview" }, [primary, density, secondary]);
}

function partyPanel(envelope, handlers) {
  const data = summaryData(envelope);
  const items = list(data.items);
  if (!items.length) return renderStatePanel({ phase: "empty", emptyCopy: copy("dialogs.session_detail.partyPanel.emptyCopy.0bf7f9b790") });
  const profile = data.ui_profile || {};
  const current = items.filter((item) => item.is_current || item.action_state === "acting").length;
  const companions = items.filter((item) => String(item.kind || "").includes("ai")).length;
  const statuses = items.reduce((total, item) => total + list(item.statuses).length, 0);
  return el("div", { class: "tavern-detail-panel-stack", "data-detail-panel": "party" }, [
    densityStrip([
      { label: SESSION_DETAIL_TABS[1].label, value: numericLabel(items.length) },
      { label: copy("dialogs.session_detail.overviewPanel.label.57184550c6"), value: numericLabel(current) },
      { label: copy("pages.live_session.renderPartyLens.message.1d97be5795"), value: numericLabel(companions) },
      { label: copy("dialogs.session_detail.turnPanel.label.6320b4a872"), value: numericLabel(statuses) },
    ]),
    el("div", { class: "tavern-detail-card-grid tavern-actor-grid" }, items.map((item) =>
      renderActorCard(item, {
        uiProfile: profile,
        onOpen: (actor, opener) => openActorDetail({ actor, uiProfile: profile, handlers, opener }),
      }))),
  ]);
}

function turnPanel(envelope) {
  const data = summaryData(envelope);
  const turn = data.turn || {};
  const decision = data.decision || {};
  const vote = data.vote || {};
  const pressure = data.pressure || {};
  const tokens = data.token_summary || null;
  const choices = list(decision.options || decision.choices);
  const primary = el("div", { class: "tavern-detail-grid tavern-detail-turn-primary" }, [
    detailSection(copy("dialogs.session_detail.turnPanel.message.7714978461"), [
      definitions([
        { label: copy("dialogs.session_detail.turnPanel.label.57184550c6"), value: turn.current_name || turn.actor_label || copy("dialogs.session_detail.turnPanel.message.c787b97972") },
        { label: copy("dialogs.session_detail.turnPanel.label.4890584bb5"), value: turn.round !== undefined ? copy("dialogs.session_detail.turnPanel.message.9db1804608", { p0: turn.round }) : copy("dialogs.session_detail.turnPanel.message.1d29ffecf0") },
        { label: copy("dialogs.session_detail.turnPanel.label.568c1e74fb"), value: turn.remaining_seconds !== null && turn.remaining_seconds !== undefined ? copy("dialogs.session_detail.turnPanel.message.67d54f9c74", { p0: turn.remaining_seconds }) : copy("dialogs.session_detail.turnPanel.message.e461e29890") },
        { label: copy("dialogs.session_detail.turnPanel.label.0ba40a0bf3"), value: data.session?.input_locked ? copy("dialogs.session_detail.turnPanel.message.56cee90958") : copy("dialogs.session_detail.turnPanel.message.35f47f9f98") },
      ]),
      semanticList(list(turn.order), copy("dialogs.session_detail.turnPanel.message.b76e622502")),
      semanticList(choices, copy("dialogs.session_detail.turnPanel.message.034190a608")),
    ]),
    detailSection(copy("dialogs.session_detail.turnPanel.message.0409fe1782"), [vote && Object.keys(vote).length
      ? definitions([
        { label: copy("dialogs.session_detail.turnPanel.label.788db1cfec"), value: vote.title || copy("dialogs.session_detail.turnPanel.message.8af6a54a17") },
        { label: copy("dialogs.session_detail.turnPanel.label.6320b4a872"), value: vote.state_label || vote.state || copy("dialogs.session_detail.turnPanel.message.cdea037991") },
        { label: copy("dialogs.session_detail.turnPanel.label.d3e5db8b5e"), value: vote.voted_count ?? 0 },
        { label: copy("dialogs.session_detail.turnPanel.label.8ba9cb6163"), value: vote.unvoted_count ?? copy("dialogs.session_detail.turnPanel.message.459961d501") },
      ])
      : renderStatePanel({ phase: "empty", emptyCopy: copy("dialogs.session_detail.turnPanel.emptyCopy.7027edca03") })]),
  ]);
  const countdown = detailDisclosure(
    copy("dialogs.session_detail.turnPanel.label.568c1e74fb"),
    numericLabel(pressure.active_timers, copy("dialogs.session_detail.overviewPanel.message.f9d529eacd")),
    semanticList(list(pressure.items), copy("dialogs.session_detail.turnPanel.message.e461e29890")),
  );
  const tokenPolicy = detailDisclosure(
    copy("dialogs.session_detail.overviewPanel.message.7588edbf92"),
    tokens?.quota_label || copy("dialogs.session_detail.overviewPanel.message.0ae3985a3c"),
    tokens ? definitions([
      { label: copy("dialogs.session_detail.overviewPanel.label.2ba93e0831"), value: numericLabel(tokens.hour, " tokens") },
      { label: copy("dialogs.session_detail.overviewPanel.label.5c9cf5e0dd"), value: numericLabel(tokens.day, " tokens") },
      { label: copy("dialogs.session_detail.overviewPanel.label.23585a2033"), value: numericLabel(tokens.all, " tokens") },
      { label: copy("dialogs.session_detail.overviewPanel.label.9251ea0bed"), value: tokens.quota_label },
    ]) : renderStatePanel({ phase: "empty", emptyCopy: copy("dialogs.session_detail.overviewPanel.emptyCopy.5e056799f0") }),
  );
  return el("div", { class: "tavern-detail-panel-stack", "data-detail-panel": "turn" }, [primary, countdown, tokenPolicy]);
}

function worldPanel(envelope, handlers) {
  const panel = renderWorldLens(envelope, { lens: "world", handlers });
  panel.classList.add("tavern-detail-world-lens");
  panel.dataset.detailPanel = "world";
  return panel;
}

function deliveryPanel(envelope, handlers) {
  const data = summaryData(envelope);
  const deliveries = list(data.deliveries?.items || data.deliveries);
  const timeline = list(data.timeline?.items);
  const openReceipt = (item, opener) => {
    const content = definitions([
      { label: copy("dialogs.session_detail.openReceipt.label.7a68830642"), value: itemLabel(item, copy("dialogs.session_detail.openReceipt.message.ac1cfdde62")) },
      { label: copy("dialogs.session_detail.openReceipt.label.6320b4a872"), value: item.state_label || item.state || item.status || copy("dialogs.session_detail.openReceipt.message.cdea037991") },
      { label: copy("dialogs.session_detail.openReceipt.label.42bf15cd2f"), value: item.summary || copy("dialogs.session_detail.openReceipt.message.529d991ecb") },
      { label: copy("dialogs.session_detail.openReceipt.label.976dfb627c"), value: item.receipt_label || copy("dialogs.session_detail.openReceipt.message.b9b60164a2") },
    ]);
    return openSheet(handlers.dialogs, { title: copy("dialogs.session_detail.openReceipt.title.0dcaf08713"), content, opener, returnToPrevious: true });
  };
  const deliveryRows = deliveries.map((item) => el("li", {}, [
    el("div", {}, [
      el("strong", { text: itemLabel(item, copy("dialogs.session_detail.openReceipt.message.f0c5fa02c9")) }),
      el("p", { text: item.summary || item.state_label || item.state || copy("dialogs.session_detail.openReceipt.message.cdea037991") }),
    ]),
    renderButton({
      variant: "secondary",
      label: copy("dialogs.session_detail.openReceipt.label.17a359052f"),
      onActivate: (_intent, event) => openReceipt(item, event.currentTarget),
    }),
  ]));
  return el("div", { class: "tavern-detail-panel-stack", "data-detail-panel": "delivery" }, [
    detailSection(copy("dialogs.session_detail.openReceipt.message.7860e2d753"), [deliveryRows.length
      ? el("ul", { class: "tavern-detail-list tavern-detail-delivery-list" }, deliveryRows)
      : renderStatePanel({ phase: "empty", emptyCopy: copy("dialogs.session_detail.openReceipt.emptyCopy.d0019e59da") })]),
    detailDisclosure(
      copy("dialogs.session_detail.openReceipt.message.1584ab0629"),
      numericLabel(timeline.length),
      el("div", { class: "tavern-detail-panel-stack" }, [
        deliveries.length ? renderDelivery(deliveries) : null,
        semanticList(timeline, copy("dialogs.session_detail.openReceipt.message.77f59136dd")),
      ]),
    ),
  ]);
}

function normalizedAction(raw, objectKey) {
  const intent = text(raw?.action || raw?.intent);
  if (!intent || raw?.transportReady !== true) return null;
  return { ...raw, action: intent, object_key: text(raw.object_key || raw.target_key, objectKey) };
}

function editorFields(descriptor) {
  return list(descriptor.fields).map((field) => ({
    ...field,
    label: field.label || FIELD_LABEL_KEYS[field.labelKey] || FIELD_LABELS[field.name] || copy("dialogs.session_detail.editorFields.message.996f2eeff2"),
    hint: field.hint || (field.name === "confirmation_name" ? copy("dialogs.session_detail.editorFields.message.685de91ca5") : ""),
    value: field.value ?? field.default ?? (["boolean", "checkbox"].includes(field.type) ? false : ""),
  }));
}

function fieldErrors(fields, draft) {
  return Object.fromEntries(fields
    .filter((field) => field.required && (field.type === "checkbox" ? !draft[field.name] : !text(draft[field.name])))
    .map((field) => [field.name, copy("dialogs.session_detail.fieldErrors.message.c54c78b568", {p0: field.label})]));
}

function actionConfirmation(descriptor) {
  const action = descriptor.action;
  const permanent = /\.(finish|abort)$/.test(action);
  return {
    impact: descriptor.description || copy("dialogs.session_detail.actionConfirmation.message.21d8f5baf7"),
    unchanged: permanent
      ? copy("dialogs.session_detail.actionConfirmation.message.a5e20846cb")
      : copy("dialogs.session_detail.actionConfirmation.message.22295aa4d1"),
    automatic: permanent
      ? copy("dialogs.session_detail.actionConfirmation.message.207dfc4a3a")
      : copy("dialogs.session_detail.actionConfirmation.message.2c2256124d"),
    recovery: permanent
      ? copy("dialogs.session_detail.actionConfirmation.message.792566fe22")
      : copy("dialogs.session_detail.actionConfirmation.message.e4d626bb15"),
    returnCheck: copy("dialogs.session_detail.actionConfirmation.message.78759fb45d"),
  };
}

function irreversibleAction(action) {
  return /\.(finish|abort)$/.test(action);
}

function responseData(payload) {
  for (const candidate of [payload, payload?.data, payload?.body, payload?.data?.data, payload?.data?.body]) {
    if (candidate?.data && typeof candidate.data === "object") return candidate.data;
  }
  return null;
}

function pacingContinuation(payload, fallbackLabel) {
  const data = responseData(payload);
  const raw = data?.continuation;
  const continuation = normalizedAction(raw, "");
  if (
    !continuation
    || continuation.action !== "session.pacing.commit"
    || !Number.isInteger(continuation.expected_revision)
    || continuation.expected_revision < 0
    || !text(continuation.object_key)
    || !list(continuation.fields).some((field) => field?.name === "acknowledge_pacing" && field?.required === true)
  ) {
    const error = new Error(copy("dialogs.session_detail.renderActions.message.5ca627d269"));
    error.recovery = copy("dialogs.session_detail.actionConfirmation.message.e4d626bb15");
    error.retryable = true;
    throw error;
  }
  return { ...continuation, label: text(raw.label || data?.target?.label, fallbackLabel) };
}

function runManagementAction(descriptor, handlers, opener) {
  const fields = editorFields(descriptor);
  const returnOpener = handlers.detailOpener || opener;
  if (fields.length) {
    const confirmation = actionConfirmation(descriptor);
    const dangerous = irreversibleAction(descriptor.action) || descriptor.action === "session.pacing.commit";
    const previewStep = descriptor.action === "session.pacing.preview";
    return openEditor(handlers.dialogs, {
      objectKey: descriptor.object_key,
      revision: descriptor.expected_revision,
      fields,
      opener: returnOpener,
      title: descriptor.label || copy("dialogs.session_detail.runManagementAction.message.01b4a97665"),
      idempotencyKey: crypto.randomUUID(),
      validate: (draft) => fieldErrors(fields, draft),
      preview: dangerous
        ? () => ({ summary: `${confirmation.impact} ${confirmation.unchanged}` })
        : null,
      submit: async ({ draft, idempotencyKey }) => dangerous
        ? {
          afterClose: () => openConfirm(handlers.dialogs, {
            opener: returnOpener,
            operation: descriptor.label || copy("dialogs.session_detail.runManagementAction.message.e92b4d6e88"),
            ...confirmation,
            confirmLabel: descriptor.action.endsWith(".finish")
              ? copy("dialogs.session_detail.runManagementAction.message.dca5bfd470")
              : descriptor.action.endsWith(".abort")
                ? copy("dialogs.session_detail.runManagementAction.message.ba4430910a")
                : descriptor.label || copy("dialogs.session_detail.runManagementAction.message.7ab88b64e5"),
            intent: { id: descriptor.action },
            idempotencyKey,
            onConfirm: ({ idempotencyKey: confirmedKey }) => handlers.actions.execute(descriptor, {
              opener: returnOpener,
              input: draft,
              idempotencyKey: confirmedKey,
            }),
          }),
        }
        : previewStep
          ? handlers.actions.execute(descriptor, {
                opener: returnOpener,
                input: draft,
                idempotencyKey,
              }).then((payload) => {
                const continuation = pacingContinuation(payload, descriptor.label);
                return {
                  afterClose: () => runManagementAction(continuation, handlers, returnOpener),
                };
              })
          : handlers.actions.execute(descriptor, {
            opener: returnOpener,
            input: draft,
            idempotencyKey,
          }),
    });
  }
  if (!irreversibleAction(descriptor.action)) {
    return handlers.actions.execute(descriptor, {
      opener: returnOpener,
      idempotencyKey: crypto.randomUUID(),
    });
  }
  const confirmation = actionConfirmation(descriptor);
  const idempotencyKey = crypto.randomUUID();
  return openConfirm(handlers.dialogs, {
    opener: returnOpener,
    operation: descriptor.label || copy("dialogs.session_detail.runManagementAction.message.d1231867ba"),
    ...confirmation,
    confirmLabel: descriptor.label || copy("dialogs.session_detail.runManagementAction.message.7ab88b64e5"),
    intent: { id: descriptor.action },
    idempotencyKey,
    onConfirm: ({ idempotencyKey: confirmedKey }) => handlers.actions.execute(descriptor, { opener: returnOpener, idempotencyKey: confirmedKey }),
  });
}

function managementPanel(payload, objectKey, handlers) {
  const summary = summaryData(payload.summary);
  const actions = list(summary.available_actions)
    .map((item) => normalizedAction(item, objectKey)).filter(Boolean);
  const unique = [...new Map(actions.map((item) => [item.action, item])).values()];
  const safe = unique.filter((item) => !irreversibleAction(item.action));
  const lifecycle = unique.filter((item) => irreversibleAction(item.action));
  const renderActions = (items, emptyCopy) => items.length
    ? el("div", { class: "tavern-management-actions" }, items.map((descriptor) =>
      renderBusinessCard({
        kind: "management-action",
        opaqueKey: descriptor.action,
        title: descriptor.label || copy("dialogs.session_detail.renderActions.message.27d705b1e6"),
        summary: descriptor.description || copy("dialogs.session_detail.renderActions.message.5ca627d269"),
        actions: [renderButton({
          variant: irreversibleAction(descriptor.action) ? "danger" : "secondary",
          label: descriptor.label || copy("dialogs.session_detail.renderActions.message.4966efa592"),
          intent: { id: descriptor.action },
          onActivate: (_intent, event) => runManagementAction(descriptor, handlers, event.currentTarget),
        })],
      })))
    : renderStatePanel({ phase: "readonly", operation: copy("dialogs.session_detail.renderActions.operation.746dc855a9"), problem: { message: emptyCopy, recovery: copy("dialogs.session_detail.renderActions.recovery.77cb92af56") } });
  return el("div", { class: "tavern-detail-panel-stack", "data-detail-panel": "management" }, [
    el("div", { class: "tavern-detail-grid tavern-detail-management-primary" }, [
      detailSection(copy("dialogs.session_detail.renderActions.message.a08260c4d3"), [renderActions(safe, copy("dialogs.session_detail.renderActions.message.9f8fed882c"))]),
      detailSection(copy("dialogs.session_detail.renderActions.message.d0baa11deb"), [
        el("p", { class: "tavern-danger-copy", text: copy("dialogs.session_detail.actionConfirmation.message.207dfc4a3a") }),
        renderActions(lifecycle, copy("dialogs.session_detail.renderActions.message.b67ce534ac")),
      ], "tavern-detail-danger"),
    ]),
  ]);
}

async function read(client, endpoint, objectKey, signal, operation, extra = {}) {
  return client.get(endpoint, {
    query: { session_key: objectKey, ...extra },
    signal,
    operation,
  });
}

async function loadPanel(tab, objectKey, handlers, signal) {
  if (tab === "overview") return overviewPanel(await read(handlers.client, "dashboard/session-summary", objectKey, signal, copy("dialogs.session_detail.loadPanel.message.6f40fdd686")), handlers);
  if (tab === "party") return partyPanel(await read(handlers.client, "dashboard/session-party", objectKey, signal, copy("dialogs.session_detail.loadPanel.message.4cedd93e9e")), handlers);
  if (tab === "turn") return turnPanel(await read(handlers.client, "dashboard/session-summary", objectKey, signal, copy("dialogs.session_detail.loadPanel.message.16e3d41893")));
  if (tab === "world") return worldPanel(await read(handlers.client, "dashboard/session-world-visuals", objectKey, signal, copy("dialogs.session_detail.loadPanel.message.5302e2432f"), { placement: "session_detail" }), handlers);
  if (tab === "delivery") return deliveryPanel(await read(handlers.client, "dashboard/session-history", objectKey, signal, copy("dialogs.session_detail.loadPanel.message.edc5a9d059")), handlers);
  const summary = await read(handlers.client, "dashboard/session-summary", objectKey, signal, copy("dialogs.session_detail.loadPanel.message.7072dbe33e"));
  return managementPanel({ summary }, objectKey, handlers);
}

export function openActorDetail({ actor, uiProfile = {}, handlers, opener } = {}) {
  if (!actor || !handlers?.dialogs) return null;
  const name = itemLabel(actor, copy("dialogs.session_detail.partyPanel.message.6be2b9ac7d"));
  return handlers.dialogs.openDialog({
    kind: "detail",
    opener,
    title: copy("dialogs.session_detail.openSessionDetail.title.b6625dad16", { p0: name }),
    size: "large",
    content: renderActorDetail(actor, uiProfile),
    returnToPrevious: true,
  });
}

export function openSessionConfigurationAction(descriptor, handlers, opener) {
  const normalized = normalizedAction(descriptor, handlers?.navigation?.objectKey || "");
  if (!normalized || !handlers?.actions || !handlers?.dialogs) return null;
  return runManagementAction(normalized, handlers, opener);
}

export function openSessionDetail({ model, handlers, opener, activeTab = "" } = {}) {
  const objectKey = text(handlers?.navigation?.objectKey);
  if (!objectKey || !handlers?.client || !handlers?.dialogs) return null;
  const runtime = modelRuntime(model, objectKey);
  const session = runtime.session || {};
  const tokens = runtime.token_summary || {};
  const summaryFacts = [
    { label: copy("dialogs.session_detail.overviewPanel.label.6b6c3f66ac"), value: session.state_label || session.state },
    { label: copy("pages.live_session.summary.world"), value: session.world_label },
    { label: copy("dialogs.session_detail.overviewPanel.label.57184550c6"), value: runtime.turn?.current_name || runtime.turn?.actor_label },
    { label: copy("pages.live_session.pacing.story_turn"), value: session.turn !== undefined ? numericLabel(session.turn) : null },
    { label: copy("pages.live_session.summary.round"), value: runtime.turn?.round !== undefined ? copy("dialogs.session_detail.overviewPanel.message.9db1804608", { p0: runtime.turn.round }) : null },
    { label: copy("visualizations.scene_path.renderScenePath.message.676abfcdaf"), value: runtime.scene_label },
    { label: copy("pages.live_session.lower.world.objective"), value: runtime.story?.title || runtime.progress_label },
    { label: copy("dialogs.session_detail.turnPanel.label.568c1e74fb"), value: runtime.turn?.remaining_seconds !== null && runtime.turn?.remaining_seconds !== undefined ? copy("dialogs.session_detail.turnPanel.message.67d54f9c74", { p0: runtime.turn.remaining_seconds }) : null },
    { label: copy("dialogs.session_detail.overviewPanel.label.d28c889060"), value: runtime.pressure?.active_timers !== undefined ? numericLabel(runtime.pressure.active_timers, copy("dialogs.session_detail.overviewPanel.message.f9d529eacd")) : null },
    { label: copy("dialogs.session_detail.overviewPanel.label.16e8f15cb9"), value: runtime.narrative_mode?.label },
    { label: copy("dialogs.session_detail.overviewPanel.label.9251ea0bed"), value: tokens.quota_label },
  ];
  const context = { ...handlers, detailOpener: opener };
  return openDetail(handlers.dialogs, {
    objectKey,
    opener,
    title: copy("dialogs.session_detail.openSessionDetail.title.b6625dad16", {p0: session.label || model?.title || copy("dialogs.session_detail.openSessionDetail.message.bfe958dea6")}),
    kicker: copy("pages.sessions.group.details"),
    tabs: SESSION_DETAIL_TABS,
    activeTab: activeTab || handlers.navigation?.detailTab || "overview",
    summaryFacts,
    permissions: Object.fromEntries(SESSION_DETAIL_TABS.map((tab) => [
      tab.id,
      tab.id !== "management" || Boolean(model?.permissions?.can_manage || model?.permissions?.operate),
    ])),
    lazyPanelLoader: (tab, key, { signal }) => loadPanel(tab, key, context, signal),
  });
}

export function openLiveSummarySheet({ model, handlers, opener } = {}) {
  const data = modelRuntime(model);
  const pressure = data.pressure || {};
  const content = definitions([
    { label: copy("dialogs.session_detail.openLiveSummarySheet.label.57184550c6"), value: data.turn?.current_name || data.turn?.actor_label || copy("dialogs.session_detail.openLiveSummarySheet.message.c787b97972") },
    { label: copy("dialogs.session_detail.openLiveSummarySheet.label.2ac8d3648a"), value: data.turn?.round !== undefined ? copy("dialogs.session_detail.openLiveSummarySheet.message.9db1804608", {p0: data.turn.round}) : copy("dialogs.session_detail.openLiveSummarySheet.message.1d29ffecf0") },
    { label: copy("dialogs.session_detail.openLiveSummarySheet.label.568c1e74fb"), value: data.turn?.remaining_seconds !== null && data.turn?.remaining_seconds !== undefined ? copy("dialogs.session_detail.openLiveSummarySheet.message.67d54f9c74", {p0: data.turn.remaining_seconds}) : copy("dialogs.session_detail.openLiveSummarySheet.message.e461e29890") },
    { label: copy("dialogs.session_detail.openLiveSummarySheet.label.d28c889060"), value: numericLabel(pressure.active_timers, copy("dialogs.session_detail.openLiveSummarySheet.message.f9d529eacd")) },
  ]);
  return openSheet(handlers.dialogs, { title: copy("dialogs.session_detail.openLiveSummarySheet.title.c70e803de4"), content, opener });
}
