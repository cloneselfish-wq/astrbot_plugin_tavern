import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import {
  canonicalStateFamily,
  renderStatePanel,
} from "../components/empty-problem.js";

function textBlock(kind, label, value) {
  const section = document.createElement("section");
  section.className = "tavern-confirm-section";
  section.dataset.confirmSection = kind;
  const heading = document.createElement("h3");
  heading.textContent = label;
  const paragraph = document.createElement("p");
  paragraph.textContent = value || copy("dialogs.session_detail.definitions.message.756762e293");
  section.append(heading, paragraph);
  return section;
}

export function openConfirm(manager, {
  opener,
  operation,
  impact,
  unchanged,
  automatic,
  recovery,
  returnCheck,
  confirmLabel,
  intent,
  idempotencyKey,
  onConfirm,
} = {}) {
  const content = document.createElement("div");
  content.className = "tavern-confirm";
  content.dataset.confirmState = "review";
  const intro = document.createElement("section");
  intro.className = "tavern-confirm-intro";
  const badge = document.createElement("span");
  badge.className = "tavern-confirm-badge";
  badge.textContent = copy("dialogs.session_detail.runManagementAction.message.e92b4d6e88");
  const heading = document.createElement("h3");
  heading.textContent = operation;
  const explanation = document.createElement("p");
  explanation.textContent = impact || copy("dialogs.session_detail.actionConfirmation.message.21d8f5baf7");
  intro.append(badge, heading, explanation);
  const impactGrid = document.createElement("div");
  impactGrid.className = "tavern-confirm-impact";
  for (const block of [
    textBlock("operation", copy("dialogs.dialogs.module.message.1c80d4a064"), operation),
    textBlock("impact", copy("dialogs.dialogs.openConfirm.message.ba5fc2e8c8"), impact),
    textBlock("unchanged", copy("dialogs.dialogs.openConfirm.message.8cc4a72738"), unchanged),
    textBlock("automatic", copy("dialogs.dialogs.openConfirm.message.57615f7fba"), automatic),
    textBlock("recovery", copy("dialogs.dialogs.openConfirm.message.98edeb449e"), recovery),
    textBlock("return-check", copy("dialogs.dialogs.openConfirm.message.ce4545dd61"), returnCheck),
  ]) {
    impactGrid.append(block);
  }
  content.append(intro, impactGrid);
  const status = document.createElement("div");
  status.className = "tavern-confirm-status";
  status.setAttribute("aria-live", "assertive");
  let pending = false;
  let attempts = 0;
  let dialog = null;
  const setState = (state) => {
    content.dataset.confirmState = state;
    if (dialog) dialog.dataset.confirmState = state;
  };
  const replayKey = idempotencyKey || crypto.randomUUID();
  const confirm = renderButton({
    variant: "danger",
    label: confirmLabel || operation,
    intent,
    onActivate: async () => {
      if (pending) return;
      pending = true;
      setState(attempts > 0 ? "replayed" : "submitting");
      attempts += 1;
      confirm.disabled = true;
      confirm.setAttribute("aria-busy", "true");
      try {
        await onConfirm?.({ idempotencyKey: replayKey });
        manager.close("confirmed", { force: true });
      } catch (error) {
        setState("failed");
        const family = canonicalStateFamily("error", error);
        status.replaceChildren(renderStatePanel({
          phase: family,
          operation,
          problem: error,
          retryAction: error?.retryable ? () => confirm.click() : null,
        }));
      } finally {
        pending = false;
        confirm.disabled = false;
        confirm.setAttribute("aria-busy", "false");
      }
    },
  });
  const back = renderButton({
    variant: "quiet",
    label: copy("dialogs.confirm.return_check"),
    onActivate: () => manager.close("return-check", { force: true }),
  });
  const footer = document.createElement("footer");
  footer.className = "tavern-dialog-footer";
  footer.append(back, confirm);
  content.append(status);
  dialog = manager.openDialog({
    kind: "confirm",
    opener,
    title: operation,
    size: "small",
    content,
    footer,
  });
  dialog.dataset.confirmState = "review";
  return dialog;
}
