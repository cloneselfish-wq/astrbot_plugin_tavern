import { BridgeClient } from "./client.js";
import { readLocation, writeLocation } from "./router.js";
import { copy } from "../copy/catalog.js";
import { canAccess, WORKSPACES } from "../shell/registry.js";
import { renderShell } from "../shell/app-shell.js";
import { ConsoleStore } from "./store.js";
import { PAGE_MODEL_ADAPTERS } from "../contracts/page-models.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { renderWorkspaceSkeleton } from "../components/page-skeleton.js";
import { pageRenderer, renderSessionSelection } from "../pages/index.js";
import { ActionController } from "./actions.js";
import { DialogManager } from "../dialogs/dialog-manager.js";

const root = document.getElementById("tavern-app");
const store = new ConsoleStore();
const client = new BridgeClient({
  onAuthenticationRequired: (error) => store.requireAuthentication(error),
  onAuthorizationChanged: (error) => store.beginAuthorizationRecovery(error),
});
let principal = null;
let navigation = null;
let shell = null;
let activePage = null;
let activeWorkspace = "";
const navigationBadges = new Map();
const dialogs = new DialogManager({
  onUrlState: (state) => {
    if (!navigation) return;
    if (state === null) {
      updateLocation({ dialog: "", detailTab: "" });
      return;
    }
    updateLocation({
      dialog: state.dialog || (state.tab ? "detail" : navigation.dialog),
      detailTab: state.tab || navigation.detailTab || "",
      objectKey: state.objectKey || navigation.objectKey,
    });
  },
});
const actions = new ActionController({ client, dialogs, announce: (message) => { const region = document.getElementById("tavern-live-region"); if (region) region.textContent = message; }, refresh: () => loadWorkspace() });

function setConnection(state, detail = {}) {
  store.setConnection(state, detail);
  if (!shell?.status) return;
  shell.setConnection?.(state);
}

function rememberNavigationBadges(envelope, model) {
  const explicit = envelope?.data?.navigation_badges;
  if (explicit && typeof explicit === "object") {
    for (const [workspace, value] of Object.entries(explicit)) {
      if (WORKSPACES[workspace] && Number.isInteger(Number(value)) && Number(value) >= 0) navigationBadges.set(workspace, Number(value));
    }
  }
  const counted = {
    sessions: "groups", todo: "actionable", characters: "review_cards", author_jobs: "jobs",
  }[navigation.workspace];
  if (counted) {
    const current = model.sections?.find((section) => section.id === counted)?.value;
    const rows = Array.isArray(current) ? current : Array.isArray(current?.items) ? current.items : [];
    const total = Number(model.pagination?.total);
    navigationBadges.set(navigation.workspace, Number.isInteger(total) && total >= rows.length ? total : rows.length);
  }
  for (const [workspace, value] of navigationBadges) shell.setBadge?.(workspace, value);
}

function sameNavigation(left, right) {
  if (!left || !right) return false;
  return left.workspace === right.workspace
    && String(left.objectKey || "") === String(right.objectKey || "")
    && String(left.lens || "") === String(right.lens || "")
    && JSON.stringify(left.filters || {}) === JSON.stringify(right.filters || {});
}

async function renderEnvelope(envelope, targetNavigation, targetShell) {
  const model = PAGE_MODEL_ADAPTERS[targetNavigation.workspace](envelope, {
    navigation: targetNavigation,
  });
  const renderer = await pageRenderer(targetNavigation.workspace);
  if (targetShell !== shell || !sameNavigation(targetNavigation, navigation)) return null;
  const page = renderer(model, {
    navigate: navigateTo,
    canNavigate: (workspace) => canAccess(workspace, principal),
    updateLocation,
    actions,
    client,
    dialogs,
    navigation: targetNavigation,
    refresh: loadWorkspace,
    store,
  });
  activePage?.dispose?.();
  activePage = page;
  activeWorkspace = targetNavigation.workspace;
  targetShell.main.replaceChildren(page);
  return model;
}

async function navigateTo(workspace, {
  objectKey = "",
  lens = "party",
  filters = {},
} = {}) {
  const next = { workspace, objectKey, lens, filters };
  writeLocation(next);
  navigation = readLocation(principal);
  store.navigate(navigation);
  await mount();
}

function updateLocation(patch, { replace = true } = {}) {
  const filters = { ...(navigation.filters || {}), ...(patch.filters || {}) };
  if (patch.lens) filters.lens = patch.lens;
  navigation = { ...navigation, ...patch, filters };
  writeLocation(navigation, { replace });
  store.navigate(navigation);
  return navigation;
}

async function mount() {
  dialogs.close("navigation", { force: true, restoreFocus: false, updateUrl: false });
  if (!shell) {
    shell = renderShell(root, principal, navigation, {
      navigate: (workspace) => navigateTo(workspace),
      refresh: () => typeof activePage?.refresh === "function" ? activePage.refresh() : loadWorkspace(),
      theme: () => store.setTheme(store.app.theme === "light" ? "dark" : "light"),
      connection: store.app.connection,
      badges: navigationBadges,
    });
  } else {
    shell.setNavigation?.(navigation);
  }
  if (activePage && activeWorkspace !== navigation.workspace) {
    activePage.dispose?.();
    activePage = null;
    activeWorkspace = "";
  }
  await loadWorkspace();
}

function failurePhase(error) {
  if (error?.status === 401 || error?.status === 403) return "permission";
  if (error?.status === 409) return "conflict";
  if (error?.status === 429) return "rate_limited";
  if (error?.status === 503) return "unavailable";
  const code = String(error?.code || "").toLowerCase();
  if (error?.name === "TimeoutError" || code.includes("timeout")) return "timeout";
  if (code.includes("disconnect") || code.includes("connection_lost")) return "disconnect";
  return "error";
}

async function loadWorkspace() {
  const targetNavigation = {
    ...navigation,
    filters: { ...(navigation.filters || {}) },
  };
  const targetShell = shell;
  targetShell.refresh.disabled = true;
  targetShell.setRefreshState?.("refreshing");

  if (targetNavigation.workspace === "session_detail" && !targetNavigation.objectKey) {
    activePage?.dispose?.();
    activePage = renderSessionSelection({
      navigate: navigateTo,
      canNavigate: (workspace) => canAccess(workspace, principal),
    });
    activeWorkspace = "session_detail";
    targetShell.main.replaceChildren(activePage);
    targetShell.setRefreshState?.("complete");
    targetShell.refresh.disabled = false;
    return;
  }

  targetShell.main.setAttribute("aria-busy", "true");
  let activeRequest = null;
  try {
    const query = { ...(targetNavigation.filters || {}) };
    if (targetNavigation.objectKey) query.session_key = targetNavigation.objectKey;
    const scope = { objectKey: targetNavigation.objectKey, filters: query };
    const cached = store.surface(targetNavigation.workspace, scope);
    if (!activePage && cached.envelope) {
      await renderEnvelope(cached.envelope, targetNavigation, targetShell);
    }
    if (activePage) activePage.dataset.refreshing = "true";
    else targetShell.main.replaceChildren(
      renderWorkspaceSkeleton(WORKSPACES[targetNavigation.workspace].label),
    );
    const { controller, requestId } = store.begin(targetNavigation.workspace, scope);
    activeRequest = { scope, requestId };
    const envelope = await client.get(WORKSPACES[targetNavigation.workspace].endpoint, {
      query,
      signal: controller.signal,
      operation: WORKSPACES[targetNavigation.workspace].label,
    });
    if (targetShell !== shell || !sameNavigation(targetNavigation, navigation)) return;
    if (!store.resolve(targetNavigation.workspace, envelope, requestId, scope)) return;
    const model = await renderEnvelope(envelope, targetNavigation, targetShell);
    if (!model) return;
    rememberNavigationBadges(envelope, model);
    setConnection("connected");
    targetShell.setRefreshState?.("complete");
  } catch (error) {
    if (error?.name === "AbortError") return;
    if (targetShell !== shell || !sameNavigation(targetNavigation, navigation)) return;
    const scope = activeRequest?.scope || { objectKey: targetNavigation.objectKey, filters: {} };
    const state = store.surface(targetNavigation.workspace, scope);
    store.reject(targetNavigation.workspace, error, activeRequest?.requestId || state.requestId, scope);
    if (error?.status === 401) setConnection("authentication-required", { error });
    else if (error?.status !== 403) setConnection("degraded", { error });
    const phase = state.lastGood ? "stale" : failurePhase(error);
    const problem = renderStatePanel({
      phase,
      operation: WORKSPACES[targetNavigation.workspace].label,
      problem: error,
      lastGood: state.lastGood,
      retryAction: () => loadWorkspace(),
    });
    if (state.lastGood && activePage) {
      problem.classList.add("tavern-transient-problem");
      targetShell.main.replaceChildren(problem, activePage);
    } else {
      activePage?.dispose?.();
      activePage = null;
      activeWorkspace = "";
      targetShell.main.replaceChildren(problem);
    }
    targetShell.setRefreshState?.("failed");
  } finally {
    if (targetShell === shell) {
      activePage?.removeAttribute?.("data-refreshing");
      targetShell.main.setAttribute("aria-busy", "false");
      targetShell.refresh.disabled = false;
    }
  }
}

async function bootstrap() {
  if (!shell) {
    navigation = readLocation({ roles: ["readonly"] });
    shell = renderShell(root, { roles: ["readonly"] }, navigation, {
      navigate: () => {}, refresh: () => bootstrap(), theme: () => {},
      connection: "connecting", badges: navigationBadges,
    });
    shell.main.replaceChildren(renderWorkspaceSkeleton(copy("app.bootstrap.rc8.032a42a13b")));
  }
  try {
    principal = await client.context(undefined, { notifySecurity: true });
    store.setPrincipal(principal);
    navigation = readLocation(principal);
    store.navigate(navigation);
    setConnection("connected");
    root.replaceChildren();
    shell = null;
    await mount();
  } catch (error) {
    if (error?.status === 401) store.requireAuthentication(error);
    setConnection(error?.status === 401 ? "authentication-required" : "degraded", { error });
    root.replaceChildren(renderStatePanel({
      phase: error?.status === 401 ? "permission" : "error",
      operation: copy("app.bootstrap.bootstrap.operation.7622b58750"),
      problem: error,
      retryAction: () => bootstrap(),
      emptyCopy: `${copy("shell.failure")} ${copy("shell.failure.next")}`,
    }));
  }
}

window.addEventListener("popstate", async () => {
  navigation = readLocation(principal);
  store.navigate(navigation);
  await mount();
});
bootstrap();
