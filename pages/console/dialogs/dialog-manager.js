import { copy } from "../copy/catalog.js";

const FOCUSABLE_SELECTOR = 'button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
const DIALOG_FAMILIES = new Set(["detail", "editor", "confirm", "mobile_sheet"]);
let dialogSequence = 0;

function resolvedDismissPolicy(policy) {
  return typeof policy === "function" ? policy() : policy;
}

function dialogFamily(kind, size) {
  const normalized = String(kind || "detail").replaceAll("-", "_");
  if (DIALOG_FAMILIES.has(normalized)) return normalized;
  if (normalized === "sheet" || size === "sheet") return "mobile_sheet";
  if (normalized === "capability") return "detail";
  return "detail";
}

function focusableNodes(dialog) {
  return [...dialog.querySelectorAll(FOCUSABLE_SELECTOR)]
    .filter((node) => !node.hidden && node.getAttribute("aria-hidden") !== "true");
}

export class DialogManager {
  constructor({ onUrlState = () => {} } = {}) {
    this.current = null;
    this.urlState = null;
    this.writeUrlState = onUrlState;
    this.onUrlState = (value) => {
      this.urlState = value;
      if (this.current) this.current.urlState = value;
      this.writeUrlState(value);
    };
  }

  discardChain(reason = "replace") {
    let state = this.current;
    this.current = null;
    while (state) {
      const parent = state.parent || null;
      state.dialog.removeEventListener("keydown", state.keydown);
      state.dialog.removeEventListener("cancel", state.cancel);
      state.dialog.removeEventListener("pointerdown", state.pointerdown);
      state.dialog.dataset.dialogState = "closed";
      if (state.dialog.open) state.dialog.close();
      state.dialog.remove();
      state.onClose(reason);
      state = parent;
    }
    delete document.body.dataset.dialogOpen;
  }

  openDialog({
    kind,
    opener = document.activeElement,
    title,
    size = "medium",
    dismissPolicy = "escape",
    content,
    lazyPanels = null,
    specialization = "",
    kicker = "",
    footer = null,
    initialFocus = null,
    onDismissBlocked = () => {},
    onClose = () => {},
    onOpen = () => {},
    returnToPrevious = false,
  }) {
    let parent = null;
    if (returnToPrevious && this.current) {
      parent = this.current;
      this.current = null;
      if (parent.dialog.open) parent.dialog.close();
      parent.dialog.hidden = true;
      parent.dialog.dataset.dialogState = "suspended";
    } else if (this.current) {
      this.discardChain("replace");
    }
    const dialog = document.createElement("dialog");
    const titleId = `tavern-dialog-title-${++dialogSequence}`;
    const family = dialogFamily(kind, size);
    dialog.className = "tavern-dialog";
    dialog.dataset.dialogKind = family;
    dialog.dataset.dialogSpecialization = specialization || (family !== kind ? kind : family);
    dialog.dataset.dialogState = "opening";
    dialog.dataset.size = size;
    dialog.dataset.testid = `tavern-dialog-${family}`;
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", titleId);
    dialog.tabIndex = -1;

    const shell = document.createElement("div");
    shell.className = "tavern-dialog-shell";
    const sheetHandle = family === "mobile_sheet" ? document.createElement("header") : null;
    if (sheetHandle) {
      sheetHandle.className = "tavern-sheet-handle";
      sheetHandle.setAttribute("aria-hidden", "true");
      sheetHandle.append(document.createElement("span"));
    }
    const header = document.createElement("header");
    header.className = family === "mobile_sheet"
      ? "tavern-dialog-header tavern-mobile-sheet-head"
      : "tavern-dialog-header";
    const headerCopy = document.createElement("div");
    headerCopy.className = "tavern-dialog-header-copy";
    if (kicker) {
      const eyebrow = document.createElement("small");
      eyebrow.textContent = kicker;
      headerCopy.append(eyebrow);
    }
    const heading = document.createElement("h2");
    heading.id = titleId;
    heading.textContent = title;
    headerCopy.append(heading);
    const close = document.createElement("button");
    close.type = "button";
    close.className = "tavern-button tavern-dialog-close";
    close.textContent = "×";
    close.setAttribute(
      "aria-label",
      copy("dialogs.manager.resolvedDismissPolicy.message.d24e8400fd", { p0: title }),
    );
    header.append(headerCopy, close);

    const body = document.createElement("div");
    body.className = "tavern-dialog-body";
    body.append(content || document.createElement("div"));
    if (sheetHandle) shell.append(sheetHandle);
    shell.append(header, body);
    if (footer instanceof Node) {
      footer.classList.add("tavern-dialog-actions");
      shell.append(footer);
      dialog.dataset.hasFooter = "true";
    }
    dialog.append(shell);

    const requestDismiss = (reason) => {
      const policy = resolvedDismissPolicy(dismissPolicy);
      if (policy === "locked") {
        onDismissBlocked(reason);
        return false;
      }
      if (reason === "escape" && policy !== "escape") return false;
      return this.close(reason);
    };

    const keydown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        requestDismiss("escape");
        return;
      }
      if (event.key !== "Tab") return;
      const nodes = focusableNodes(dialog);
      if (!nodes.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = nodes[0];
      const last = nodes.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    const cancel = (event) => {
      event.preventDefault();
      requestDismiss("escape");
    };
    const pointerdown = (event) => {
      if (event.target !== dialog) return;
      const bounds = dialog.getBoundingClientRect();
      const inside = event.clientX >= bounds.left && event.clientX <= bounds.right
        && event.clientY >= bounds.top && event.clientY <= bounds.bottom;
      if (!inside) requestDismiss("backdrop");
    };
    close.addEventListener("click", () => requestDismiss("button"));
    dialog.addEventListener("keydown", keydown);
    dialog.addEventListener("cancel", cancel);
    dialog.addEventListener("pointerdown", pointerdown);

    document.body.append(dialog);
    document.body.dataset.dialogOpen = "true";
    this.current = {
      dialog,
      opener,
      onClose,
      keydown,
      cancel,
      pointerdown,
      dismissPolicy,
      lazyPanels,
      family,
      parent,
      urlState: null,
    };
    this.onUrlState({ dialog: family, specialization: dialog.dataset.dialogSpecialization });
    dialog.showModal?.();
    dialog.dataset.dialogState = "open";
    const preferred = typeof initialFocus === "string"
      ? dialog.querySelector(initialFocus)
      : initialFocus;
    (preferred || focusableNodes(dialog)[0] || dialog).focus();
    onOpen(dialog);
    return dialog;
  }

  close(reason = "dismiss", {
    force = false,
    restoreFocus = true,
    updateUrl = true,
  } = {}) {
    const state = this.current;
    if (!state) return false;
    if (
      !force
      && ["escape", "button", "dismiss"].includes(reason)
      && resolvedDismissPolicy(state.dismissPolicy) === "locked"
    ) {
      return false;
    }
    this.current = null;
    state.dialog.removeEventListener("keydown", state.keydown);
    state.dialog.removeEventListener("cancel", state.cancel);
    state.dialog.removeEventListener("pointerdown", state.pointerdown);
    state.dialog.dataset.dialogState = "closed";
    if (state.dialog.open) state.dialog.close();
    state.dialog.remove();
    state.onClose(reason);
    if (state.parent) {
      const parent = state.parent;
      parent.dialog.hidden = false;
      this.current = parent;
      document.body.dataset.dialogOpen = "true";
      parent.dialog.showModal?.();
      parent.dialog.dataset.dialogState = "open";
      if (restoreFocus && state.opener?.isConnected !== false) state.opener?.focus?.();
      if (updateUrl) this.onUrlState(parent.urlState || {
        dialog: parent.family,
        specialization: parent.dialog.dataset.dialogSpecialization,
      });
      return true;
    }
    delete document.body.dataset.dialogOpen;
    if (restoreFocus && state.opener?.isConnected !== false) state.opener?.focus?.();
    if (updateUrl) this.onUrlState(null);
    return true;
  }

  closeForPermissionChange() {
    if (!this.current) return false;
    this.discardChain("permission-change");
    this.onUrlState(null);
    return true;
  }
}

export function rovingTabs(tablist, onSelect = () => {}) {
  const tabs = [...tablist.querySelectorAll('[role="tab"]')]
    .filter((tab) => !tab.disabled && !tab.hidden && tab.getAttribute("aria-hidden") !== "true");
  const activate = (index, { focus = true } = {}) => {
    if (!tabs.length) return;
    const safeIndex = Math.max(0, Math.min(tabs.length - 1, index));
    tabs.forEach((tab, itemIndex) => {
      tab.tabIndex = itemIndex === safeIndex ? 0 : -1;
      tab.setAttribute("aria-selected", String(itemIndex === safeIndex));
    });
    const selected = tabs[safeIndex];
    if (focus) selected.focus();
    onSelect(selected.dataset.tab);
  };
  tablist.addEventListener("click", (event) => {
    const tab = event.target.closest?.('[role="tab"]');
    const index = tabs.indexOf(tab);
    if (index >= 0) activate(index, { focus: false });
  });
  tablist.addEventListener("keydown", (event) => {
    const index = tabs.indexOf(document.activeElement);
    if (index < 0) return;
    let next = null;
    if (["ArrowRight", "ArrowDown"].includes(event.key)) next = (index + 1) % tabs.length;
    if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = (index - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabs.length - 1;
    if (next !== null) {
      event.preventDefault();
      activate(next);
    }
  });
  const initial = Math.max(
    0,
    tabs.findIndex((tab) => tab.getAttribute("aria-selected") === "true"),
  );
  activate(initial, { focus: false });
  return activate;
}
