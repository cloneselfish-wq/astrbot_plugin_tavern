export const PHASES = Object.freeze(["idle", "loading", "refreshing", "ready", "empty", "error", "stale", "partial", "readonly", "permission", "conflict"]);

const SURFACE_TIMESTAMP_PRECISION_MS = 1000;

function safeLocalStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    // AstrBot plugin pages run in a sandboxed iframe without allow-same-origin.
    // Accessing localStorage there throws SecurityError before bootstrap starts.
    return null;
  }
}

function initialTheme() {
  const stored = safeLocalStorage()?.getItem("tavern.console.theme");
  if (stored === "light" || stored === "dark") return stored;
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function emptySurface() {
  return { phase: "idle", data: null, lastGood: null, envelope: null, permissions: {}, problems: [], requestId: "", sequence: null, updatedAt: "", lastSuccessAt: "", error: null, stale: false, readonly: false, retryable: false, retryAfterAt: null };
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
}

function signature(value) {
  return JSON.stringify(canonical(value ?? {}));
}

function responseSequence(payload, data) {
  for (const value of [payload?.sequence, payload?.latest_sequence, data?.sequence, data?.latest_sequence]) {
    if (Number.isSafeInteger(value) && value >= 0) return value;
  }
  return null;
}

function responseUpdatedAt(payload, data) {
  const value = payload?.updated_at ?? data?.updated_at;
  if (typeof value !== "string" || !value.trim()) return "";
  return Number.isFinite(Date.parse(value)) ? value : "";
}

function isOlderSurfaceResponse(state, sequence, updatedAt) {
  const currentSequence = Number.isSafeInteger(state.sequence) ? state.sequence : null;
  if (currentSequence !== null && sequence !== null) {
    if (sequence < currentSequence) return true;
    if (sequence > currentSequence) return false;
  }
  const currentTime = state.updatedAt ? Date.parse(state.updatedAt) : NaN;
  const responseTime = updatedAt ? Date.parse(updatedAt) : NaN;
  return Number.isFinite(currentTime)
    && Number.isFinite(responseTime)
    && currentTime - responseTime >= SURFACE_TIMESTAMP_PRECISION_MS;
}

function principalSecurityValue(principal = {}) {
  const roles = new Set(Array.isArray(principal.roles) ? principal.roles.map(String) : []);
  for (const [flag, role] of [
    ["is_admin", "admin"],
    ["is_author", "author"],
    ["is_host", "host"],
    ["is_player", "player"],
    ["is_readonly", "readonly"],
  ]) {
    if (principal[flag]) roles.add(role);
  }
  return {
    identity: String(principal.principalRef || principal.principal_ref || principal.username || "anonymous"),
    authenticated: principal.authenticated !== false,
    roles: [...roles].sort(),
    capabilities: canonical(principal.capabilities && typeof principal.capabilities === "object" ? principal.capabilities : {}),
    authSource: String(principal.auth_source || ""),
    roleSource: String(principal.role_source || ""),
  };
}

export function installSecurityScopeCleanup(store, {
  disposeActions = () => {},
  disposeWorkspace = () => {},
  stopEvents = () => {},
  clearSequences = () => {},
} = {}) {
  const listener = () => {
    for (const cleanup of [disposeActions, disposeWorkspace, stopEvents, clearSequences]) {
      try { cleanup(); } catch { /* security cleanup continues fail-closed */ }
    }
  };
  store.addEventListener("security-scope", listener);
  return () => store.removeEventListener("security-scope", listener);
}

export class ConsoleStore extends EventTarget {
  constructor() {
    super();
    this.app = {
      principal: null,
      authentication: "unknown",
      authorization: "ready",
      connection: "connecting",
      connectionDetail: {},
      theme: initialTheme(),
    };
    this.navigation = { domain: "live", workspace: "dashboard", objectKey: "", lens: "party", filter: "" };
    this.surfaces = new Map();
    this.intents = new Map();
    this.controllers = new Map();
  }

  principalScope() {
    const principal = this.app.principal ?? {};
    return String(principal.principalRef || principal.principal_ref || principal.username || "anonymous");
  }

  principalSignature(principal = this.app.principal) {
    return signature(principalSecurityValue(principal ?? {}));
  }

  surfaceKey(kind, scope = {}) {
    return [this.principalScope(), this.navigation.workspace, scope.objectKey ?? this.navigation.objectKey, kind, signature(scope.filters ?? {})].join("::");
  }

  surface(kind, scope) {
    const key = this.surfaceKey(kind, scope);
    if (!this.surfaces.has(key)) this.surfaces.set(key, emptySurface());
    return this.surfaces.get(key);
  }

  updateSurface(kind, patch, scope) {
    const state = this.surface(kind, scope);
    Object.assign(state, patch);
    this.emit("surface", { kind, key: this.surfaceKey(kind, scope), state });
    return state;
  }

  begin(kind, scope) {
    const state = this.surface(kind, scope);
    const previous = this.controllers.get(this.surfaceKey(kind, scope));
    previous?.abort();
    const controller = new AbortController();
    const requestId = crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`;
    this.controllers.set(this.surfaceKey(kind, scope), controller);
    this.updateSurface(kind, { phase: state.lastGood ? "refreshing" : "loading", requestId, error: null }, scope);
    return { controller, requestId };
  }

  resolve(kind, payload, requestId, scope) {
    const state = this.surface(kind, scope);
    if (state.requestId !== requestId) return false;
    const serverState = payload?.state;
    const data = payload?.data ?? payload;
    const sequence = responseSequence(payload, data);
    const updatedAt = responseUpdatedAt(payload, data);
    if (isOlderSurfaceResponse(state, sequence, updatedAt)) return false;
    const empty = serverState === "empty" || (Array.isArray(data?.items) && data.items.length === 0);
    const semanticStates = new Set(["ready", "empty", "stale", "partial", "readonly", "permission", "conflict"]);
    let phase = semanticStates.has(serverState) ? serverState : empty ? "empty" : "ready";
    if (phase === "ready" && payload?.stale) phase = "stale";
    const lastGood = phase === "permission" ? null : ["ready", "empty", "stale", "partial", "readonly"].includes(phase) ? data : state.lastGood;
    const permissions = payload?.permissions ?? data?.permissions ?? {};
    this.updateSurface(kind, { phase, data: phase === "permission" ? null : data, lastGood, envelope: payload, permissions, problems: Array.isArray(payload?.problems) ? payload.problems : [], sequence: sequence ?? state.sequence, updatedAt: updatedAt || state.updatedAt, lastSuccessAt: new Date().toISOString(), stale: Boolean(payload?.stale || phase === "stale"), readonly: Boolean(payload?.readonly || serverState === "readonly"), error: null, retryable: false, retryAfterAt: null }, scope);
    return true;
  }

  reject(kind, error, requestId, scope) {
    if (error?.name === "AbortError") return;
    const key = this.surfaceKey(kind, scope);
    const state = this.surfaces.get(key);
    if (!state) return;
    if (state.requestId !== requestId) return;
    if (error?.status === 401) {
      this.requireAuthentication(error);
      return;
    }
    const phase = error?.status === 403 ? "permission" : error?.status === 409 ? "conflict" : state.lastGood ? "stale" : "error";
    const retryAfterSeconds = Math.max(0, Number(error?.retryAfterSeconds || 0));
    const lastGood = phase === "permission" ? null : state.lastGood;
    this.updateSurface(kind, {
      phase,
      error,
      data: lastGood,
      lastGood,
      envelope: phase === "permission" ? null : state.envelope,
      permissions: phase === "permission" ? {} : state.permissions,
      problems: phase === "permission" ? [] : state.problems,
      stale: phase === "stale",
      retryable: Boolean(error?.retryable),
      retryAfterAt: retryAfterSeconds ? Date.now() + retryAfterSeconds * 1000 : null,
    }, scope);
  }

  setPrincipal(principal, { forceInvalidate = false, reason = "principal-or-permission-changed" } = {}) {
    const previousSignature = this.principalSignature();
    const nextSignature = this.principalSignature(principal);
    const signatureChanged = previousSignature !== nextSignature;
    const changed = signatureChanged || forceInvalidate;
    if (changed) this.invalidateSecurityScope(reason);
    this.app.principal = principal ?? null;
    this.app.authentication = principal && principal.authenticated !== false ? "ready" : "required";
    this.app.authorization = this.app.authentication === "ready" ? "ready" : "blocked";
    this.emit("principal", { principal: this.app.principal, changed, signatureChanged });
    return changed;
  }

  invalidateSecurityScope(reason = "security-scope-changed", { clearPrincipal = false } = {}) {
    this.controllers.forEach((controller) => {
      try { controller.abort(); } catch { /* already closed */ }
    });
    this.controllers.clear();
    this.surfaces.clear();
    this.intents.clear();
    Object.assign(this.navigation, { objectKey: "", lens: "party", filter: "", filters: {} });
    if (clearPrincipal) this.app.principal = null;
    this.emit("security-scope", { reason, clearPrincipal });
  }

  requireAuthentication(error = null) {
    this.invalidateSecurityScope("authentication-required", { clearPrincipal: true });
    this.app.authentication = "required";
    this.app.authorization = "blocked";
    this.setConnection("authentication-required");
    this.emit("authentication", { state: "required", error });
  }

  beginAuthorizationRecovery(error = null) {
    this.setAuthorization("checking", error);
    this.invalidateSecurityScope("authorization-recheck");
    return true;
  }

  setAuthentication(state, error = null) {
    this.app.authentication = state;
    this.emit("authentication", { state, error });
  }

  setAuthorization(state, error = null) {
    this.app.authorization = state;
    this.emit("authorization", { state, error });
  }

  setConnection(state, detail = {}) {
    const nextDetail = { ...detail };
    if (this.app.connection === state && signature(this.app.connectionDetail) === signature(nextDetail)) return false;
    this.app.connection = state;
    this.app.connectionDetail = nextDetail;
    this.emit("connection", { state, ...this.app.connectionDetail });
    return true;
  }

  navigate(next) {
    Object.assign(this.navigation, next);
    this.emit("navigation", this.navigation);
  }

  setTheme(theme) {
    this.app.theme = theme === "light" ? "light" : "dark";
    try { safeLocalStorage()?.setItem("tavern.console.theme", this.app.theme); } catch { /* in-memory theme remains usable */ }
    document.documentElement.dataset.theme = this.app.theme;
    this.emit("theme", this.app.theme);
  }

  emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}
