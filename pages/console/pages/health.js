import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderCapabilityHub } from "../components/capability-hub.js";
import { renderMetricStrip } from "../components/cards.js";
import { renderStatusBadge } from "../components/status.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { formatUtc8Minute } from "../components/time.js";
import { el, label, pageRoot, rows, stateNotice, summary, value } from "./shared.js";

const NORMAL = new Set(["正常", "可用", "ready"]);
const HEALTH_CARD = { kind: "health" };

function stateToken(state) {
  const text = String(state || "");
  if (NORMAL.has(text)) return "ready";
  if (text.includes("恢复")) return "recovering";
  if (text.includes("不可用")) return "error";
  return "warning";
}

function healthKind(item) {
  return {
    pure_install: "pure-install",
    development: "development",
    manifest_missing: "manifest-missing",
    manifest_corrupt: "manifest-damaged",
    backup_missing: "no-backup",
    backup_corrupt: "backup-damaged",
    backup_ready: "backup-ready",
    service: "service",
  }[String(item.condition || "service")] || "service";
}

function healthCaseCopy(kind) {
  return {
    "pure-install": copy("pages.health.rc8.a6b3f1edf8"),
    development: copy("pages.health.rc8.917fd6c2b6"),
    "manifest-damaged": copy("pages.health.rc8.8157dd600c"),
    "manifest-missing": copy("pages.health.rc8.fc932d8850"),
    "no-backup": copy("pages.health.rc8.552592a0f7"),
    "backup-damaged": copy("pages.health.rc8.a62ec879a4"),
    "backup-ready": copy("pages.health.rc8.c2ff77d49f"),
  }[kind] || "";
}

function descriptorActions(item, handlers, variant = "secondary") {
  return rows(item.available_actions).filter((action)=>action?.transportReady===true&&action.intent&&Number.isInteger(action.expected_revision)).map((action) => renderButton({
    variant,
    label: action.label || copy("pages.health.actions.message.44fd226562"),
    intent: { id: action.intent },
    onActivate: (_intent, event) => handlers.actions?.execute({
      ...action,
      object_key: item.key,
      target_key: item.key,
      confirmation: {
        impact: action.description,
        unchanged: copy("pages.health.actions.message.fe55e07e4f"),
        automatic: copy("pages.health.actions.message.b56d27374d"),
        recovery: copy("pages.health.actions.recovery.94804e7574"),
        returnCheck: copy("pages.health.actions.message.1bd9ea90c3"),
        confirmLabel: action.label || copy("pages.health.actions.message.51e44d56ec"),
      },
    }, { opener: event.currentTarget }),
  }));
}

function serviceCard(item, handlers) {
  const state = item.state || item.status || copy("pages.health.renderHealth.message.cdea037991");
  const abnormal = !NORMAL.has(String(state));
  const kind = healthKind(item);
  return el("article", { ...HEALTH_CARD, class: `tavern-health-service ${abnormal ? "tavern-is-attention" : "tavern-is-normal"}`, "data-health-case": kind, "data-health-state": state }, [
    el("header", {}, [
      el("div", {}, [el("h3", { text: label(item, copy("pages.health.renderHealth.message.595254b183")) }), el("p", { text: item.summary || copy("pages.health.renderHealth.message.925471eb31") })]),
      renderStatusBadge({ state: stateToken(state), label: state }),
    ]),
    healthCaseCopy(kind) ? el("p", { class: "tavern-health-case-note", text: healthCaseCopy(kind) }) : null,
    abnormal ? el("dl", { class: "tavern-health-recovery-facts" }, [
      el("div", {}, [el("dt", { text: copy("pages.health.renderHealth.text.8a82deea73") }), el("dd", { text: item.reason || item.summary || copy("pages.health.renderHealth.message.c8246c71b4") })]),
      item.affected_scope ? el("div", {}, [el("dt", { text: copy("pages.health.renderHealth.affected_scope") }), el("dd", { text: item.affected_scope })]) : null,
      el("div", {}, [el("dt", { text: copy("pages.health.renderHealth.text.4178afb987") }), el("dd", { text: item.automatic_action || copy("pages.health.renderHealth.message.40a1d6ffa2") })]),
      el("div", {}, [el("dt", { text: copy("pages.health.renderHealth.text.2425e9e5ea") }), el("dd", { text: item.next_step || copy("pages.health.renderHealth.message.c990f4471f") })]),
    ]) : null,
    el("footer", {}, [
      item.last_checked_at || item.updated_at ? el("time", { text: `${copy("pages.health.renderHealth.last_checked")} ${formatUtc8Minute(item.last_checked_at || item.updated_at)}` }) : null,
      ...descriptorActions(item, handlers),
    ]),
  ]);
}

function attentionHero(item, handlers) {
  if (!item) return null;
  const actions = descriptorActions(item, handlers, "primary");
  return el("section", { class: "tavern-health-priority", "data-health-case": healthKind(item) }, [
    el("header", {}, [
      el("div", {}, [el("span", { text: "RECOVERY REQUIRED" }), el("h2", { text: label(item, copy("pages.health.rc8.34670f8e0f")) }), el("p", { text: item.reason || item.summary || copy("pages.health.renderHealth.message.c8246c71b4") })]),
      renderStatusBadge({ state: stateToken(item.state || item.status), label: item.state || item.status || copy("pages.health.renderHealth.message.cdea037991") }),
    ]),
    healthCaseCopy(healthKind(item)) ? el("p", { class: "tavern-health-case-note", text: healthCaseCopy(healthKind(item)) }) : null,
    el("div", { class: "tavern-health-next-grid" }, [
      el("div", {}, [el("small", { text: copy("pages.health.rc8.2fba1a554d") }), el("strong", { text: item.automatic_action || copy("pages.health.renderHealth.message.40a1d6ffa2") })]),
      el("div", {}, [el("small", { text: copy("pages.audit.delivery.next_step") }), el("strong", { text: item.next_step || copy("pages.health.renderHealth.message.c990f4471f") })]),
    ]),
    actions.length ? el("div", { class: "tavern-health-priority-actions" }, actions) : null,
  ]);
}

function normalServiceGrid(items, handlers) {
  if (!items.length) return null;
  return el("section", { class: "tavern-health-normal-list", "aria-label": copy("pages.health.rc8.50f3ec72a8") }, items.map((item) => serviceCard(item, handlers)));
}

function latencyPanel(samples) {
  if (!samples.length) return el("section", { class: "tavern-health-proof" }, [el("h2", { text: copy("pages.health.latency.heading") }), renderStatePanel({ phase: "empty", emptyCopy: copy("pages.health.rc8.d9c83db05b") })]);
  const max = Math.max(...samples.map((sample) => Number(sample.value) || 0), 1);
  return el("section", { class: "tavern-health-proof" }, [
    el("h2", { text: copy("pages.health.latency.heading") }),
    el("ol", { class: "tavern-health-latency-list" }, samples.map((sample) => el("li", {}, [
      el("span", { text: label(sample, copy("pages.health.latency.sample")) }),
      el("strong", { text: copy("pages.health.latency.value", { p0: sample.value ?? copy("pages.health.latency.unknown") }) }),
      el("meter", { min: 0, max, value: Number(sample.value) || 0 }),
      sample.updated_at ? el("time", { text: formatUtc8Minute(sample.updated_at) }) : null,
    ]))),
  ]);
}

function incidentPanel(items) {
  if (!items.length) return el("section", { class: "tavern-health-proof" }, [el("h2", { text: copy("pages.health.incidents.heading") }), renderStatePanel({ phase: "empty", emptyCopy: copy("pages.health.rc8.08eb04d321") })]);
  return el("section", { class: "tavern-health-proof" }, [
    el("h2", { text: copy("pages.health.incidents.heading") }),
    el("ol", { class: "tavern-health-incident-list" }, items.map((item) => el("li", {}, [
      el("header", {}, [el("strong", { text: label(item, copy("pages.health.incidents.unknown")) }), renderStatusBadge({ state: stateToken(item.state), label: item.state || copy("pages.health.renderHealth.message.cdea037991") })]),
      el("p", { text: summary(item, copy("pages.health.incidents.summary_unknown")) }),
      item.created_at ? el("time", { text: formatUtc8Minute(item.created_at) }) : null,
    ]))),
  ]);
}

function capabilityRows(value) {
  if (Array.isArray(value)) return value;
  return value && typeof value === "object" ? [value] : [];
}

function explicitCapabilityRows(topology, diagnostics, key) {
  const underscored = key.replaceAll("-", "_");
  return capabilityRows(topology?.[key] ?? topology?.[underscored] ?? diagnostics?.[key] ?? diagnostics?.[underscored]);
}

function capabilityFacts(items) {
  const attention = items.filter((item) => !NORMAL.has(String(item?.state || item?.status))).length;
  const actions = items.flatMap((item) => rows(item?.available_actions)).filter(
    (action) => action?.transportReady === true && action.intent && Number.isInteger(action.expected_revision),
  ).length;
  return [
    { label: copy("pages.health.capability.fact.checked"), value: items.length },
    { label: copy("pages.health.capability.fact.attention"), value: attention },
    { label: copy("pages.health.capability.fact.actions"), value: actions },
  ];
}

function healthCapabilityPanels(topology, recovery, diagnostics) {
  const maintenance = capabilityRows(recovery);
  const definitions = [
    ["providers", copy("pages.health.capability.providers.label"), explicitCapabilityRows(topology, diagnostics, "providers"), "health"],
    ["panel-status", copy("pages.health.capability.panel_status.label"), explicitCapabilityRows(topology, diagnostics, "panel-status"), "settings"],
    ["extensions", copy("pages.health.capability.extensions.label"), explicitCapabilityRows(topology, diagnostics, "extensions"), "modules"],
    ["maintenance", copy("pages.health.capability.maintenance.label"), maintenance, "health"],
  ];
  return definitions.map(([key, panelLabel, items, workspace]) => {
    const attention = items.filter((item) => !NORMAL.has(String(item?.state || item?.status))).length;
    const reported = items.length > 0;
    return {
      key,
      group: "system",
      label: panelLabel,
      summary: key === "maintenance" ? copy("pages.health.capability.maintenance.summary") : copy("pages.health.capability.default.summary"),
      state: reported
        ? attention ? copy("pages.health.capability.state.attention") : copy("pages.health.capability.state.normal")
        : copy("pages.health.capability.state.unreported"),
      stateToken: reported ? attention ? "warning" : "ready" : "readonly",
      workspace,
      facts: reported ? capabilityFacts(items) : [],
      boundary: reported
        ? items.map((item) => summary(item, label(item))).filter(Boolean).slice(0, 3).join("；")
        : copy("components.capability_hub.not_available"),
    };
  });
}

export function renderHealth(model, handlers = {}) {
  const root = pageRoot(model, "tavern-health");
  root.setAttribute("class", `${root.className} tavern-health-page`);
  root.append(stateNotice(model, copy("pages.health.renderHealth.message.b86fbde0bb")));
  const services=rows(value(model,"services"));
  const additional = rows(value(model, "additional_services"));
  const topology = value(model, "topology") || {};
  const recovery = value(model, "recovery");
  const diagnostics = value(model, "diagnostics") || {};
  const attention = services.filter((item) => !NORMAL.has(String(item.state || item.status)));
  const normal = services.filter((item) => NORMAL.has(String(item.state || item.status)));
  const toolbar = el("header", { class: "tavern-page-toolbar tavern-health-toolbar" }, [
      el("div", { class: "tavern-page-toolbar-copy" }, [el("h2", { text: attention.length ? copy("pages.health.renderHealth.message.ed4544a56b", { p0: attention.length }) : copy("pages.health.renderHealth.message.7723ff003c") }), el("p", { text: model.summary || copy("pages.health.renderHealth.message.d79b6ac028") })]),
      renderButton({ variant: "secondary", label: copy("pages.health.renderHealth.label.c25fb86b1e"), onActivate: () => handlers.refresh?.() }),
    ]);
  const metrics = renderMetricStrip({ workspace: "health", metrics: rows(value(model, "metrics")) });
  const urgent = attentionHero(attention[0], handlers);
  const attentionBlock = urgent || renderStatePanel({
    phase: services.length || additional.length ? "ready" : "empty",
    emptyCopy: copy("pages.health.renderHealth.text.02c17d639d"),
  });
  const healthGrid = el("div", { class: "tavern-health-grid" }, [
    attention.length > 1 ? el("section", { class: "tavern-health-priority-grid", "aria-label": copy("pages.health.renderHealth.message.c5b8203200") }, attention.slice(1).map((item) => serviceCard(item, handlers))) : null,
    normalServiceGrid([...normal, ...additional], handlers),
    el("section", { class: "tavern-health-capability-slot" }, [renderCapabilityHub({
      panels: healthCapabilityPanels(topology, recovery, diagnostics),
      group: "system",
      title: copy("pages.health.capability.title"),
      summary: diagnostics.summary || copy("pages.health.capability.summary"),
      handlers,
    })]),
    el("div", { class: "tavern-health-proof-grid" }, [latencyPanel(rows(value(model, "latency"))), incidentPanel(rows(value(model, "incidents")))]),
  ]);
  root.append(toolbar, metrics, attentionBlock, healthGrid);
  return root;
}
