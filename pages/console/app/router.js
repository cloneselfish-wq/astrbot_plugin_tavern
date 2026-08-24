import { WORKSPACES, canAccess, visibleGroups } from "../shell/registry.js";
import { FILTER_POLICIES, projectFilters } from "../contracts/page-model-policy.js";

const DETAIL_TABS = new Set(["overview", "party", "turn", "world", "delivery", "management"]);

export function readLocation(principal, currentLocation = globalThis.location) {
  const params = new URLSearchParams(currentLocation?.search || "");
  const requested = params.get("workspace") || "dashboard";
  const fallback = visibleGroups(principal)[0]?.items[0] || "about";
  const requestedAllowed = Boolean(WORKSPACES[requested] && canAccess(requested, principal));
  const workspace = requestedAllowed ? requested : fallback;
  const rawFilters = Object.fromEntries(Object.keys(FILTER_POLICIES[workspace] || {}).map((name) => [name, params.get(name)]));
  const filters = Object.fromEntries(projectFilters(workspace, rawFilters).map((item) => [item.name, item.value]));
  const lens = requestedAllowed ? params.get("lens") || filters.lens || "party" : "party";
  if ("lens" in filters) filters.lens = lens;
  const objectKey = requestedAllowed ? params.get("object") || params.get("session") || "" : "";
  const dialog = requestedAllowed && objectKey && params.get("dialog") === "detail" ? "detail" : "";
  const requestedTab = params.get("detail_tab") || "";
  return {
    workspace,
    objectKey,
    lens,
    filters,
    dialog,
    detailTab: dialog && DETAIL_TABS.has(requestedTab) ? requestedTab : "",
  };
}

export function writeLocation(state, {
  replace = false,
  currentLocation = globalThis.location,
  currentHistory = globalThis.history,
} = {}) {
  const params = new URLSearchParams();
  params.set("workspace", state.workspace);
  if (state.objectKey) params.set("object", state.objectKey);
  if (state.lens && state.lens !== "party") params.set("lens", state.lens);
  if (state.dialog === "detail" && state.objectKey) {
    params.set("dialog", "detail");
    if (DETAIL_TABS.has(state.detailTab)) params.set("detail_tab", state.detailTab);
  }
  for (const [name, policy] of Object.entries(FILTER_POLICIES[state.workspace] || {})) {
    if (name === "lens") continue;
    const value = state.filters?.[name];
    if (value !== undefined && value !== null && value !== "" && value !== policy.default) {
      params.set(name, String(value));
    }
  }
  currentHistory[replace ? "replaceState" : "pushState"](
    state,
    "",
    `${currentLocation?.pathname || ""}?${params}`,
  );
}

export function updateFilterState(state, name, value) {
  const policy = FILTER_POLICIES[state.workspace]?.[name];
  if (!policy) return state;
  const filters = { ...(state.filters || {}), [name]: value };
  if (policy.cursor_reset && name !== "cursor" && "cursor" in (FILTER_POLICIES[state.workspace] || {})) {
    filters.cursor = FILTER_POLICIES[state.workspace].cursor.default;
  }
  const next = { ...state, filters };
  if (name === "lens") next.lens = String(value || policy.default);
  return next;
}
