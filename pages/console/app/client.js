import { copy } from "../copy/catalog.js";

const inFlight = new Map();
export const SSE_RETRY_DELAYS_MS = Object.freeze([1000, 2000, 4000, 8000, 16000, 30000]);
const BRIDGE_PROBLEM_COPY = Object.freeze({
  authentication: { message: copy("client.error.authentication"), automatic: copy("client.automatic.authentication"), next_step: copy("client.next.authentication") },
  authorization: { message: copy("client.error.authorization"), automatic: copy("client.automatic.authorization"), next_step: copy("client.next.authorization") },
  conflict: { message: copy("client.error.conflict"), automatic: copy("client.automatic.conflict"), next_step: copy("client.next.conflict") },
  rate_limited: { message: copy("client.error.rate_limited"), automatic: copy("client.automatic.rate_limited"), next_step: copy("client.next.rate_limited") },
  timeout: { message: "", automatic: copy("client.automatic.timeout"), next_step: copy("client.next.timeout") },
  disconnect: { message: copy("client.error.disconnect"), automatic: copy("client.automatic.disconnect"), next_step: copy("client.next.disconnect") },
  server: { message: copy("client.error.server"), automatic: copy("client.automatic.server"), next_step: copy("client.next.server") },
  network: { message: copy("client.error.network"), automatic: copy("client.automatic.network"), next_step: copy("client.next.network") },
});

function activePluginPageBridge(candidate = globalThis.AstrBotPluginPage) {
  if (!candidate || typeof candidate !== "object") return null;
  if (typeof candidate.ready !== "function" || typeof candidate.apiGet !== "function" || typeof candidate.apiPost !== "function") return null;
  return candidate;
}

function bridgeEndpoint(path) {
  const normalized = String(path || "").trim().replace(/^\/+/, "");
  if (!normalized || normalized.includes("\\") || normalized.includes("://") || normalized.includes("?") || normalized.includes("#")) {
    throw new SafeProblem({ message: copy("client.error.request"), recovery: copy("client.recovery.retry"), code: "bridge.endpoint_invalid" });
  }
  return normalized;
}

function compactQuery(query = {}) {
  return Object.fromEntries(Object.entries(query || {}).filter(([, value]) => (
    value !== "" && value !== null && value !== undefined
  )));
}

function abortError() {
  return new DOMException("Aborted", "AbortError");
}

async function waitForBridge(promise, { signal, timeoutMs, operation } = {}) {
  if (signal?.aborted) throw abortError();
  let timer = null;
  let onAbort = null;
  const competitors = [Promise.resolve(promise)];
  if (signal) {
    competitors.push(new Promise((_, reject) => {
      onAbort = () => reject(abortError());
      signal.addEventListener("abort", onAbort, { once: true });
    }));
  }
  const timeout = Math.max(0, Number(timeoutMs) || 0);
  if (timeout) {
    competitors.push(new Promise((_, reject) => {
      timer = setTimeout(() => reject(new SafeProblem({
        operation,
        message: copy("client.error.timeout", { operation: String(operation || copy("client.operation.read")) }),
        recovery: copy("client.recovery.timeout"),
        code: "request.timeout",
        retryable: true,
      })), timeout);
    }));
  }
  try {
    return await Promise.race(competitors);
  } finally {
    if (timer !== null) clearTimeout(timer);
    if (signal && onAbort) signal.removeEventListener("abort", onAbort);
  }
}

export class SafeProblem extends Error {
  constructor({ operation, message, recovery, automatic = "", next_step = "", code = "request.failed", status = 0, retryable = false, retryAfterSeconds = 0 } = {}) {
    super(message || copy("client.error.request"));
    this.name = "SafeProblem";
    this.operation = operation || copy("client.operation.read");
    this.code = code;
    this.status = status;
    this.recovery = recovery === undefined ? copy("client.recovery.retry") : recovery;
    this.automatic = automatic;
    this.next_step = next_step;
    this.retryable = retryable;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

function bridgeProblem(error, operation, code, forcedKind = "") {
  const raw = String(error?.message || error?.response?.data?.message || error?.data?.message || "");
  const explicitStatus = Number(
    error?.status
    ?? error?.statusCode
    ?? error?.response?.status
    ?? error?.data?.status
  ) || 0;
  let kind = forcedKind;
  if (!kind && (explicitStatus === 401 || /(?:\b401\b|登录|身份)/u.test(raw))) kind = "authentication";
  if (!kind && (explicitStatus === 403 || /(?:\b403\b|权限|无权)/u.test(raw))) kind = "authorization";
  if (!kind && (explicitStatus === 409 || /(?:\b409\b|冲突|已被.*更新|较新的内容)/u.test(raw))) kind = "conflict";
  if (!kind && (explicitStatus === 429 || /(?:\b429\b|频繁|限流)/u.test(raw))) kind = "rate_limited";
  if (!kind && (explicitStatus === 504 || /(?:超时|timeout)/iu.test(raw))) kind = "timeout";
  if (!kind && /(?:中断|断开|连接已丢失|disconnected|failed to fetch|networkerror|socket|reset by peer|connection.*(?:lost|closed|reset)|load failed)/iu.test(raw)) kind = "disconnect";
  if (!kind && explicitStatus >= 500) kind = "server";
  // Timeouts are already represented by ``waitForBridge`` and real transport
  // failures normally carry an explicit connection message.  An otherwise
  // opaque bridge rejection is more safely described as a server failure than
  // as a proven disconnect.
  if (!kind) kind = "server";
  const statusByKind = { authentication: 401, authorization: 403, conflict: 409, rate_limited: 429, timeout: 504, server: explicitStatus >= 500 ? explicitStatus : 500 };
  const userCopy = BRIDGE_PROBLEM_COPY[kind];
  const semanticCode = kind === "disconnect" && !/disconnect|connection_lost/.test(code)
    ? `${code}.connection_lost`
    : kind === "timeout" && !/timeout/.test(code)
      ? `${code}.timeout`
      : code;
  return new SafeProblem({
    operation,
    message: kind === "timeout"
      ? copy("client.error.timeout", { operation: String(operation || copy("client.operation.read")) })
      : userCopy.message,
    recovery: "",
    automatic: userCopy.automatic,
    next_step: userCopy.next_step,
    code: semanticCode,
    status: statusByKind[kind] || 0,
    retryable: ["rate_limited", "timeout", "disconnect", "network", "server"].includes(kind),
    retryAfterSeconds: Number(error?.retryAfterSeconds) || 0,
  });
}

function envelopeProblem(response, payload, operation) {
  const raw = payload?.error ?? payload?.problems?.[0] ?? {};
  return new SafeProblem({
    operation,
    message: raw.message || copy("client.error.server_status", { status: response.status }),
    recovery: raw.recovery,
    automatic: raw.automatic,
    next_step: raw.next_step,
    code: raw.code,
    status: response.status,
    retryable: raw.retryable ?? (response.status >= 500 || response.status === 429),
    retryAfterSeconds: raw.retry_after_seconds ?? Number(response.headers.get("retry-after") || 0),
  });
}

export class BridgeClient {
  constructor({
    apiBase = "/api/console",
    contextPath = "/api/context",
    requestTimeoutMs = 30000,
    onAuthenticationRequired = null,
    onAuthorizationChanged = null,
    hostBridge = activePluginPageBridge(),
  } = {}) {
    this.apiBase = apiBase.replace(/\/$/, "");
    this.contextPath = contextPath;
    this.requestTimeoutMs = Math.max(0, Number(requestTimeoutMs) || 0);
    this.onAuthenticationRequired = onAuthenticationRequired;
    this.onAuthorizationChanged = onAuthorizationChanged;
    this.hostBridge = activePluginPageBridge(hostBridge);
  }

  async context(signal, { notifySecurity = false } = {}) {
    if (this.hostBridge) {
      const pageContext = await waitForBridge(this.hostBridge.ready(), {
        signal,
        timeoutMs: this.requestTimeoutMs,
        operation: copy("client.operation.read_identity"),
      });
      const principal = await this.requestBridge("GET", "dashboard/context", {
        signal,
        operation: copy("client.operation.read_identity"),
        dedupe: true,
      });
      return { ...(pageContext || {}), ...(principal || {}), standalone: false };
    }
    return this.requestAbsolute(this.contextPath, {
      signal,
      operation: copy("client.operation.read_identity"),
      notifySecurity,
    });
  }

  async get(path, { query = {}, signal, operation = copy("client.operation.read"), dedupe = true } = {}) {
    const compact = compactQuery(query);
    if (this.hostBridge) return this.requestBridge("GET", path, { query: compact, signal, operation, dedupe });
    const url = new URL(`${this.apiBase}/${path.replace(/^\//, "")}`, location.origin);
    Object.entries(compact).forEach(([key, value]) => url.searchParams.set(key, String(value)));
    return this.requestAbsolute(url.pathname + url.search, { signal, operation, dedupe });
  }

  async post(path, body, { signal, operation = copy("client.operation.write"), idempotencyKey = "" } = {}) {
    if (this.hostBridge) {
      const bridgeBody = body && typeof body === "object" ? { ...body } : {};
      if (idempotencyKey && !bridgeBody.idempotency_key) bridgeBody.idempotency_key = idempotencyKey;
      return this.requestBridge("POST", path, { body: bridgeBody, signal, operation, dedupe: false });
    }
    const headers = { "content-type": "application/json" };
    if (idempotencyKey) headers["x-idempotency-key"] = idempotencyKey;
    return this.requestAbsolute(`${this.apiBase}/${path.replace(/^\//, "")}`, {
      method: "POST", body: JSON.stringify(body ?? {}), headers, signal, operation, dedupe: false,
    });
  }

  async upload(path, file, { signal, operation = copy("client.operation.write"), idempotencyKey = "" } = {}) {
    if (!(file instanceof File)) {
      throw new SafeProblem({ operation, message: copy("client.error.file_required"), recovery: copy("client.recovery.choose_file") });
    }
    if (this.hostBridge) {
      try {
        const endpoint = bridgeEndpoint(path);
        const bridgeUploadEndpoint = idempotencyKey
          ? `${endpoint}${endpoint.includes("?") ? "&" : "?"}idempotency_key=${encodeURIComponent(idempotencyKey)}`
          : endpoint;
        return await waitForBridge(this.hostBridge.upload(bridgeUploadEndpoint, file), {
          signal,
          timeoutMs: this.requestTimeoutMs,
          operation,
        });
      } catch (error) {
        if (error?.name === "AbortError" || error instanceof SafeProblem) throw error;
        throw bridgeProblem(error, operation, "bridge.upload_failed");
      }
    }
    const body = new FormData();
    body.set("file", file, file.name);
    const headers = {};
    if (idempotencyKey) headers["x-idempotency-key"] = idempotencyKey;
    return this.requestAbsolute(`${this.apiBase}/${path.replace(/^\//, "")}`, {
      method: "POST", body, headers, signal, operation, dedupe: false,
    });
  }

  async requestAbsolute(path, options = {}) {
    const key = `${options.method || "GET"} ${path}`;
    if (options.dedupe !== false && inFlight.has(key)) return inFlight.get(key);
    const promise = (async () => {
      let response;
      const {
        operation,
        dedupe: _dedupe,
        notifySecurity = true,
        timeoutMs = this.requestTimeoutMs,
        ...fetchOptions
      } = options;
      const externalSignal = fetchOptions.signal;
      const requestController = new AbortController();
      let abortKind = "";
      const abortRequest = (kind) => {
        if (abortKind || requestController.signal.aborted) return;
        abortKind = kind;
        requestController.abort();
      };
      const onExternalAbort = () => abortRequest("external");
      if (externalSignal?.aborted) onExternalAbort();
      else externalSignal?.addEventListener?.("abort", onExternalAbort, { once: true });
      const timeout = Math.max(0, Number(timeoutMs) || 0);
      const timeoutTimer = timeout ? setTimeout(() => abortRequest("timeout"), timeout) : null;
      fetchOptions.signal = requestController.signal;
      let payload = null;
      try {
        response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...fetchOptions });
        try {
          payload = await response.json();
        } catch (error) {
          if (abortKind || error?.name === "AbortError") throw error;
          payload = null;
        }
      } catch (error) {
        if (abortKind === "external") throw error?.name === "AbortError" ? error : new DOMException("Aborted", "AbortError");
        if (abortKind === "timeout" || error?.name === "AbortError") {
          const operationName = String(operation || copy("client.operation.read"));
          throw new SafeProblem({
            operation: operationName,
            message: copy("client.error.timeout", { operation: operationName }),
            recovery: copy("client.recovery.timeout"),
            code: "request.timeout",
            status: 0,
            retryable: true,
          });
        }
        throw new SafeProblem({ operation, message: copy("client.error.network"), recovery: copy("client.recovery.network"), retryable: true });
      } finally {
        if (timeoutTimer !== null) clearTimeout(timeoutTimer);
        externalSignal?.removeEventListener?.("abort", onExternalAbort);
      }
      if (!response.ok) {
        const problem = envelopeProblem(response, payload, operation);
        if (notifySecurity !== false) {
          const details = { method: String(fetchOptions.method || "GET").toUpperCase(), path: String(path) };
          if (problem.status === 401) this.notifySecurity(this.onAuthenticationRequired, problem, details);
          if (problem.status === 403) this.notifySecurity(this.onAuthorizationChanged, problem, details);
        }
        throw problem;
      }
      return payload ?? {};
    })();
    if (options.dedupe !== false) inFlight.set(key, promise);
    try { return await promise; } finally { if (inFlight.get(key) === promise) inFlight.delete(key); }
  }

  async requestBridge(method, path, { query = {}, body = {}, signal, operation = copy("client.operation.read"), dedupe = true } = {}) {
    const endpoint = bridgeEndpoint(path);
    const key = `BRIDGE ${method} ${endpoint} ${method === "GET" ? JSON.stringify(query || {}) : ""}`;
    if (dedupe !== false && inFlight.has(key)) return inFlight.get(key);
    const promise = (async () => {
      try {
        const request = method === "POST"
          ? this.hostBridge.apiPost(endpoint, body ?? {})
          : this.hostBridge.apiGet(endpoint, query ?? {});
        return await waitForBridge(request, { signal, timeoutMs: this.requestTimeoutMs, operation });
      } catch (error) {
        if (error?.name === "AbortError" || error instanceof SafeProblem) throw error;
        throw bridgeProblem(error, operation, "bridge.request_failed");
      }
    })();
    if (dedupe !== false) inFlight.set(key, promise);
    try { return await promise; } finally { if (inFlight.get(key) === promise) inFlight.delete(key); }
  }

  events({ after = 0, sessionKey = "", onEvent, onError }) {
    if (this.hostBridge && typeof this.hostBridge.subscribeSSE === "function") {
      let active = true;
      let subscriptionId = "";
      void this.hostBridge.subscribeSSE("dashboard/events", {
        onMessage: (event) => {
          if (!active) return;
          try {
            const payload = event?.parsed ?? JSON.parse(String(event?.raw ?? "{}"));
            onEvent?.(payload);
          } catch (error) {
            onError?.(error);
          }
        },
        onError: () => {
          if (active) onError?.(bridgeProblem(null, copy("client.operation.read"), "bridge.event_stream_disconnected", "disconnect"));
        },
      }, {
        after_seq: String(Math.max(0, Number(after) || 0)),
        session_key: String(sessionKey || ""),
      }).then((value) => {
        subscriptionId = String(value || "");
        if (!active && subscriptionId) void this.hostBridge.unsubscribeSSE?.(subscriptionId);
      }).catch((error) => { if (active) onError?.(error); });
      return () => {
        active = false;
        if (subscriptionId) void this.hostBridge.unsubscribeSSE?.(subscriptionId);
      };
    }
    const query = new URLSearchParams({
      after_seq: String(Math.max(0, Number(after) || 0)),
      session_key: String(sessionKey || ""),
    });
    const url = `${this.apiBase}/dashboard/events?${query.toString()}`;
    const stream = new EventSource(url, { withCredentials: true });
    let ended = false;
    stream.onmessage = (event) => {
      if (ended) return;
      try {
        onEvent?.(JSON.parse(event.data));
      } catch (error) {
        ended = true;
        stream.close();
        onError?.(error);
      }
    };
    stream.onerror = (error) => {
      if (ended) return;
      ended = true;
      stream.close();
      onError?.(error);
    };
    return () => {
      if (ended) return;
      ended = true;
      stream.close();
    };
  }

  notifySecurity(handler, problem, details) {
    if (typeof handler !== "function") return;
    try {
      const pending = handler(problem, details);
      pending?.catch?.(() => {});
    } catch {
      // The original SafeProblem remains authoritative for the request caller.
    }
  }
}

export class RecoveringEventStream {
  constructor({
    client,
    sessionKey,
    getAfter = () => 0,
    onEvent = () => {},
    onError = () => {},
    onState = () => {},
    setTimer = (callback, delay) => setTimeout(callback, delay),
    clearTimer = (timer) => clearTimeout(timer),
    retryDelays = SSE_RETRY_DELAYS_MS,
  } = {}) {
    this.client = client;
    this.sessionKey = String(sessionKey || "");
    this.getAfter = getAfter;
    this.onEvent = onEvent;
    this.onError = onError;
    this.onState = onState;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.retryDelays = [...retryDelays].map((value) => Math.max(0, Number(value) || 0));
    if (!this.retryDelays.length) this.retryDelays = [...SSE_RETRY_DELAYS_MS];
    this.stopped = true;
    this.attempt = 0;
    this.timer = null;
    this.closeStream = null;
    this.serial = 0;
    this.activeSerial = 0;
  }

  start() {
    if (!this.client || !this.sessionKey || !this.stopped) return;
    this.stopped = false;
    this.attempt = 0;
    this.connect(false);
  }

  retryNow() {
    if (this.stopped) return;
    this.clearReconnectTimer();
    this.closeActive();
    this.connect(true);
  }

  stop() {
    if (this.stopped) return;
    this.stopped = true;
    this.clearReconnectTimer();
    this.closeActive();
  }

  connect(manual) {
    if (this.stopped) return;
    this.clearReconnectTimer();
    this.closeActive();
    const serial = ++this.serial;
    this.activeSerial = serial;
    this.onState(this.attempt > 0 || manual ? "reconnecting" : "connecting", {
      attempt: this.attempt,
      nextRetryMs: 0,
      manual: Boolean(manual),
    });
    const after = Math.max(0, Number(this.getAfter() || 0));
    this.closeStream = this.client.events({
      after,
      sessionKey: this.sessionKey,
      onEvent: (event) => this.handleEvent(event, serial),
      onError: (error) => this.handleError(error, serial),
    });
  }

  handleEvent(event, serial) {
    if (this.stopped || serial !== this.activeSerial) return;
    this.attempt = 0;
    this.clearReconnectTimer();
    this.onState("healthy", { attempt: 0, nextRetryMs: 0 });
    try {
      const pending = this.onEvent(event);
      pending?.catch?.((error) => this.onError(error, { processing: true }));
    } catch (error) {
      this.onError(error, { processing: true });
    }
  }

  handleError(error, serial) {
    if (this.stopped || serial !== this.activeSerial) return;
    this.closeActive();
    this.attempt += 1;
    const delay = this.retryDelays[Math.min(this.attempt - 1, this.retryDelays.length - 1)];
    this.onState("degraded", { attempt: this.attempt, nextRetryMs: delay });
    this.onError(error, { processing: false, attempt: this.attempt, nextRetryMs: delay });
    if (this.timer !== null) return;
    this.timer = this.setTimer(() => {
      this.timer = null;
      this.connect(false);
    }, delay);
  }

  closeActive() {
    this.activeSerial = 0;
    const close = this.closeStream;
    this.closeStream = null;
    close?.();
  }

  clearReconnectTimer() {
    if (this.timer === null) return;
    this.clearTimer(this.timer);
    this.timer = null;
  }
}
