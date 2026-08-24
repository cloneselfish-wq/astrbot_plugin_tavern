import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { renderStatusBadge } from "../components/status.js";
import { openConfirm } from "../dialogs/confirm-dialog.js";
import { openEditor } from "../dialogs/editor-dialog.js";
import { el, label, pageRoot, rows, stateNotice, summary, value } from "./shared.js";

const SAVE_INTENT = "settings.group.save";
const RESTORE_PREVIEW_INTENT = "backup.restore.preview";
const SETTING_GROUP_ORDER = Object.freeze(["permissions", "model", "context", "time", "recovery", "panel"]);

function isSaveDescriptor(action) {
  return action?.intent === SAVE_INTENT && action.transportReady === true
    && Number.isInteger(action.expected_revision) && action.expected_revision >= 0;
}

function descriptorBy(panel, predicate) {
  const items = rows(panel?.items);
  const item = items.find((entry) => rows(entry.available_actions).some(predicate)) || items[0];
  const action = rows(panel?.available_actions).find(predicate)
    || rows(item?.available_actions).find(predicate);
  return action && item ? { ...action, object_key: item.key, target_key: item.key } : null;
}

const saveDescriptor = (panel) => descriptorBy(panel, isSaveDescriptor);

function isRestorePreviewDescriptor(action) {
  return action?.intent === RESTORE_PREVIEW_INTENT && action.transportReady === true
    && Number.isInteger(action.expected_revision) && action.expected_revision >= 0
    && rows(action.fields).some((field) => field?.name === "file" && field?.type === "file");
}

const restorePreviewDescriptor = (panel) => descriptorBy(panel, isRestorePreviewDescriptor);

function displayValue(raw) {
  if (typeof raw === "boolean") return raw ? copy("pages.settings.rc8.8da97ddda9") : copy("dialogs.manager.resolvedDismissPolicy.message.3fd47edce4");
  if (raw === null || raw === undefined || raw === "") return copy("pages.settings.rc8.2f5f1d6fbf");
  return String(raw);
}

function metric(item) {
  return el("article", { class: "tavern-settings-metric" }, [
    el("span", { text: label(item, copy("pages.settings.control.message.44bc7ccc36")) }),
    el("strong", { text: displayValue(item?.value) }),
    item?.summary ? el("small", { text: item.summary }) : null,
  ]);
}

function policyRow(item) {
  return el("li", { class: "tavern-settings-policy-row" }, [
    el("div", {}, [
      el("strong", { text: label(item, copy("pages.settings.control.message.44bc7ccc36")) }),
      el("p", { text: summary(item, copy("pages.settings.control.message.5101570c1f")) }),
    ]),
    el("b", { text: displayValue(item?.value) }),
  ]);
}

function flowStep(item, index) {
  return el("li", { class: "tavern-setting-flow-step" }, [
    el("small", { text: String(index + 1).padStart(2, "0") }),
    el("strong", { text: label(item, copy("pages.settings.control.message.44bc7ccc36")) }),
    el("span", { text: summary(item, copy("pages.settings.control.message.5101570c1f")) }),
  ]);
}

function flowCard(items) {
  return el("section", { class: "tavern-setting-detail-card tavern-setting-flow-card" }, [
    el("header", { class: "tavern-setting-detail-head" }, [
      el("div", {}, [
        el("h4", { text: copy("pages.settings.policy.title") }),
        el("p", { text: copy("pages.settings.policy.summary") }),
      ]),
    ]),
    items.length
      ? el("ol", { class: "tavern-setting-flow" }, items.map(flowStep))
      : el("p", { class: "tavern-settings-empty-detail", text: copy("pages.settings.renderSettings.message.b86a1dbb92") }),
  ]);
}

function budgetCard(items) {
  const numeric = items.map((item) => ({ item, amount: Number(item?.value) })).filter(({ amount }) => Number.isFinite(amount));
  const ceiling = Math.max(1, ...numeric.map(({ amount }) => Math.abs(amount)));
  return el("section", { class: "tavern-setting-detail-card tavern-setting-budget-card" }, [
    el("header", { class: "tavern-setting-detail-head" }, [
      el("div", {}, [
        el("h4", { text: copy("pages.settings.summary.operation") }),
        el("p", { text: copy("pages.settings.policy.summary") }),
      ]),
    ]),
    numeric.length
      ? el("div", { class: "tavern-setting-budget-visual" }, numeric.map(({ item, amount }) => el("div", { class: "tavern-setting-budget-row" }, [
        el("span", { text: label(item, copy("pages.settings.control.message.44bc7ccc36")) }),
        el("div", { class: "tavern-setting-budget-track" }, [el("i", { style: `--value:${Math.max(4, Math.round(Math.abs(amount) / ceiling * 100))}%` })]),
        el("strong", { text: displayValue(item.value) }),
      ])))
      : el("p", { class: "tavern-settings-empty-detail", text: copy("pages.settings.summary.missing") }),
  ]);
}

function openGroupEditor(view, handlers, opener) {
  if (!view.action || view.readonly || !handlers.dialogs?.openDialog || !handlers.actions?.execute) return;
  openEditor(handlers.dialogs, {
    objectKey: view.action.object_key,
    revision: view.action.expected_revision,
    fields: rows(view.action.fields),
    opener,
    title: view.action.label || view.panel.label || label(view.panel),
    kicker: copy("pages.settings.editor.kicker"),
    specialization: "settings",
    intro: {
      kicker: copy("pages.settings.editor.kicker"),
      title: view.action.label || view.panel.label || label(view.panel),
      summary: view.action.description || copy("pages.settings.editor.summary"),
    },
    contextFacts: [
      { label: copy("pages.settings.editor.current_state"), value: copy("pages.settings.editor.loaded") },
      { label: copy("pages.settings.editor.save_scope"), value: copy("pages.settings.editor.current_group_only") },
      { label: copy("pages.settings.editor.conflict_handling"), value: copy("pages.settings.editor.keep_draft") },
    ],
    shellFooter: true,
    labels: {
      cancel: copy("pages.settings.editor.cancel"),
      submit: view.action.label || copy("pages.settings.renderSettings.label.ecf0ea0119"),
    },
    preview: () => ({ summary: view.action.description || summary(view.panel, copy("pages.settings.renderSettings.message.b86a1dbb92")) }),
    submit: ({ draft, idempotencyKey }) => handlers.actions?.execute(view.action, { opener, input: draft, idempotencyKey }),
  });
}

function recoveryEnvelope(payload) {
  for (const candidate of [payload, payload?.data, payload?.body, payload?.data?.data, payload?.data?.body]) {
    if (candidate?.data?.continuation) return candidate;
  }
  return null;
}

function validRecoveryContinuation(value) {
  return value?.intent === "backup.restore.execute" && value?.transportReady === true
    && typeof value?.target_key === "string" && value.target_key.length > 0
    && Number.isInteger(value?.expected_revision) && value.expected_revision >= 0
    && rows(value?.fields).some((field) => field?.name === "confirm_text");
}

function continuationProblem() {
  const error = new Error(copy("pages.settings.recovery.continuation_invalid"));
  error.recovery = copy("pages.settings.recovery.continuation_recovery");
  error.retryable = true;
  return error;
}

function openRestoreConfirmation(continuation, details, handlers, opener) {
  if (!handlers.dialogs?.openDialog || !handlers.actions?.execute) return;
  const replayKey = crypto.randomUUID();
  openEditor(handlers.dialogs, {
    objectKey: continuation.target_key,
    revision: continuation.expected_revision,
    fields: rows(continuation.fields),
    idempotencyKey: replayKey,
    opener,
    title: continuation.label || copy("pages.settings.recovery.execute_title"),
    kicker: copy("pages.settings.recovery.execute_kicker"),
    specialization: "settings-recovery-confirmation",
    intro: {
      kicker: copy("pages.settings.recovery.execute_kicker"),
      title: continuation.label || copy("pages.settings.recovery.execute_title"),
      summary: continuation.description || copy("pages.settings.recovery.execute_summary"),
    },
    contextFacts: rows(details).map((detail) => ({ label: detail.label, value: detail.summary || detail.state })),
    shellFooter: true,
    labels: {
      cancel: copy("pages.settings.recovery.cancel"),
      submit: copy("pages.settings.recovery.review_execute"),
    },
    submit: ({ draft, idempotencyKey }) => ({
      afterClose: () => openConfirm(handlers.dialogs, {
        opener,
        operation: continuation.label || copy("pages.settings.recovery.execute_title"),
        impact: continuation.description || copy("pages.settings.recovery.execute_summary"),
        unchanged: copy("pages.settings.recovery.unchanged"),
        automatic: rows(details).map((detail) => `${detail.label}：${detail.summary || detail.state}`).join("；") || copy("pages.settings.recovery.automatic"),
        recovery: copy("pages.settings.recovery.rollback"),
        returnCheck: copy("pages.settings.recovery.return_check"),
        confirmLabel: copy("pages.settings.recovery.confirm_execute"),
        intent: { id: continuation.intent },
        idempotencyKey,
        onConfirm: ({ idempotencyKey: confirmedKey }) => handlers.actions.execute(
          { ...continuation, object_key: continuation.target_key },
          { opener, input: draft, idempotencyKey: confirmedKey },
        ),
      }),
    }),
  });
}

function openRestorePreview(view, handlers, opener) {
  const descriptor = view.restorePreview;
  if (!descriptor || view.readonly || !handlers.dialogs?.openDialog || !handlers.client?.upload) return;
  const fields = rows(descriptor.fields).map((field) => (
    field?.type === "file" ? { ...field, accept: ".zip,application/zip" } : field
  ));
  openEditor(handlers.dialogs, {
    objectKey: descriptor.object_key,
    revision: descriptor.expected_revision,
    fields,
    opener,
    title: descriptor.label,
    kicker: copy("pages.settings.recovery.preview_kicker"),
    specialization: "settings-recovery-preview",
    intro: {
      kicker: copy("pages.settings.recovery.preview_kicker"),
      title: descriptor.label,
      summary: descriptor.description,
    },
    contextFacts: [
      { label: copy("pages.settings.recovery.current_state"), value: copy("pages.settings.recovery.not_uploaded") },
      { label: copy("pages.settings.recovery.save_scope"), value: copy("pages.settings.recovery.preview_only") },
      { label: copy("pages.settings.recovery.conflict_handling"), value: copy("pages.settings.recovery.rollback_protected") },
    ],
    shellFooter: true,
    labels: {
      cancel: copy("pages.settings.recovery.cancel"),
      submit: descriptor.label,
    },
    submit: async ({ draft, idempotencyKey }) => {
      const payload = await handlers.client.upload(
        "dashboard/recovery-preview",
        draft.file,
        { operation: descriptor.label, idempotencyKey },
      );
      const envelope = recoveryEnvelope(payload);
      const continuation = envelope?.data?.continuation;
      if (!validRecoveryContinuation(continuation)) throw continuationProblem();
      const details = rows(envelope?.data?.details);
      return { afterClose: () => openRestoreConfirmation(continuation, details, handlers, opener) };
    },
  });
}

function recoveryWorkflow(view, handlers) {
  if (view.key !== "recovery") return null;
  const available = Boolean(
    view.restorePreview && !view.readonly && handlers.dialogs?.openDialog && handlers.client?.upload,
  );
  const preview = renderButton({
    variant: "secondary",
    label: view.restorePreview?.label || copy("pages.settings.recovery.preview_action"),
    disabledReason: available ? "" : view.readonly ? copy("pages.settings.editor.readonly") : copy("pages.settings.editor.unavailable"),
    onActivate: (_intent, event) => openRestorePreview(view, handlers, event.currentTarget),
  });
  preview.dataset.settingsRecoveryPreview = "true";
  const steps = [
    ["01", copy("pages.settings.recovery.step_read"), copy("pages.settings.recovery.step_read_summary")],
    ["02", copy("pages.settings.recovery.step_check"), copy("pages.settings.recovery.step_check_summary")],
    ["03", copy("pages.settings.recovery.step_confirm"), copy("pages.settings.recovery.step_confirm_summary")],
  ];
  return el("section", { class: "tavern-settings-recovery" }, [
    el("header", {}, [el("h4", { text: copy("pages.settings.recovery.title") }), el("p", { text: copy("pages.settings.recovery.summary") })]),
    el("ol", { class: "tavern-settings-recovery-flow" }, steps.map(([number, title, detail]) => el("li", {}, [el("small", { text: number }), el("strong", { text: title }), el("span", { text: detail })]))),
    el("div", { class: "tavern-settings-recovery-action" }, [
      el("p", {}, [el("strong", { text: copy("pages.settings.recovery.protection_title") }), el("span", { text: copy("pages.settings.recovery.protection_summary") })]),
      preview,
    ]),
  ]);
}

function modelProbe(view, handlers) {
  if (view.key !== "model") return null;
  const providers = rows(view.panel?.model_chain).filter(Boolean);
  const status = el("p", { class: "tavern-settings-empty-detail", text: view.panel?.probe_summary || "" });
  const button = renderButton({
    variant: "secondary",
    label: view.panel?.probe_action_label || copy("pages.settings.model_probe.action"),
    disabledReason: providers.length && handlers.client?.post ? "" : copy("pages.settings.model_probe.unavailable"),
    onActivate: async () => {
      status.textContent = copy("pages.settings.model_probe.running");
      try {
        const request = { idempotency_key: crypto.randomUUID() };
        request[["provider", "ids"].join("_")] = providers;
        const payload = await handlers.client.post("console/providers/health-check", request);
        const entries = rows(payload?.probe?.items || payload?.data?.probe?.items);
        status.textContent = entries.length
          ? entries.map((item) => `${item.name || "模型"}：${item.status || "完成"}${Number.isFinite(Number(item.latency_ms)) ? ` · ${item.latency_ms} ms` : ""}`).join("；")
          : copy("pages.settings.model_probe.complete");
      } catch (error) {
        status.textContent = `检测失败：${error?.message || "模型服务暂时不可用"}`;
      }
    },
  });
  return el("section", { class: "tavern-setting-detail-card" }, [
    el("header", { class: "tavern-setting-detail-head" }, [el("div", {}, [
      el("h4", { text: view.panel?.probe_action_label || copy("pages.settings.model_probe.action") }), status,
    ])]), button,
  ]);
}

function panelView(panel, readonly, handlers) {
  const action = saveDescriptor(panel);
  const restorePreview = restorePreviewDescriptor(panel);
  const items = rows(panel.items);
  const summaryItems = rows(panel.summary_items);
  const visualItems = rows(panel.visual);
  const editable = Boolean(
    action && rows(action.fields).length && !readonly
    && handlers.dialogs?.openDialog && handlers.actions?.execute,
  );
  const view = { key: panel.key, panel, action, restorePreview, readonly, editable };
  const headlineMetric = summaryItems[0] || items[0] || null;
  const edit = renderButton({
    variant: "primary",
    label: action?.label || copy("pages.settings.renderSettings.label.ecf0ea0119"),
    disabledReason: editable
      ? ""
      : readonly
        ? copy("pages.settings.editor.readonly")
        : copy("pages.settings.editor.unavailable"),
    onActivate: (_intent, event) => openGroupEditor(view, handlers, event.currentTarget),
  });
  edit.setAttribute("data-settings-editor", panel.key);
  const policy = el("section", { class: "tavern-settings-policy tavern-setting-detail-card tavern-setting-policy-card" }, [
    el("header", {}, [el("h4", { text: copy("pages.settings.policy.title") }), el("p", { text: copy("pages.settings.policy.summary") })]),
    items.length ? el("ol", { class: "tavern-settings-policy-list" }, items.map(policyRow)) : el("p", { class: "tavern-settings-empty-detail", text: copy("pages.settings.renderSettings.message.b86a1dbb92") }),
  ]);
  const impact = el("article", { class: "tavern-setting-impact-card" }, [
    el("h4", { text: copy("pages.settings.impact.title") }),
    el("p", { text: panel.impact_summary || action?.description || summary(panel, copy("pages.settings.renderSettings.message.b86a1dbb92")) }),
  ]);
  const conflict = el("article", { class: "tavern-setting-impact-card tavern-is-warning" }, [
    el("h4", { text: copy("pages.settings.impact.conflict_title") }),
    el("p", { text: panel.conflict_summary || copy("pages.settings.impact.conflict_summary") }),
  ]);
  const numericItems = summaryItems.length ? summaryItems : items;
  const flowItems = visualItems.length ? visualItems : items;
  const primaryDetails = panel.key === "model" || panel.key === "context"
    ? [budgetCard(numericItems), flowCard(flowItems)]
    : panel.key === "permissions" || panel.key === "time" || panel.key === "recovery"
      ? [flowCard(flowItems), policy]
      : [policy, flowCard(flowItems)];
  const recovery = recoveryWorkflow(view, handlers);
  if (recovery) primaryDetails.push(recovery);
  const probe = modelProbe(view, handlers);
  if (probe) primaryDetails.push(probe);
  view.node = el("div", { class: "tavern-settings-panel tavern-setting-workspace", "data-settings-panel": panel.key }, [
    el("header", { class: "tavern-setting-panel-head tavern-settings-summary-first tavern-setting-hero" }, [
      el("div", { class: "tavern-setting-hero-copy" }, [
        el("p", { class: "tavern-settings-section-label", text: copy("pages.settings.renderSettings.message.4a48d745e3") }),
        el("h3", { text: panel.label || label(panel) }),
        el("p", { text: summary(panel, copy("pages.settings.renderSettings.message.b86a1dbb92")) }),
      ]),
      el("div", { class: "tavern-setting-state-visual" }, [
        el("b", { text: headlineMetric ? displayValue(headlineMetric.value) : "—" }),
        el("div", {}, [
          el("strong", { text: readonly ? copy("pages.settings.renderSettings.message.3b5ec3533b") : panel.state || copy("pages.settings.renderSettings.message.2c4b644304") }),
          el("small", { text: headlineMetric ? label(headlineMetric, panel.label || label(panel)) : panel.label || label(panel) }),
        ]),
      ]),
    ]),
    summaryItems.length
      ? el("section", { class: "tavern-settings-metrics tavern-setting-metric-grid", "aria-label": copy("pages.settings.summary.aria") }, summaryItems.map(metric))
      : renderStatePanel({ phase: "partial", operation: copy("pages.settings.summary.operation"), problem: { message: copy("pages.settings.summary.missing"), recovery: copy("pages.settings.summary.recovery") } }),
    el("div", { class: "tavern-setting-detail-layout" }, [
      el("div", { class: "tavern-setting-primary" }, primaryDetails),
      el("aside", { class: "tavern-setting-aside" }, [impact, conflict]),
    ]),
    el("footer", { class: "tavern-settings-detail-band tavern-setting-panel-actions tavern-setting-savebar" }, [
      el("span", { text: action?.description || summary(panel, copy("pages.settings.renderSettings.message.b86a1dbb92")) }),
      edit,
    ]),
  ]);
  return view;
}

export function renderSettings(model, handlers = {}) {
  const root = pageRoot(model, "tavern-settings");
  root.setAttribute("class", `${root.className} tavern-settings-page`);
  root.append(stateNotice(model, copy("pages.settings.renderSettings.message.69412243d6")));
  const groupsSection = value(model, "groups") || {};
  const navigationByKey = new Map(rows(groupsSection.navigation).map((group) => [group.key, group]));
  const panelByKey = new Map(rows(groupsSection.panels).map((panel) => [panel.key, panel]));
  const groups = SETTING_GROUP_ORDER.map((key) => navigationByKey.get(key)).filter(Boolean);
  const panels = SETTING_GROUP_ORDER.map((key) => panelByKey.get(key)).filter(Boolean);
  const groupKeys = groups.map((group) => group.key);
  const panelKeys = panels.map((panel) => panel.key);
  const contractReady = groupKeys.length === SETTING_GROUP_ORDER.length
    && panelKeys.length === SETTING_GROUP_ORDER.length
    && SETTING_GROUP_ORDER.every((key, index) => groupKeys[index] === key && panelKeys[index] === key);
  if (!contractReady) {
    root.append(renderStatePanel({
      phase: "partial",
      operation: copy("pages.settings.summary.operation"),
      problem: {
        message: copy("pages.settings.summary.missing"),
        recovery: copy("pages.settings.summary.recovery"),
      },
    }));
    return root;
  }
  const readonly = model.readonly || model.permissions?.can_manage === false;
  const views = panels.map((panel) => panelView(panel, readonly, handlers));
  const byKey = new Map(views.map((view) => [view.key, view]));
  const panelShells = panels.map((panel) => el("section", {
    id: `tavern-settings-panel-${panel.key}`, class: "tavern-setting-tabpanel", role: "tabpanel", tabindex: "0",
    "aria-labelledby": `tavern-settings-tab-${panel.key}`, "data-settings-panel-shell": panel.key, hidden: true,
  }));
  const shellByKey = new Map(panels.map((panel, index) => [panel.key, panelShells[index]]));
  let activeKey = "";
  const tabButtons = groups.map((group) => {
    const button = renderButton({ variant: "quiet", label: group.label || label(group), onActivate: () => activate(group.key, { push: true }) });
    button.id = `tavern-settings-tab-${group.key}`;
    button.setAttribute("id", button.id);
    button.setAttribute("role", "tab");
    button.setAttribute("data-settings-group-control", group.key);
    button.setAttribute("aria-controls", `tavern-settings-panel-${group.key}`);
    return button;
  });

  function activate(group, { focus = false, push = false } = {}) {
    if (!byKey.has(group)) return;
    activeKey = group;
    for (const button of tabButtons) {
      const selected = button.getAttribute("data-settings-group-control") === group;
      button.setAttribute("aria-selected", String(selected));
      button.setAttribute("tabindex", selected ? "0" : "-1");
      if (selected) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
      if (selected && focus) button.focus();
    }
    for (const view of views) {
      const shell = shellByKey.get(view.key);
      const selected = view.key === group;
      shell.replaceChildren(...(selected ? [view.node] : []));
      if (selected) shell.removeAttribute("hidden"); else shell.setAttribute("hidden", "");
    }
    if (push) handlers.updateLocation?.({ filters: { ...(handlers.navigation?.filters || {}), group } }, { replace: false });
  }

  const tablist = el("nav", { class: "tavern-settings-tabs tavern-settings-groups tavern-setting-index", role: "tablist", "aria-label": copy("pages.settings.renderSettings.message.4a48d745e3") }, tabButtons);
  tablist.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const current = tabButtons.findIndex((button) => button === event.target || button.contains(event.target));
    if (current < 0) return;
    const last = tabButtons.length - 1;
    const next = event.key === "Home" ? 0 : event.key === "End" ? last : event.key === "ArrowRight" ? (current + 1) % tabButtons.length : (current - 1 + tabButtons.length) % tabButtons.length;
    event.preventDefault();
    activate(tabButtons[next].getAttribute("data-settings-group-control"), { focus: true, push: true });
  });

  const reload = renderButton({
    variant: "secondary",
    label: copy("pages.settings.reload"),
    disabledReason: handlers.refresh ? "" : copy("pages.settings.reload_unavailable"),
    onActivate: () => handlers.refresh?.(),
  });
  root.dataset.draftState = "editor-dialog";
  root.append(
    el("header", { class: "tavern-settings-hero tavern-page-toolbar" }, [
      el("div", {}, [el("p", { class: "tavern-settings-kicker", text: copy("pages.settings.renderSettings.text.7f4a0f0636") }), el("h2", { text: model.title || copy("pages.settings.renderSettings.message.99879763ae") }), el("p", { text: model.summary || copy("pages.settings.renderSettings.message.5b4dbe09c1") })]),
      el("div", { class: "tavern-settings-hero-actions" }, [reload, renderStatusBadge({ state: readonly ? "readonly" : "ready", label: readonly ? copy("pages.settings.renderSettings.message.3b5ec3533b") : copy("pages.settings.renderSettings.message.2c4b644304") })]),
    ]),
    tablist,
    el("main", { class: "tavern-setting-panel-stack tavern-settings-workspace tavern-settings-panels" }, panelShells),
  );
  const requested = handlers.navigation?.filters?.group || groupsSection.selected_group || panels[0]?.key;
  activate(byKey.has(requested) ? requested : panels[0]?.key || "");
  return root;
}
