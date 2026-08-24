import { copy } from "../copy/catalog.js";
import { renderButton } from "./buttons.js";
import { createElement, testToken } from "./dom.js";

export function renderPagination({
  workspace,
  cursor = "",
  nextCursor = "",
  previousCursor = "",
  rangeLabel = "",
  hasMore = false,
  onPage = () => {},
} = {}) {
  const workspaceToken = testToken(workspace, "workspace");
  const nav = createElement("nav", {
    class: "tavern-pagination",
    "aria-label": copy("components.primitives.renderPagination.message.66c9fcc282", { p0: workspace }),
    "data-testid": `tavern-pagination-${workspaceToken}`,
    "aria-busy": "false",
  });
  let pending = false;
  let previous;
  let next;
  const activate = async (target) => {
    if (pending || !target) return;
    pending = true;
    nav.setAttribute("aria-busy", "true");
    previous.disabled = true;
    next.disabled = true;
    try {
      if (typeof onPage === "function") await onPage(target);
    } finally {
      pending = false;
      nav.setAttribute("aria-busy", "false");
      previous.disabled = !previousCursor;
      next.disabled = !(hasMore && nextCursor);
    }
  };
  previous = renderButton({
    label: copy("components.primitives.renderPagination.label.c9b9ae7a61"),
    disabledReason: previousCursor ? "" : copy("components.primitives.renderPagination.message.b0b1ba503e"),
    onActivate: () => activate(previousCursor),
  });
  next = renderButton({
    label: copy("components.primitives.renderPagination.label.8a8542f696"),
    disabledReason: hasMore && nextCursor ? "" : copy("components.primitives.renderPagination.message.cf1cf77b7c"),
    onActivate: () => activate(nextCursor),
  });
  nav.append(previous, createElement("span", {
    "aria-live": "polite",
    "aria-atomic": "true",
    text: rangeLabel,
  }), next);
  nav.dataset.cursor = testToken(cursor, "start");
  return nav;
}
