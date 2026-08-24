import { copy } from "../copy/catalog.js";
import { renderButton } from "./buttons.js";
import { createElement } from "./dom.js";

export const CANONICAL_STATE_FAMILIES = Object.freeze([
  "ready", "empty", "loading", "refreshing", "stale", "partial",
  "readonly", "permission", "conflict", "error", "rate_limited",
  "unavailable", "timeout", "disconnect",
]);

const CANONICAL_STATES = new Set(CANONICAL_STATE_FAMILIES);
const HTTP_STATE_FAMILIES = Object.freeze({
  401: "permission",
  403: "permission",
  409: "conflict",
  429: "rate_limited",
  500: "error",
  503: "unavailable",
  504: "timeout",
});
const STATE_MARKS = Object.freeze({
  ready: "✓",
  empty: "○",
  loading: "…",
  refreshing: "↻",
  stale: "◷",
  partial: "◐",
  readonly: "◇",
  permission: "⊘",
  conflict: "⇄",
  error: "!",
  rate_limited: "⌛",
  unavailable: "×",
  timeout: "⌚",
  disconnect: "⤫",
});
const DETAIL_LABELS = Object.freeze({
  operation: copy("components.empty_problem.detail.operation"),
  reason: copy("components.empty_problem.detail.reason"),
  automatic: copy("components.empty_problem.detail.automatic"),
  "next-step": copy("components.empty_problem.detail.next_step"),
  "last-good": copy("components.empty_problem.detail.last_good"),
});
const PROBLEM_DEFAULTS = Object.freeze({
  reason: copy("components.empty_problem.default.reason"),
  automatic: copy("components.empty_problem.default.automatic"),
  nextStep: copy("components.empty_problem.default.next_step"),
});

function problemStatus(problem) {
  const status = Number(problem?.status);
  return Number.isInteger(status) ? status : 0;
}

export function canonicalStateFamily(phase = "error", problem = null) {
  const requested = String(phase || "error").trim().toLowerCase().replaceAll("-", "_");
  const status = problemStatus(problem) || Number(requested) || 0;
  const mapped = HTTP_STATE_FAMILIES[status];
  const code = `${problem?.kind || ""} ${problem?.code || ""}`.toLowerCase();
  if (/disconnect|connection[_ -]?lost/.test(code) || requested === "disconnect") return "disconnect";
  if (/timeout/.test(code) || requested === "timeout") return "timeout";
  if (/rate[_ -]?limit/.test(code) || requested === "rate_limited") return "rate_limited";
  if (/unavailable|network/.test(code) || requested === "unavailable") return "unavailable";
  if (mapped && (!CANONICAL_STATES.has(requested) || ["error", "permission", "conflict"].includes(requested))) return mapped;
  return CANONICAL_STATES.has(requested) ? requested : mapped || "error";
}

export function stateFingerprint(phase, problem = null) {
  const family = canonicalStateFamily(phase, problem);
  const status = problemStatus(problem) || Number(phase) || 0;
  if (status === 401) return "unauthenticated";
  if (status === 403) return "forbidden";
  if (status === 409) return "revision-conflict";
  if (status === 429) return "retry-after";
  if (status === 500) return "server-error";
  if (status === 503) return "service-unavailable";
  if (family === "timeout") return "request-timeout";
  if (family === "disconnect") return "transport-disconnected";
  return `state-${family.replaceAll("_", "-")}`;
}

function stateTitle(family, operation, problem, emptyCopy) {
  const titles = {
    ready: problem?.message || operation,
    loading: copy("components.primitives.renderStatePanel.message.86b926e48b", { p0: operation }),
    refreshing: copy("components.primitives.renderStatePanel.message.d0f08f8965", { p0: operation }),
    empty: emptyCopy,
    error: copy("components.primitives.renderStatePanel.message.942be111a0", { p0: operation }),
    stale: copy("components.primitives.renderStatePanel.message.e40c8a0a26"),
    partial: copy("components.primitives.renderStatePanel.message.637d73eb17"),
    readonly: copy("components.primitives.renderStatePanel.message.f745f93b7c"),
    permission: copy("components.primitives.renderStatePanel.message.2f9305e988"),
    conflict: copy("components.primitives.renderStatePanel.message.d31ace3c8a"),
    rate_limited: problem?.message || copy("client.error.rate_limited"),
    unavailable: problem?.message || copy("client.error.server"),
    timeout: problem?.message || copy("client.error.timeout", { operation }),
    disconnect: problem?.message || copy("client.error.disconnect"),
  };
  return titles[family];
}

function detail(kind, value) {
  const normalized = String(value || "").trim();
  return normalized
    ? createElement("p", {
      class: "tavern-state-detail",
      "data-detail-kind": kind,
      text: `${DETAIL_LABELS[kind] || kind}：${normalized}`,
    })
    : null;
}

export function renderStatePanel({
  phase = "empty",
  operation = copy("components.primitives.renderStatePanel.message.0e625b1be6"),
  problem = null,
  lastGood = null,
  retryAction = null,
  emptyCopy = copy("components.primitives.renderStatePanel.message.4d3b8e2a87"),
} = {}) {
  const hasProblem = Boolean(problem && typeof problem === "object" && !Array.isArray(problem));
  const safeOperation = String(operation || copy("components.primitives.renderStatePanel.message.0e625b1be6")).trim();
  const safePhase = canonicalStateFamily(phase, problem);
  const fingerprint = stateFingerprint(phase, problem);
  const title = stateTitle(safePhase, safeOperation, problem, emptyCopy);
  const alert = [
    "error", "permission", "conflict", "rate_limited", "unavailable",
    "timeout", "disconnect",
  ].includes(safePhase);
  const heading = createElement("div", { class: "tavern-state-heading" }, [
    createElement("span", {
      class: "tavern-state-mark",
      "data-state-mark": safePhase,
      "aria-hidden": "true",
      text: STATE_MARKS[safePhase],
    }),
    createElement("div", { class: "tavern-state-copy" }, [
      createElement("span", {
        class: "tavern-state-operation",
        "data-detail-kind": hasProblem ? "operation" : undefined,
        text: hasProblem
          ? `${DETAIL_LABELS.operation}：${safeOperation}`
          : safeOperation,
      }),
      createElement("h2", { text: title }),
    ]),
  ]);
  const panel = createElement("section", {
    class: "tavern-state-panel",
    "data-phase": safePhase,
    "data-state-fingerprint": fingerprint,
    "data-testid": `tavern-state-${safePhase}`,
    role: alert ? "alert" : "status",
    "aria-live": alert ? "assertive" : "polite",
    "aria-atomic": alert ? "true" : "false",
  }, [heading]);
  const details = hasProblem ? [
    detail("reason", problem?.reason || problem?.message || PROBLEM_DEFAULTS.reason),
    detail("automatic", problem?.automatic || problem?.automatic_action || PROBLEM_DEFAULTS.automatic),
    detail("next-step", problem?.next_step || problem?.recovery || PROBLEM_DEFAULTS.nextStep),
  ] : [];
  if (lastGood && ["refreshing", "stale", "partial"].includes(safePhase)) {
    details.push(detail("last-good", copy("components.primitives.renderStatePanel.text.30622e6ec7")));
  }
  if (details.length) {
    panel.append(createElement("div", { class: "tavern-state-details" }, details));
  }
  if (retryAction) {
    panel.append(createElement("div", { class: "tavern-state-actions" }, [renderButton({
      variant: "secondary",
      label: copy("components.primitives.renderStatePanel.label.b8784c8dd5"),
      onActivate: retryAction,
    })]));
  }
  return panel;
}
