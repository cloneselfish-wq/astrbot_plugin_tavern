import { copy } from "../copy/catalog.js";
import { createElement, testToken } from "./dom.js";

const BUTTON_VARIANTS = new Set(["primary", "secondary", "quiet", "danger", "inline"]);

export function renderButton({
  variant = "secondary",
  label = "",
  labelKey = "",
  icon = null,
  busy = false,
  disabledReason = "",
  intent = null,
  onActivate = null,
  buttonType = "button",
} = {}) {
  const accessibleLabel = String(label || (labelKey ? copy(labelKey) : "")).trim();
  const safeVariant = BUTTON_VARIANTS.has(variant) ? variant : "secondary";
  const actionId = intent?.id ? testToken(intent.id, "action") : "";
  const reasonId = disabledReason ? `tavern-disabled-${actionId || testToken(accessibleLabel, "button")}` : "";
  const button = createElement("button", {
    class: "tavern-button",
    type: buttonType,
    "data-variant": safeVariant,
    "aria-busy": busy ? "true" : "false",
    "aria-disabled": busy || disabledReason || !accessibleLabel ? "true" : "false",
    "aria-label": accessibleLabel || undefined,
    "aria-describedby": reasonId || undefined,
    "data-testid": actionId ? `tavern-button-${actionId}` : undefined,
  });
  if (icon) button.append(icon);
  button.append(createElement("span", { class: "tavern-button-label", text: accessibleLabel }));
  if (busy) {
    button.append(createElement("span", {
      class: "tavern-button-progress",
      text: copy("components.primitives.renderButton.text.694b71bc80"),
    }));
  }
  if (disabledReason) {
    button.append(createElement("small", {
      id: reasonId,
      class: "tavern-button-reason",
      text: disabledReason,
    }));
  }
  button.disabled = Boolean(busy || disabledReason || !accessibleLabel);
  if (intent?.id) button.dataset.actionId = intent.id;
  button.addEventListener("click", (event) => {
    if (!button.disabled && typeof onActivate === "function") {
      onActivate(intent, event);
    }
  });
  return button;
}
