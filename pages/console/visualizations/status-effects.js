import { copy } from "../copy/catalog.js";
const TONES = new Set(["beneficial", "harmful", "neutral", "warning", "unknown"]);
const SYMBOLS = Object.freeze({
  up: "↑",
  down: "↓",
  dot: "•",
  warning: "!",
  question: "?",
  clock: "◷",
  shield: "◇",
});

function remainingLabel(item) {
  if (item.duration) return String(item.duration);
  const remaining = item.remaining;
  if (!remaining || typeof remaining !== "object" || remaining.value === undefined) return "";
  const unit = { rounds: copy("pages.dashboard.dashboardSessionCard.message.9fb1d89660"), seconds: copy("visualizations.status_effects.rc8.9dcdc2b289"), minutes: copy("visualizations.status_effects.rc8.bd957bc497") }[remaining.kind] || "";
  return `${remaining.value}${unit ? ` ${unit}` : ""}`;
}

export function statusItems(actor) {
  return Array.isArray(actor?.statuses) ? actor.statuses.filter((item) =>
    item && typeof item === "object" && String(item.label || "").trim()) : [];
}

export function renderStatusEffects(actor, uiProfile = {}, { detail = false } = {}) {
  const policy = uiProfile?.party?.statuses;
  if (!policy) return null;
  const all = statusItems(actor);
  const limit = detail ? all.length : Math.max(0, Math.min(4, Number(policy.max_compact) || 4));
  const items = all.slice(0, limit);
  if (!items.length) return null;
  const wrapper = document.createElement("section");
  wrapper.className = "tavern-status-effects";
  wrapper.setAttribute("aria-label", copy("visualizations.actor.rc8.853a55f3ee"));
  for (const item of items) {
    const tone = TONES.has(item.tone) ? item.tone : "unknown";
    const symbolKey = Object.hasOwn(SYMBOLS, item.symbol) ? item.symbol : "question";
    const effect = document.createElement("span");
    effect.className = "tavern-status-effect";
    effect.dataset.tone = tone;
    effect.dataset.symbol = symbolKey;
    effect.title = [item.summary, remainingLabel(item)].filter(Boolean).join(" · ");
    const symbol = document.createElement("b");
    symbol.setAttribute("aria-hidden", "true");
    symbol.textContent = SYMBOLS[symbolKey];
    const label = document.createElement("span");
    label.textContent = item.label;
    effect.append(symbol, label);
    const remaining = remainingLabel(item);
    if (remaining) {
      const duration = document.createElement("small");
      duration.textContent = remaining;
      effect.append(duration);
    }
    wrapper.append(effect);
  }
  return wrapper;
}
