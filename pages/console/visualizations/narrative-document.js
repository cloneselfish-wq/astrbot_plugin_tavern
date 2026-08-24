import { copy } from "../copy/catalog.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { formatUtc8Minute } from "../components/time.js";

export const NARRATIVE_DOCUMENT_SCHEMA = "tavern-narrative-document/1.0.0";
export const NARRATIVE_BLOCK_KINDS = Object.freeze([
  "narration",
  "action",
  "dialogue",
  "reaction",
  "transition",
  "reveal",
  "system_note",
]);

const KIND_SET = new Set(NARRATIVE_BLOCK_KINDS);
const PUBLIC_VISIBILITIES = new Set(["", "public"]);
const INITIAL_BLOCKS = 6;
const TEXT = Object.freeze({
  continue: copy("visualizations.narrative_document.rc8.4ec2a77b1a"),
  collapse: copy("visualizations.narrative_document.rc8.8cacd71189"),
  copy: copy("visualizations.narrative_document.rc8.8fa474a488"),
  copied: copy("visualizations.narrative_document.rc8.347573497c"),
  copyFailed: copy("visualizations.narrative_document.rc8.456864401b"),
  replay: copy("pages.live_session.renderReplayLens.title.66e55d7614"),
  invalid: copy("visualizations.narrative_document.rc8.acaa1afc87"),
  recovery: copy("visualizations.narrative_document.rc8.78d4ac7fdd"),
  missingOperation: copy("visualizations.narrative_document.problem.operation.missing"),
  corruptOperation: copy("visualizations.narrative_document.problem.operation.corrupt"),
  legacyOperation: copy("visualizations.narrative_document.problem.operation.legacy"),
  storyAutomatic: copy("visualizations.narrative_document.problem.automatic.story"),
  legacyAutomatic: copy("visualizations.narrative_document.problem.automatic.legacy"),
  missingNext: copy("visualizations.narrative_document.problem.next.missing"),
  restoreNext: copy("visualizations.narrative_document.problem.next.restore"),
});

function node(tag, attributes = {}, children = []) {
  const element = document.createElement(tag);
  const isTime = String(tag || "").toLowerCase() === "time";
  for (const [name, value] of Object.entries(attributes)) {
    if (value === undefined || value === null || value === false) continue;
    if (name === "class") element.className = String(value);
    else if (name === "text") element.textContent = isTime ? formatUtc8Minute(value) : String(value);
    else if (name === "dataset") Object.assign(element.dataset, value);
    else element.setAttribute(name, String(value));
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child) element.append(child);
  }
  return element;
}

function rawDocument(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (value.narrative_document && typeof value.narrative_document === "object") {
    return value.narrative_document;
  }
  if (value.document && typeof value.document === "object") return value.document;
  if (Array.isArray(value.blocks)) return value;
  return null;
}

function safeBlock(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const kind = String(raw.kind || "").trim().toLowerCase();
  const text = String(raw.text || "").trim();
  const visibility = String(raw.visibility || "").trim().toLowerCase();
  if (!KIND_SET.has(kind) || !text || text.length > 500) return null;
  if (!PUBLIC_VISIBILITIES.has(visibility)) return false;
  const speakerLabel = String(raw.speaker_label || raw.speaker?.label || "").trim();
  if (kind === "dialogue" && !speakerLabel) return null;
  if (speakerLabel.length > 80) return null;
  return {
    kind,
    text,
    tone: String(raw.tone || "").trim().slice(0, 80),
    speakerLabel: ["dialogue", "reaction"].includes(kind) ? speakerLabel : "",
  };
}

export function publicNarrativeDocument(value) {
  const raw = rawDocument(value);
  if (!raw) return null;
  if (raw.schema && raw.schema !== NARRATIVE_DOCUMENT_SCHEMA) return null;
  const mode = String(raw.mode || "").trim().toLowerCase();
  if (!new Set(["minimal", "balanced", "epic"]).has(mode)) return null;
  if (!Array.isArray(raw.blocks) || !raw.blocks.length) return null;
  const blocks = [];
  for (const candidate of raw.blocks) {
    const block = safeBlock(candidate);
    if (block === null) return null;
    if (block !== false) blocks.push(block);
  }
  if (!blocks.length) return null;
  return {
    mode,
    title: String(raw.title || "").trim().slice(0, 120),
    blocks,
    continuity: raw.continuity && typeof raw.continuity === "object"
      ? { ...raw.continuity }
      : {},
  };
}

function explicitLegacy(value) {
  if (!value || typeof value !== "object") return null;
  const candidate = value.legacy_record === true
    ? value
    : value.narrative_document?.legacy_record === true
      ? value.narrative_document
      : null;
  if (!candidate) return null;
  const text = String(candidate.text || value.text || value.summary || "").trim();
  if (!text || text.length > 12000) return null;
  return { text, label: String(candidate.label || "").trim() };
}

function dialogueText(block) {
  const body = /^[“\"]/.test(block.text) ? block.text : `“${block.text}”`;
  return `「${block.speakerLabel}」\n${body}`;
}

export function narrativePlainText(value, { includeTitle = false } = {}) {
  const documentValue = publicNarrativeDocument(value);
  if (!documentValue) return explicitLegacy(value)?.text || "";
  const paragraphs = documentValue.blocks.map((block) =>
    block.kind === "dialogue" ? dialogueText(block) : block.text);
  if (includeTitle && documentValue.title) paragraphs.unshift(documentValue.title);
  return paragraphs.join("\n\n");
}

function renderBlock(block, index) {
  const wrapper = node("section", {
    class: "tavern-story-block",
    dataset: { blockKind: block.kind, blockIndex: String(index) },
  });
  if (block.speakerLabel) {
    wrapper.append(node("strong", {
      class: "tavern-story-speaker",
      text: `「${block.speakerLabel}」`,
    }));
  }
  const bodyTag = block.kind === "dialogue" ? "blockquote" : "p";
  wrapper.append(node(bodyTag, { text: block.text }));
  return wrapper;
}

export function renderNarrativeBlocks(documentValue, {
  initialBlocks = INITIAL_BLOCKS,
  expandable = true,
} = {}) {
  const safe = publicNarrativeDocument(documentValue);
  if (!safe) return null;
  const limit = Math.max(1, Math.min(8, Number(initialBlocks) || INITIAL_BLOCKS));
  const body = node("div", { class: "tavern-story-copy" });
  safe.blocks.forEach((block, index) => {
    const rendered = renderBlock(block, index);
    if (expandable && index >= limit) rendered.hidden = true;
    body.append(rendered);
  });
  if (expandable && safe.blocks.length > limit) {
    const toggle = node("button", {
      class: "tavern-button tavern-story-expand",
      type: "button",
      "data-variant": "quiet",
      text: TEXT.continue,
      "aria-expanded": "false",
    });
    toggle.addEventListener("click", () => {
      const expanded = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(expanded));
      toggle.textContent = expanded ? TEXT.collapse : TEXT.continue;
      for (const child of body.querySelectorAll("[data-block-index]")) {
        child.hidden = !expanded && Number(child.dataset.blockIndex) >= limit;
      }
    });
    body.append(toggle);
  }
  return body;
}

function problemDefaults(code) {
  const normalized = String(code || "").trim().toLowerCase();
  if (normalized.includes("legacy_record_invalid")) {
    return {
      operation: TEXT.legacyOperation,
      automatic: TEXT.legacyAutomatic,
      nextStep: TEXT.restoreNext,
    };
  }
  if (normalized.includes("corrupt") || normalized.includes("document_invalid")) {
    return {
      operation: TEXT.corruptOperation,
      automatic: TEXT.storyAutomatic,
      nextStep: TEXT.restoreNext,
    };
  }
  return {
    operation: TEXT.missingOperation,
    automatic: TEXT.storyAutomatic,
    nextStep: TEXT.missingNext,
  };
}

function problemFor(story) {
  const supplied = story?.narrative_problem
    || (Array.isArray(story?.problems) ? story.problems[0] : null);
  const code = supplied?.code || "story.narrative_document_unavailable";
  const defaults = problemDefaults(code);
  const reason = supplied?.reason || supplied?.message || TEXT.invalid;
  const recovery = supplied?.recovery || TEXT.recovery;
  const nextStep = supplied?.next_step || (
    supplied?.recovery
      ? `${defaults.nextStep}；${supplied.recovery}`
      : defaults.nextStep
  );
  return {
    operation: supplied?.operation || defaults.operation,
    reason,
    message: reason,
    automatic: supplied?.automatic || supplied?.automatic_action || defaults.automatic,
    next_step: nextStep,
    recovery,
    code,
    retryable: supplied?.retryable !== false,
  };
}

export function renderNarrativeDocument(story, {
  session = {},
  narrativeMode = {},
  initialBlocks = INITIAL_BLOCKS,
  controls = true,
  compact = false,
  onReplay = null,
} = {}) {
  const documentValue = publicNarrativeDocument(story);
  const legacy = explicitLegacy(story);
  const article = node("article", {
    class: `tavern-story-stage${compact ? " tavern-is-compact" : ""}`,
    "data-testid": "tavern-live-story",
    dataset: { storyFormat: documentValue ? "document" : legacy ? "legacy" : "problem" },
  });
  const chips = [
    session.state_label || session.state,
    session.scene_label,
    session.time_label ? formatUtc8Minute(session.time_label) : "",
    narrativeMode.label,
    story?.source_label,
  ].filter(Boolean);
  article.append(node("header", {}, [
    node("div", {}, [
      node("p", {
        class: "tavern-page-eyebrow",
        text: story?.source_label || copy("pages.live_session.storyStage.message.4771a6ddfd"),
      }),
      node("h2", {
        text: documentValue?.title || story?.title || story?.label
          || copy("pages.live_session.storyStage.message.4771a6ddfd"),
      }),
      chips.length
        ? node("div", { class: "tavern-live-chips" }, chips.map((chip) =>
          node("span", { text: chip })))
        : null,
    ]),
  ]));
  if (documentValue) {
    article.append(renderNarrativeBlocks(documentValue, { initialBlocks, expandable: !compact }));
  } else if (legacy) {
    article.append(node("div", { class: "tavern-story-copy" }, [
      node("p", { text: legacy.text, dataset: { legacyRecord: "true" } }),
    ]));
  } else {
    const narrativeProblem = problemFor(story);
    article.append(renderStatePanel({
      phase: "partial",
      operation: narrativeProblem.operation,
      problem: narrativeProblem,
    }));
  }
  if (controls && (documentValue || legacy)) {
    const feedback = node("span", { class: "tavern-story-action-feedback", role: "status" });
    const copyButton = node("button", {
      class: "tavern-button",
      type: "button",
      "data-variant": "secondary",
      text: TEXT.copy,
    });
    copyButton.addEventListener("click", async () => {
      try {
        if (typeof globalThis.navigator?.clipboard?.writeText !== "function") throw new Error("clipboard-unavailable");
        await globalThis.navigator.clipboard.writeText(narrativePlainText(story));
        feedback.textContent = TEXT.copied;
      } catch (_error) {
        feedback.textContent = TEXT.copyFailed;
      }
    });
    const replayButton = typeof onReplay === "function"
      ? node("button", {
        class: "tavern-button",
        type: "button",
        "data-variant": "quiet",
        text: TEXT.replay,
      })
      : null;
    replayButton?.addEventListener("click", onReplay);
    article.append(node("footer", { class: "tavern-story-actions" }, [
      copyButton,
      replayButton,
      feedback,
    ]));
  }
  return article;
}

export function narrativeRecordKind(value) {
  if (publicNarrativeDocument(value)) return "document";
  if (explicitLegacy(value)) return "legacy";
  if (value?.narrative_problem && typeof value.narrative_problem === "object") return "problem";
  return "event";
}

export function hasStructuredNarrative(items) {
  return Array.isArray(items) && items.some((item) => narrativeRecordKind(item) !== "event");
}

export function renderNarrativeReplay(items = []) {
  const section = node("section", {
    class: "tavern-narrative-replay",
    dataset: { visualization: "narrative-replay" },
  }, [node("h3", { text: TEXT.replay })]);
  for (const item of Array.isArray(items) ? items : []) {
    const documentValue = publicNarrativeDocument(item);
    const legacy = explicitLegacy(item);
    const card = node("article", { class: "tavern-replay-story" }, [
      node("header", {}, [
        node("strong", { text: item.title || item.label || documentValue?.title || TEXT.replay }),
        item.created_at || item.time_label
          ? node("time", { datetime: item.created_at || "", text: item.created_at || item.time_label })
          : null,
      ]),
    ]);
    if (documentValue) card.append(renderNarrativeBlocks(documentValue, { expandable: false }));
    else if (legacy) card.append(node("p", { text: legacy.text, dataset: { legacyRecord: "true" } }));
    else if (item.narrative_problem) {
      const narrativeProblem = problemFor(item);
      card.append(renderStatePanel({
        phase: "partial",
        operation: narrativeProblem.operation,
        problem: narrativeProblem,
      }));
    }
    else if (item.summary) card.append(node("p", { text: item.summary }));
    section.append(card);
  }
  return section;
}
