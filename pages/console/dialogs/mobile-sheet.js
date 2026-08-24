export function openSheet(manager, {
  title,
  content,
  actions = [],
  opener,
  mode = "detail",
  kicker = "",
  dismissPolicy = "escape",
  returnToPrevious = false,
} = {}) {
  const wrapper = document.createElement("div");
  wrapper.className = "tavern-mobile-sheet";
  wrapper.dataset.sheetMode = ["detail", "editor", "confirm"].includes(mode)
    ? mode
    : "detail";
  const body = document.createElement("div");
  body.className = "tavern-mobile-sheet-body";
  const panel = document.createElement("section");
  panel.className = "tavern-mobile-sheet-panel";
  if (content) panel.append(content);
  body.append(panel);
  wrapper.append(body);
  const footer = document.createElement("footer");
  footer.className = "tavern-dialog-footer tavern-mobile-sheet-actions";
  footer.append(...actions);
  if (!actions.length) footer.hidden = true;
  const dialog = manager.openDialog({
    kind: "mobile_sheet",
    opener,
    title,
    kicker,
    size: "sheet",
    dismissPolicy,
    specialization: wrapper.dataset.sheetMode,
    content: wrapper,
    footer,
    returnToPrevious,
  });
  dialog.querySelector(".tavern-sheet-handle")?.setAttribute("data-sheet-mode", wrapper.dataset.sheetMode);
  return dialog;
}
