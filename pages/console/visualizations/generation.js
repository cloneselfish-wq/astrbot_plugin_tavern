import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { formatUtc8Minute } from "../components/time.js";

const STATE_PRESENTATION = Object.freeze({
  running: { tone: "warning", symbol: "◷", label: copy("pages.dashboard.publicState.message.dc9591e56d") },
  cancelling: { tone: "warning", symbol: "◷", label: copy("visualizations.generation.rc8.8db595a86a") },
  completed: { tone: "beneficial", symbol: "↑", label: copy("visualizations.generation.renderGeneration.message.f28461bb49") },
  cancelled: { tone: "neutral", symbol: "•", label: copy("visualizations.generation.rc8.a37778f17c") },
  failed: { tone: "harmful", symbol: "!", label: copy("pages.dashboard.actionableList.text.b7e3e715f1") },
  unknown: { tone: "unknown", symbol: "?", label: copy("components.primitives.renderFormField.message.cdea037991") },
});
const STAGE_TONES = new Set(["neutral", "beneficial", "warning", "harmful", "unknown"]);
const STAGE_SYMBOLS = new Set(["○", "●", "•", "▶", "◷", "◇", "✓", "↑", "↻", "×", "!", "?"]);

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

function durationLabel(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return copy("visualizations.generation.renderGeneration.message.d19cb801e2");
  return seconds < 1 ? `${Math.round(seconds * 1000)} ms` : `${seconds.toFixed(2)} s`;
}

function resultLabels() {
  return {
    completed: copy("visualizations.generation.renderGeneration.message.f28461bb49"),
    running: copy("visualizations.generation.renderGeneration.message.dc9591e56d"),
    repaired: copy("visualizations.generation.renderGeneration.message.7475293551"),
    fallback: copy("visualizations.generation.renderGeneration.message.9ef4188ec6"),
    failed: copy("visualizations.generation.renderGeneration.message.61bfa8da63"),
    cancelled: copy("visualizations.generation.rc8.a37778f17c"),
    unknown: copy("visualizations.generation.renderGeneration.message.cdea037991"),
  };
}

function stagePresentation(stage) {
  const label = typeof stage?.stage_label === "string" ? stage.stage_label.trim() : "";
  const tone = typeof stage?.stage_tone === "string" ? stage.stage_tone : "";
  const symbol = typeof stage?.stage_symbol === "string" ? stage.stage_symbol : "";
  const problem = Boolean(stage?.stage_problem)
    || !label
    || !STAGE_TONES.has(tone)
    || !STAGE_SYMBOLS.has(symbol);
  return problem
    ? {
      label: copy("visualizations.generation.rc8.f0fd1034dd"),
      tone: "unknown",
      symbol: "?",
      problem: true,
    }
    : { label, tone, symbol, problem: false };
}

function safeDescriptor(operation) {
  if (!operation?.can_cancel || !Array.isArray(operation.available_actions)) return null;
  return operation.available_actions.find((descriptor) =>
    descriptor?.intent === "operation.cancel.request"
    && descriptor.transportReady === true
    && Number.isInteger(descriptor.expected_revision)) || null;
}

function countdownTarget(reminder, now) {
  const timestamp = new Date(reminder?.next_reminder_at || "").valueOf();
  if (Number.isFinite(timestamp)) return timestamp;
  const seconds = Number(reminder?.next_reminder_in_seconds);
  return Number.isFinite(seconds) ? now + Math.max(0, seconds) * 1000 : null;
}

function renderReminder(reminder, countdowns, now, operationState) {
  if (!reminder || typeof reminder !== "object") return null;
  const section = node("section", { class: "tavern-generation-reminder" }, [
    node("h5", { text: copy("visualizations.generation.rc8.26644959a6") }),
  ]);
  const facts = node("dl");
  const rows = [
    [copy("visualizations.generation.rc8.58ec6db769"), copy("visualizations.generation.elapsed_minutes", { p0: Math.max(0, Number(reminder.elapsed_minutes) || 0) })],
    [copy("visualizations.generation.rc8.135da521a4"), reminder.source_label || copy("pages.live_session.rc8.bbfd71b211")],
    [copy("visualizations.generation.rc8.4ca4cfc250"), reminder.last_reminder_at ? formatUtc8Minute(reminder.last_reminder_at) : copy("visualizations.generation.rc8.4cd0cefbaa")],
  ];
  for (const [label, value] of rows) {
    facts.append(node("div", {}, [node("dt", { text: label }), node("dd", { text: value })]));
  }
  const active = ["running", "cancelling"].includes(operationState);
  const target = reminder.enabled && active ? countdownTarget(reminder, now) : null;
  if (target !== null) {
    const output = node("dd", { text: copy("visualizations.generation.rc8.3db3bfd88e") });
    output.dataset.countdownTarget = String(target);
    countdowns.push(output);
    facts.append(node("div", {}, [node("dt", { text: copy("visualizations.generation.rc8.22711efe1a") }), output]));
  } else {
    facts.append(node("div", {}, [
      node("dt", { text: copy("visualizations.generation.rc8.22711efe1a") }),
      node("dd", { text: !active ? copy("visualizations.generation.rc8.79cbd1c0fb") : reminder.enabled ? copy("visualizations.generation.rc8.9807834aec") : copy("visualizations.generation.rc8.4bce1d2f07") }),
    ]));
  }
  section.append(facts);
  return section;
}

function updateCountdowns(nodes, now = Date.now()) {
  for (const output of nodes) {
    const target = Number(output.dataset.countdownTarget);
    const seconds = Number.isFinite(target) ? Math.max(0, Math.ceil((target - now) / 1000)) : 0;
    output.textContent = seconds > 0 ? copy("visualizations.generation.countdown", { p0: seconds }) : copy("visualizations.generation.rc8.78b332ad79");
  }
}

export function renderGeneration(items = [], { onAction = null, now = Date.now() } = {}) {
  const section = node("section", {
    class: "tavern-generation-waterfall",
    dataset: { visualization: "generation-waterfall" },
  }, [node("h3", { text: copy("visualizations.generation.renderGeneration.message.73e1302e74") })]);
  const safeItems = Array.isArray(items) ? items : [];
  if (!safeItems.length) {
    section.append(node("p", { text: copy("visualizations.generation.renderGeneration.message.496571a9fe") }));
    return section;
  }
  const labels = resultLabels();
  const countdowns = [];
  const operations = [];
  for (const operation of safeItems) {
    const presentation = STATE_PRESENTATION[operation.state] || STATE_PRESENTATION.unknown;
    const block = node("details", {
      class: "tavern-generation-operation",
      dataset: { state: operation.state || "unknown", tone: presentation.tone },
    });
    const operationTitle = node("h4", {
      text: operation.label || copy("visualizations.generation.renderGeneration.message.5c2568ac77"),
    });
    const state = node("span", {
      class: "tavern-generation-state",
      dataset: { tone: presentation.tone },
    }, [
      node("b", { text: presentation.symbol, "aria-hidden": "true" }),
      node("span", { text: presentation.label }),
    ]);
    block.append(node("summary", {}, [operationTitle, state]));
    const body = node("div", { class: "tavern-generation-operation-body" });
    const operationState = node("p", {
      text: [
        operation.repair_used ? copy("visualizations.generation.renderGeneration.message.431999eb06") : "",
        operation.fallback_used ? copy("visualizations.generation.renderGeneration.message.749bee597a") : "",
      ].filter(Boolean).join(" · "),
    });
    if (operationState.textContent) body.append(operationState);
    const list = node("ol");
    const stages = Array.isArray(operation.stages) ? operation.stages : [];
    for (const stage of stages) {
      const stageView = stagePresentation(stage);
      const row = node("li", {
        dataset: {
          kind: "generation-stage",
          state: stage.result || "unknown",
          tone: stageView.tone,
        },
      });
      const marker = node("span", { class: "tavern-generation-marker", "aria-hidden": "true" });
      const body = node("div", {}, [
        node("strong", {}, [
          node("span", { text: `${stageView.symbol} `, "aria-hidden": "true" }),
          node("span", { text: stageView.label }),
        ]),
        node("p", {
          text: [
            labels[stage.result] || labels.unknown,
            stage.repair_used ? copy("visualizations.generation.renderGeneration.message.7475293551") : "",
            stage.fallback_used ? copy("visualizations.generation.renderGeneration.message.9ef4188ec6") : "",
          ].filter(Boolean).join(" · "),
        }),
      ]);
      if (stageView.problem) {
        body.append(node("p", {
          class: "tavern-generation-stage-problem",
          text: copy("visualizations.generation.rc8.aade867f37"),
        }));
      }
      row.append(marker, body, node("span", {
        class: "tavern-generation-duration",
        text: durationLabel(stage.duration_seconds),
      }));
      list.append(row);
    }
    if (!stages.length) list.append(node("li", { text: copy("visualizations.generation.renderGeneration.message.8d2cdf1431") }));
    body.append(list);
    const reminder = renderReminder(operation.reminder, countdowns, now, operation.state);
    if (reminder) body.append(reminder);
    const descriptor = safeDescriptor(operation);
    if (descriptor && typeof onAction === "function") {
      body.append(renderButton({
        variant: "danger",
        label: descriptor.label,
        intent: { id: descriptor.intent },
        onActivate: (_intent, event) => onAction(descriptor, event.currentTarget),
      }));
    }
    block.append(body);
    block.open = true;
    operations.push(block);
    section.append(block);
  }
  const mobileQuery = typeof globalThis.matchMedia === "function"
    ? globalThis.matchMedia("(max-width: 430px)")
    : null;
  const syncDisclosureMode = (event = mobileQuery) => {
    const mobile = Boolean(event?.matches);
    for (const operation of operations) {
      operation.open = !mobile;
    }
  };
  syncDisclosureMode();
  mobileQuery?.addEventListener?.("change", syncDisclosureMode);
  updateCountdowns(countdowns, now);
  const timer = countdowns.length
    ? globalThis.setInterval(() => {
      if (!section.isConnected) {
        globalThis.clearInterval(timer);
        return;
      }
      updateCountdowns(countdowns);
    }, 1000)
    : null;
  section.dispose = () => {
    if (timer !== null) globalThis.clearInterval(timer);
    mobileQuery?.removeEventListener?.("change", syncDisclosureMode);
  };
  return section;
}

export const renderGenerationWaterfall = renderGeneration;
