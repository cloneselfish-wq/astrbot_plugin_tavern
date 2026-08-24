import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import {
  canonicalStateFamily,
  renderStatePanel,
} from "../components/empty-problem.js";
import { renderFormField } from "../components/forms.js";

let editorSequence = 0;

const FIELD_LABELS = Object.freeze({
  action: copy("dialogs.dialogs.module.message.1c80d4a064"), reason: copy("dialogs.dialogs.module.message.d30c83cc43"), confirmation_name: copy("dialogs.dialogs.module.message.41c4369613"),
  acknowledge_archive: copy("dialogs.dialogs.module.message.6d7763a8a2"), acknowledge_install: copy("dialogs.dialogs.module.message.858d2546c8"),
  acknowledge_retire: copy("dialogs.dialogs.module.message.7ad9c5e429"), acknowledge_departure: copy("dialogs.dialogs.module.message.025befba0a"),
  acknowledge_restore: copy("dialogs.dialogs.module.message.ac5c9bde9a"), acknowledge_replace: copy("dialogs.dialogs.module.message.98d004e69c"),
  acknowledge_delete: copy("dialogs.dialogs.module.message.0d260d3cb8"), acknowledge_trash: copy("dialogs.dialogs.module.message.6f58e31f0e"),
  acknowledge_migration: copy("dialogs.dialogs.module.message.3e8c663f7e"), acknowledge_pacing: copy("dialogs.dialogs.module.message.e9e9a42d54"),
  restore_confirmation: copy("dialogs.dialogs.module.message.3cbe617c18"), pacing_action: copy("dialogs.dialogs.module.message.61eb3bfc5c"), timer_operation: copy("dialogs.dialogs.module.operation.8280250640"),
  timer_seconds: copy("dialogs.dialogs.module.message.0b3c52b7f4"), module: copy("dialogs.dialogs.module.message.4b85a43ce5"), enabled: copy("dialogs.dialogs.module.message.6419a4cc48"),
  github_repository: copy("dialogs.dialogs.module.message.720f02a99a"), repo_url: copy("dialogs.dialogs.module.message.720f02a99a"),
  github_branch: copy("dialogs.dialogs.module.message.a251448a84"), branch: copy("dialogs.dialogs.module.message.a251448a84"), github_candidate: copy("dialogs.dialogs.module.message.cc4c99b961"),
  card_field: copy("dialogs.dialogs.module.message.43775fbe9c"), name: copy("dialogs.dialogs.module.message.d44e9b3d3b"), label: copy("dialogs.dialogs.module.label.d44e9b3d3b"), description: copy("dialogs.dialogs.module.description.4262c45dc7"), help: copy("dialogs.dialogs.module.message.56c6469bcf"),
  field_type: copy("dialogs.dialogs.module.message.9eb4ed7763"), required: copy("dialogs.dialogs.module.message.84113c0f7e"), preset: copy("dialogs.dialogs.module.message.f1de1e2621"), role: copy("dialogs.dialogs.module.message.fe5ed6a740"),
  private_direction: copy("dialogs.dialogs.module.message.69d3bd5c72"), simulation_turns: copy("dialogs.dialogs.module.message.60f7435df4"), party_size: copy("dialogs.dialogs.module.message.329dbb3503"),
  memory_operation: copy("dialogs.dialogs.module.operation.4bff2233ae"), window: copy("dialogs.dialogs.module.message.1888476e9c"), limit: copy("dialogs.dialogs.module.message.32ce9f0658"), candidate_world: copy("dialogs.dialogs.module.message.7ca8426498"),
  file: copy("dialogs.dialogs.module.message.52481b58c2"), instance_name: copy("dialogs.dialogs.module.message.aaad5f2e1b"), instance_slug: copy("dialogs.dialogs.module.message.4e6b38609e"),
  restrict_groups: copy("dialogs.dialogs.module.message.17e5248fef"), unauthorized_behavior: copy("dialogs.dialogs.module.message.3a3b8ff88e"),
  public_status: copy("dialogs.dialogs.module.message.7ba17fee1a"), request_timeout_seconds: copy("dialogs.dialogs.module.message.ea2564fe97"),
  generation_budget_total_seconds: copy("dialogs.dialogs.module.message.d7c6e4bf96"), generation_budget_per_call_seconds: copy("dialogs.dialogs.module.message.90b9004df3"),
  generation_budget_max_calls: copy("dialogs.dialogs.module.message.8e3597404b"), generation_budget_max_fallbacks: copy("dialogs.dialogs.module.message.c6a0d9a9da"),
  user_cooldown_seconds: copy("dialogs.dialogs.module.message.be37d71669"), temperature: copy("dialogs.dialogs.module.message.06c5a1370e"), max_tokens: copy("dialogs.dialogs.module.message.56627c94a9"),
  json_repair_attempts: copy("dialogs.dialogs.module.message.d3f19026e7"), max_input_chars: copy("dialogs.dialogs.module.message.75fb0b335b"),
  max_output_chars: copy("dialogs.dialogs.module.message.ade335c5e6"), recent_turns: copy("dialogs.dialogs.module.message.5b024d3607"), memory_limit: copy("dialogs.dialogs.module.message.643023decc"),
  two_phase_checks: copy("dialogs.dialogs.module.message.649f40d228"), auto_snapshot_interval: copy("dialogs.dialogs.module.message.c1550d523e"),
  audit_retention_days: copy("dialogs.dialogs.module.message.c4d4dfa269"), store_model_payloads: copy("dialogs.dialogs.module.message.a6476fca5d"),
  panel_enabled: copy("dialogs.dialogs.module.message.b9aa487b37"), allow_insecure_http: copy("dialogs.dialogs.module.message.2a084a54f4"), secure_cookie: copy("dialogs.dialogs.module.message.b596b0cb82"),
  mode: copy("pages.live_session.controlPanel.text.16e8f15cb9"),
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

function fieldLabel(field) {
  return field.label
    || FIELD_LABEL_KEYS[field.labelKey]
    || FIELD_LABELS[field.name]
    || copy("dialogs.dialogs.fieldLabel.message.996f2eeff2");
}

function textBlock(label, value) {
  if (!value) return null;
  const section = document.createElement("section");
  const heading = document.createElement("h3");
  heading.textContent = label;
  const paragraph = document.createElement("p");
  paragraph.textContent = value;
  section.append(heading, paragraph);
  return section;
}

export function openEditor(manager, {
  objectKey,
  fields = [],
  draft = {},
  validate = () => ({}),
  preview,
  submit,
  idempotencyKey,
  opener,
  title = copy("dialogs.dialogs.openEditor.message.0518365699"),
  kicker = "",
  specialization = "",
  intro = null,
  contextFacts = [],
  feedback = null,
  shellFooter = true,
  labels = {},
} = {}) {
  const form = document.createElement("form");
  form.className = "tavern-editor";
  if (specialization) form.dataset.editorSpecialization = specialization;
  form.id = `tavern-editor-form-${++editorSequence}`;
  form.dataset.editorState = "clean";
  const context = document.createElement("section");
  context.className = "tavern-editor-context";
  const visibleFacts = (Array.isArray(contextFacts) ? contextFacts : [])
    .filter((fact) => fact?.label && fact?.value !== undefined && fact?.value !== null);
  if (visibleFacts.length) {
    context.append(...visibleFacts.map((fact) => {
      const item = document.createElement("div");
      const term = document.createElement("small");
      term.textContent = fact.label;
      const description = document.createElement("strong");
      description.textContent = String(fact.value);
      item.append(term, description);
      return item;
    }));
  } else {
    const changeCheck = document.createElement("p");
    changeCheck.textContent = copy("dialogs.editor.change_check");
    context.append(changeCheck);
  }
  const introArea = document.createElement("section");
  introArea.className = "tavern-editor-intro";
  if (intro) {
    if (intro.kicker) {
      const eyebrow = document.createElement("span");
      eyebrow.className = "tavern-story-kicker";
      eyebrow.textContent = intro.kicker;
      introArea.append(eyebrow);
    }
    const heading = document.createElement("h3");
    heading.textContent = intro.title || title;
    const paragraph = document.createElement("p");
    paragraph.textContent = intro.summary || "";
    introArea.append(heading, paragraph);
  } else {
    const heading = document.createElement("h3");
    heading.textContent = title;
    introArea.append(heading);
  }
  const fieldArea = document.createElement("div");
  fieldArea.className = "tavern-editor-fields";
  const previewArea = document.createElement("section");
  previewArea.className = "tavern-editor-preview tavern-editor-impact";
  previewArea.setAttribute("aria-live", "polite");
  const status = document.createElement("div");
  status.className = "tavern-editor-problems tavern-editor-feedback";
  status.setAttribute("aria-live", "assertive");
  const footer = document.createElement("footer");
  footer.className = "tavern-dialog-footer tavern-editor-actions";
  let dirty = false;
  let submitting = false;
  let dialog = null;
  let submitMode = "save";
  let previewedSignature = "";
  const replayKey = idempotencyKey || crypto.randomUUID();
  const submittedFields = fields.filter((field) => field?.uiOnly !== true);
  let currentDraft = {
    ...Object.fromEntries(fields.map((field) => [
      field.name,
      field.value ?? field.default ?? (["boolean", "checkbox"].includes(field.type) ? false : ""),
    ])),
    ...draft,
  };

  const setEditorState = (state) => {
    form.dataset.editorState = state;
    if (dialog) dialog.dataset.editorState = state;
  };
  const values = () => Object.fromEntries(submittedFields.map((field) => {
    const control = form.elements[field.name];
    if (field.type === "file") {
      return [field.name, control?.files?.[0] || null];
    }
    if (["boolean", "checkbox"].includes(field.type)) {
      return [field.name, control?.checked === true];
    }
    return [field.name, control?.value ?? ""];
  }));
  const renderFields = (errors = {}) => {
    fieldArea.replaceChildren(...fields.map((field) => {
      const node = renderFormField({
        ...field,
        label: fieldLabel(field),
        value: currentDraft[field.name] ?? "",
        error: errors[field.name] || "",
      });
      if (field.wide) node.classList.add("tavern-field-wide");
      if (field.uiOnly) node.dataset.uiOnly = "true";
      return node;
    }));
  };
  const showPreview = (result) => {
    if (result instanceof Node) {
      previewArea.replaceChildren(result);
      return;
    }
    const block = textBlock(
      copy("dialogs.dialogs.showPreview.message.0ce4c40f92"),
      typeof result === "string"
        ? result
        : result?.summary || result?.message || copy("dialogs.dialogs.showPreview.message.6f08645665"),
    );
    const rawDiff = result && typeof result === "object"
      ? (result.diff_summary || result.diff)
      : "";
    const diff = Array.isArray(rawDiff)
      ? rawDiff.filter((item) => typeof item === "string").join("；")
      : typeof rawDiff === "string" ? rawDiff : "";
    const diffBlock = textBlock(
      copy("dialogs.editor.preview_heading"),
      diff || copy("dialogs.editor.diff_unavailable"),
    );
    diffBlock.className = "tavern-editor-diff";
    previewArea.replaceChildren(block, diffBlock);
  };
  const showBlockedDismissal = () => {
    const confirmation = renderStatePanel({
      phase: "conflict",
      operation: copy("dialogs.dialogs.showBlockedDismissal.operation.7951c0e860"),
      problem: {
        message: copy("dialogs.dialogs.showBlockedDismissal.message.2697bd6128"),
        recovery: copy("dialogs.dialogs.showBlockedDismissal.recovery.444feb9ee4"),
      },
    });
    const confirmDiscard = renderButton({
      variant: "danger",
      label: copy("dialogs.dialogs.showBlockedDismissal.label.8fb781e6c9"),
      onActivate: () => manager.close("discarded", { force: true }),
    });
    confirmation.append(confirmDiscard);
    status.replaceChildren(confirmation);
    confirmDiscard.focus();
  };
  const discard = renderButton({
    variant: "quiet",
    label: labels.cancel || copy("dialogs.dialogs.showPreview.label.ef804eda90"),
    onActivate: () => dirty
      ? showBlockedDismissal()
      : manager.close("discarded", { force: true }),
  });
  const save = renderButton({
    variant: "primary",
    label: labels.submit || copy("dialogs.editor.submit_action"),
    buttonType: "submit",
    onActivate: () => { submitMode = "save"; },
  });
  const previewAction = preview ? renderButton({
    variant: "secondary",
    label: labels.preview || copy("dialogs.editor.preview_action"),
    buttonType: "submit",
    onActivate: () => { submitMode = "preview"; },
  }) : null;
  footer.append(discard);
  if (previewAction) footer.append(previewAction);
  footer.append(save);
  for (const button of [discard, previewAction, save].filter(Boolean)) {
    button.setAttribute("form", form.id);
  }
  if (preview) {
    previewArea.append(textBlock(
      copy("dialogs.editor.preview_heading"),
      copy("dialogs.editor.preview_pending"),
    ));
  }
  const feedbackArea = feedback ? document.createElement("div") : null;
  if (feedbackArea) {
    feedbackArea.className = "tavern-editor-feedback";
    const strong = document.createElement("strong");
    strong.textContent = feedback.title || "";
    const span = document.createElement("span");
    span.textContent = feedback.summary || "";
    feedbackArea.append(strong, span);
  }
  form.append(...[context, introArea, fieldArea, preview ? previewArea : null, status, feedbackArea].filter(Boolean));
  renderFields();

  form.addEventListener("input", () => {
    dirty = true;
    setEditorState("dirty");
    previewedSignature = "";
    currentDraft = { ...currentDraft, ...values() };
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) return;
    currentDraft = values();
    setEditorState("validating");
    const errors = validate(currentDraft) || {};
    if (Object.keys(errors).length) {
      setEditorState("invalid");
      renderFields(errors);
      status.replaceChildren(renderStatePanel({
        phase: "error",
        operation: copy("dialogs.dialogs.showBlockedDismissal.operation.c8550237ba"),
        problem: {
          message: copy("dialogs.dialogs.showBlockedDismissal.message.5c3f6d9184"),
          recovery: copy("dialogs.dialogs.showBlockedDismissal.recovery.2a308713c9"),
        },
      }));
      fieldArea.querySelector('[aria-invalid="true"]')?.focus();
      return;
    }
    const signature = JSON.stringify(currentDraft, (_key, value) => (
      typeof File !== "undefined" && value instanceof File
        ? { name: value.name, size: value.size, type: value.type, lastModified: value.lastModified }
        : value
    ));
    submitting = true;
    setEditorState("validating");
    save.disabled = true;
    save.setAttribute("aria-busy", "true");
    try {
      // Preview is an explicit secondary action.  The primary submit button
      // must never be silently converted into a preview-only round trip: that
      // made every editor using this shared component look saved while no
      // semantic intent had actually been sent.
      if (preview && submitMode === "preview") {
        const result = await preview(currentDraft);
        showPreview(result);
        previewedSignature = signature;
        setEditorState("preview");
        status.replaceChildren(renderStatePanel({
          phase: "ready",
          operation: copy("dialogs.dialogs.showBlockedDismissal.operation.6116e64140"),
          problem: {
            message: copy("dialogs.dialogs.showBlockedDismissal.message.1951200d7f"),
            recovery: copy("dialogs.dialogs.showBlockedDismissal.recovery.9232d1d292"),
          },
        }));
        return;
      }
      const outcome = await submit?.({
        objectKey,
        draft: currentDraft,
        idempotencyKey: replayKey,
      });
      dirty = false;
      setEditorState("clean");
      if (manager.current?.dialog && manager.current.dialog !== dialog) return;
      if (typeof outcome?.afterClose === "function") {
        manager.close("collected", { force: true });
        queueMicrotask(() => outcome.afterClose());
        return;
      }
      manager.close("saved", { force: true });
    } catch (error) {
      const conflict = error?.status === 409;
      const family = conflict ? "conflict" : canonicalStateFamily("error", error);
      const state = conflict
        ? "conflict"
        : family === "rate_limited"
          ? "rate-limited"
          : ["timeout", "disconnect"].includes(family)
            ? "lost-response-replay"
            : "dirty";
      setEditorState(state);
      status.replaceChildren(renderStatePanel({
        phase: family,
        operation: copy("dialogs.dialogs.showBlockedDismissal.operation.c8550237ba"),
        problem: error,
        lastGood: currentDraft,
        retryAction: error?.retryable ? () => form.requestSubmit() : null,
      }));
    } finally {
      submitting = false;
      save.disabled = false;
      save.setAttribute("aria-busy", "false");
    }
  });

  dialog = manager.openDialog({
    kind: "editor",
    opener,
    title,
    kicker,
    specialization,
    footer,
    dismissPolicy: () => dirty ? "locked" : "escape",
    onDismissBlocked: showBlockedDismissal,
    content: form,
  });
  dialog.dataset.editorState = form.dataset.editorState;
  dialog.isDirty = () => dirty;
  dialog.currentDraft = () => ({ ...currentDraft });
  return dialog;
}
