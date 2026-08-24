import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderFilterBar } from "../components/filters.js";
import { renderPagination } from "../components/pagination.js";
import { renderStatusBadge } from "../components/status.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { formatUtc8Minute } from "../components/time.js";
import { openDetail } from "../dialogs/detail-dialog.js";
import { openEditor } from "../dialogs/editor-dialog.js";
import { el, label, pageRoot, rows, stateNotice, summary, value } from "./shared.js";

const DELIVERY_STATE_TOKENS = Object.freeze({
  pending: "waiting",
  leased: "running",
  sending: "running",
  partially_sent: "warning",
  confirmed: "ready",
  delivered: "ready",
  failed: "error",
  retry_wait: "recovering",
  permanently_failed: "error",
  cancelled: "readonly",
});
const DELIVERY_SECTION = Object.freeze({ id: "delivery" });

function fields(model) {
  const byName = Object.fromEntries(rows(model.filters).map((field) => [field.name, field]));
  const select = (name, title) => ({ name, label: title, type: "select", default: "", options: [{ value: "", label: copy("pages.audit.fields.label.8b65a8100d", { p0: title }) }, ...rows(byName[name]?.options)] });
  return [
    { name: "q", label: copy("pages.audit.fields.label.4641451915"), type: "search", default: "", hint: copy("pages.audit.fields.message.1779cada12") },
    select("status", copy("pages.audit.fields.message.6320b4a872")),
    select("object", copy("pages.audit.fields.message.53f92c0639")),
    select("actor", copy("pages.audit.fields.message.b48ff04a7a")),
    select("action", copy("pages.audit.fields.message.be37d84119")),
    select("time", copy("pages.audit.fields.message.8b6ff49851")),
  ].filter((field) => field.type !== "select" || field.options.length > 1);
}

function apply(handlers, filters, { resetCursor = true } = {}) {
  handlers.updateLocation?.({ filters: { ...filters, ...(resetCursor ? { cursor: "" } : {}) } }, { replace: false });
  return handlers.refresh?.();
}

function filterDisclosure(form, values, filterFields) {
  const defaults = Object.fromEntries(filterFields.map((field) => [field.name, field.default]));
  const names = new Set(filterFields.map((field) => field.name));
  const active = Object.entries(values || {}).filter(
    ([name, entry]) => names.has(name) && name !== "cursor"
      && String(entry ?? "") !== String(defaults[name] ?? ""),
  ).length;
  const compact = globalThis.matchMedia?.("(max-width: 760px)")?.matches === true;
  return el("details", { class: "tavern-mobile-filter-disclosure", open: !compact || active > 0 }, [
    el("summary", { text: active ? copy("components.filters.mobile_summary_active", { p0: active }) : copy("components.filters.mobile_summary") }),
    form,
  ]);
}

function actionButton(item, descriptor, handlers) {
  if (!descriptor?.intent || descriptor.transportReady !== true) return null;
  if (!Number.isInteger(descriptor.expected_revision) || typeof handlers.actions?.execute !== "function") {
    return renderButton({
      variant: "secondary",
      label: descriptor.label || copy("pages.audit.deliveries.message.8804c49119"),
      disabledReason: item.action_boundary || copy("pages.audit.delivery.disabled_reason"),
    });
  }
  const action = { ...descriptor, object_key: item.key, target_key: item.key };
  return renderButton({
    variant: "secondary",
    label: descriptor.label || copy("pages.audit.deliveries.message.8804c49119"),
    intent: { id: descriptor.intent },
    onActivate: (_intent, event) => {
      if (rows(action.fields).length) {
        openEditor(handlers.dialogs, {
          objectKey: item.key,
          revision: action.expected_revision,
          fields: rows(action.fields),
          opener: event.currentTarget,
          title: action.label,
          preview: () => ({ summary: action.description || copy("pages.audit.delivery.next_step") }),
          submit: ({ draft, idempotencyKey }) => handlers.actions.execute(action, { opener: event.currentTarget, input: draft, idempotencyKey }),
        });
      } else handlers.actions.execute(action, { opener: event.currentTarget });
    },
  });
}

function deliveryActions(item, handlers) {
  const descriptors = rows(item.available_actions);
  const actions = descriptors.map((descriptor) => actionButton(item, descriptor, handlers)).filter(Boolean);
  if (actions.length) return actions;
  return [el("span", { class: "tavern-audit-action-boundary", text: copy("pages.audit.delivery.disabled"), title: item.action_boundary || copy("pages.audit.delivery.disabled_reason") })];
}

function deliveryDetail(item) {
  const parts = rows(item.parts);
  return el("section", { class: "tavern-audit-delivery-detail" }, [
    parts.length
      ? el("ol", {}, parts.map((part) => el("li", {}, [
        el("strong", { text: part.label || copy("visualizations.delivery.renderDelivery.message.15e23afda7") }),
        el("span", { text: `${part.state || copy("pages.audit.timeline.message.cdea037991")} · ${copy("visualizations.delivery.partRow.message.77f89963b8", { p0: part.attempts ?? 0 })}` }),
      ])))
      : el("p", { text: item.action_boundary || copy("pages.audit.delivery.disabled_reason") }),
    el("dl", { class: "tavern-audit-delivery-meta" }, [
      el("div", {}, [el("dt", { text: copy("pages.audit.delivery.parts") }), el("dd", { text: item.parts_summary || copy("pages.audit.deliveries.emptyCopy.bd33688413") })]),
      el("div", {}, [el("dt", { text: copy("pages.audit.delivery.attempts") }), el("dd", { text: item.attempts ?? 0 })]),
      el("div", {}, [el("dt", { text: copy("pages.audit.delivery.next_step") }), el("dd", { text: item.next_step || copy("pages.health.renderHealth.message.c990f4471f") })]),
      item.failure_reason ? el("div", {}, [el("dt", { text: copy("pages.author_jobs.jobCard.failure") }), el("dd", { text: item.failure_reason })]) : null,
    ]),
  ]);
}

function openDeliveryDetail(item, handlers, opener) {
  if (!handlers.dialogs?.openDialog) return;
  openDetail(handlers.dialogs, {
    objectKey: item.key,
    opener,
    title: label(item, copy("pages.audit.deliveries.label.3252c40c63")),
    tabs: [{ id: "parts", label: copy("pages.audit.delivery.parts") }],
    activeTab: "parts",
    lazyPanelLoader: () => deliveryDetail(item),
  });
}

function deliveryState(item) {
  const token = String(item.public_state || item.state_token || item.status || "").trim().toLowerCase();
  if (DELIVERY_STATE_TOKENS[token]) return DELIVERY_STATE_TOKENS[token];
  if (item.failure_reason) return "error";
  const delivered = Number(item.delivered_parts);
  const remaining = Number(item.remaining_parts);
  if (item.delivered_parts != null && item.remaining_parts != null && Number.isFinite(delivered) && Number.isFinite(remaining)) {
    if (remaining === 0) return "ready";
    if (delivered > 0) return "warning";
  }
  return "waiting";
}

function deliveryRow(item, handlers) {
  const state = deliveryState(item);
  return el("article", { class: "tavern-audit-delivery-row", "data-delivery-state": item.state || "unknown" }, [
    el("div", { class: "tavern-audit-delivery-main" }, [
      el("strong", { text: label(item, copy("pages.audit.deliveries.label.3252c40c63")) }),
      el("span", { text: summary(item, item.parts_summary || copy("pages.audit.deliveries.emptyCopy.bd33688413")) }),
    ]),
    el("div", { class: "tavern-audit-delivery-state" }, [
      renderStatusBadge({ state, label: item.state || copy("pages.audit.timeline.message.cdea037991") }),
      el("small", { text: copy("visualizations.delivery.partRow.message.77f89963b8", { p0: item.attempts ?? 0 }) }),
    ]),
    el("div", { class: "tavern-audit-delivery-actions" }, [
      renderButton({ variant: "secondary", label: copy("pages.audit.delivery.parts"), onActivate: (_intent, event) => openDeliveryDetail(item, handlers, event.currentTarget) }),
      ...deliveryActions(item, handlers),
    ]),
    item.updated_at ? el("time", { text: formatUtc8Minute(item.updated_at) }) : null,
  ]);
}






function deliveryPanel(items, handlers) {
  const ledger = el("div", { class: "tavern-audit-delivery-ledger" });
  const panel = el("section", { class: "tavern-audit-ledger-panel", "aria-labelledby": "tavern-audit-delivery-title" }, [
    el("header", { class: "tavern-audit-panel-head" }, [
      el("div", {}, [
        el("h3", { id: "tavern-audit-delivery-title", text: copy("pages.audit.module.label.22c404d449") }),
        el("p", { text: copy("pages.audit.renderAudit.message.c9497d923c") }),
      ]),
      renderStatusBadge({ state: items.length ? "ready" : "readonly", label: String(items.length) }),
    ]),
    ledger,
  ]);
  if (items.length) ledger.append(...items.map((item) => deliveryRow(item, handlers)));
  else ledger.append(renderStatePanel({ phase: "empty", emptyCopy: copy("pages.audit.deliveries.emptyCopy.bd33688413") }));
  return panel;
}

function auditRecordDetail(item) {
  const facts = [
    [copy("pages.audit.fields.message.8b6ff49851"), item.updated_at ? formatUtc8Minute(item.updated_at) : copy("pages.memories.displayRows.message.4bdc19b47d")],
    [copy("pages.audit.fields.message.be37d84119"), label(item, copy("pages.audit.timeline.message.ab5ec33be4"))],
    [copy("pages.audit.fields.message.53f92c0639"), item.object_label || copy("pages.audit.timeline.object_unknown")],
    [copy("pages.audit.fields.message.b48ff04a7a"), item.actor_label || copy("pages.audit.timeline.actor_unknown")],
    [copy("pages.audit.fields.message.6320b4a872"), item.state || copy("pages.audit.timeline.message.cdea037991")],
    [copy("pages.audit.renderAudit.message.3ae6638817"), summary(item, copy("pages.audit.timeline.message.e2a2accf29"))],
  ];
  return el("dl", { class: "tavern-audit-record-detail" }, facts.map(([title, content]) => el("div", {}, [
    el("dt", { text: title }),
    el("dd", { text: content }),
  ])));
}

function openAuditRecordDetail(item, handlers, opener) {
  if (!item || !handlers.dialogs?.openDialog) return;
  openDetail(handlers.dialogs, {
    objectKey: "audit-record",
    opener,
    title: copy("pages.audit.timeline.message.863c1e10ad"),
    tabs: [{ id: "summary", label: copy("pages.audit.renderAudit.message.3ae6638817") }],
    activeTab: "summary",
    specialization: "audit-record",
    summaryFacts: [
      { label: copy("pages.audit.fields.message.be37d84119"), value: label(item, copy("pages.audit.timeline.message.ab5ec33be4")) },
      { label: copy("pages.audit.fields.message.6320b4a872"), value: item.state || copy("pages.audit.timeline.message.cdea037991") },
    ],
    lazyPanelLoader: () => auditRecordDetail(item),
  });
}

function auditPanel(items, handlers) {
  let selected = items[0] || null;
  const selectors = [];
  const selectRecord = (item) => {
    selected = item;
    for (const entry of selectors) {
      const active = entry.item === selected;
      entry.button.setAttribute("aria-pressed", String(active));
      entry.row.dataset.selected = String(active);
    }
  };
  const records = items.length
    ? (() => {
      const body = el("tbody");
      for (const item of items) {
        const select = el("button", {
          type: "button",
          class: "tavern-audit-record-select",
          "aria-pressed": "false",
          onClick: () => selectRecord(item),
        }, [
          el("strong", { text: label(item, copy("pages.audit.timeline.message.ab5ec33be4")) }),
          el("small", { text: copy("pages.audit.timeline.context", { p0: item.object_label || copy("pages.audit.timeline.object_unknown"), p1: item.actor_label || copy("pages.audit.timeline.actor_unknown") }) }),
        ]);
        const row = el("tr", { "data-selected": "false" }, [
          el("td", { text: item.updated_at ? formatUtc8Minute(item.updated_at) : copy("pages.memories.displayRows.message.4bdc19b47d") }),
          el("td", {}, [select]),
          el("td", { text: summary(item, copy("pages.audit.timeline.message.e2a2accf29")) }),
          el("td", {}, [renderStatusBadge({ state: "ready", label: item.state || copy("pages.audit.timeline.message.cdea037991") })]),
        ]);
        selectors.push({ item, button: select, row });
        body.append(row);
      }
      selectRecord(selected);
      return el("div", { class: "tavern-audit-table-scroll" }, [el("table", { class: "tavern-audit-compact-table" }, [
        el("thead", {}, [el("tr", {}, [
          el("th", { text: copy("pages.audit.fields.message.8b6ff49851") }),
          el("th", { text: copy("pages.audit.fields.message.be37d84119") }),
          el("th", { text: copy("pages.audit.renderAudit.message.3ae6638817") }),
          el("th", { text: copy("pages.audit.fields.message.6320b4a872") }),
        ])]),
        body,
      ])]);
    })()
    : renderStatePanel({ phase: "empty", emptyCopy: copy("pages.audit.timeline.text.99f1d8e678") });
  return el("section", { class: "tavern-audit-ledger-panel", "aria-labelledby": "tavern-audit-records-title" }, [
    el("header", { class: "tavern-audit-panel-head" }, [
      el("div", {}, [
        el("h3", { id: "tavern-audit-records-title", text: copy("pages.audit.module.label.863c1e10ad") }),
        el("p", { text: copy("pages.audit.renderAudit.message.3ae6638817") }),
      ]),
      renderStatusBadge({ state: items.length ? "ready" : "readonly", label: String(items.length) }),
    ]),
    el("div", { class: "tavern-audit-records" }, [
      records,
      items.length ? el("footer", { class: "tavern-audit-record-actions" }, [
        renderButton({
          variant: "secondary",
          label: copy("components.capability_hub.open_details"),
          disabledReason: handlers.dialogs?.openDialog ? "" : copy("components.capability_hub.dialog_unavailable"),
          onActivate: (_intent, event) => openAuditRecordDetail(selected, handlers, event.currentTarget),
        }),
      ]) : null,
    ]),
  ]);
}

export function renderAudit(model, handlers = {}) {
  const root = pageRoot(model, "tavern-audit");
  root.setAttribute("class", `${root.className} tavern-audit-page`);
  root.append(stateNotice(model, copy("pages.audit.renderAudit.message.abeec21bf1")));
  const filterValues = handlers.navigation?.filters || {};
  const filterFields = fields(model);
  const deliveries = rows(value(model, DELIVERY_SECTION.id));
  const auditRecords = rows(value(model, "audit"));
  const countState = (states) => deliveries.filter((item) => states.includes(deliveryState(item))).length;
  const density = [
    [copy("pages.audit.delivery.state.delivered"), countState(["ready"]), copy("pages.audit.renderAudit.message.c9497d923c")],
    [copy("pages.audit.delivery.state.waiting"), countState(["waiting"]), copy("pages.audit.renderAudit.message.c9497d923c")],
    [copy("pages.audit.delivery.state.recovering"), countState(["running", "recovering", "warning"]), copy("pages.audit.renderAudit.message.c9497d923c")],
    [copy("pages.audit.delivery.state.manual"), countState(["error", "readonly"]), copy("pages.audit.delivery.next_step")],
    [copy("pages.audit.module.label.863c1e10ad"), auditRecords.length, copy("pages.audit.renderAudit.message.3ae6638817")],
  ];
  root.append(
    el("header", { class: "tavern-page-toolbar tavern-audit-toolbar" }, [
      el("div", { class: "tavern-page-toolbar-copy" }, [el("h2", { text: copy("pages.audit.toolbar.title") }), el("p", { text: copy("pages.audit.renderAudit.message.e6ecc89674") })]),
      el("div", { class: "tavern-audit-toolbar-controls" }, [
        filterDisclosure(renderFilterBar({ workspace: "audit", fields: filterFields, values: filterValues, onApply: (next) => apply(handlers, next), onClear: () => apply(handlers, {}) }), filterValues, filterFields),
        renderButton({ variant: "secondary", label: copy("pages.audit.renderAudit.label.621e17c543"), onActivate: () => handlers.refresh?.() }),
      ]),
    ]),
    el("section", { class: "tavern-density-strip tavern-audit-density" }, density.map(([title, count, detail]) => el("article", { class: "tavern-density-stat" }, [el("small", { text: title }), el("strong", { text: count }), el("span", { text: detail })]))),
    el("div", { class: "tavern-audit-page-split" }, [
      deliveryPanel(rows(value(model, DELIVERY_SECTION.id)), handlers),
      auditPanel(rows(value(model, "audit")), handlers),
    ]),
  );
  if (model.pagination) root.append(renderPagination({ workspace: "audit", ...model.pagination, onPage: (cursor) => apply(handlers, { ...filterValues, cursor }, { resetCursor: false }) }));
  return root;
}
