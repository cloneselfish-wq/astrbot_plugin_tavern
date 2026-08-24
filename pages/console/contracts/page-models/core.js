import { scanPrivateFields, safeText } from "../../app/security.js";
import { copy } from "../../copy/catalog.js";
import { optionalSectionFallback, projectFilters, requiredSectionFailure } from "../page-model-policy.js";

const ACTION_DESCRIPTOR_KEYS = new Set([
  "action_id", "intent", "label", "target_kind", "description",
  "expected_revision", "transportReady", "focus_return", "fields",
  "object_key", "target_key",
]);
const SAFE_OPAQUE_ACTION_KEY = /^[a-z0-9]{1,12}_[A-Za-z0-9_-]{12}$/;

function isGenericRevisionKey(key) {
  const normalized = String(key || "").trim().toLowerCase();
  return normalized === "revision"
    || normalized.startsWith("revision_")
    || normalized.endsWith("_revision");
}

function isActionDescriptor(value) {
  const expected = value?.expected_revision;
  return typeof value?.action_id === "string" && value.action_id.trim()
    && typeof value.intent === "string" && value.intent.trim()
    && typeof value.target_kind === "string" && value.target_kind.trim()
    && value.transportReady === true
    && ((Number.isInteger(expected) && expected >= 0) || (typeof expected === "string" && expected.trim()));
}

export function publicValue(value) {
  if (value === undefined) return undefined;
  if (Array.isArray(value)) return value.slice(0, 100).map(publicValue).filter((item) => item !== undefined);
  if (value && typeof value === "object") {
    const descriptor = Boolean(isActionDescriptor(value));
    return Object.fromEntries(Object.entries(value).map(([key, item]) => {
      if (descriptor && !ACTION_DESCRIPTOR_KEYS.has(key)) return [key, undefined];
      if (descriptor && ["object_key", "target_key"].includes(key)
        && !SAFE_OPAQUE_ACTION_KEY.test(String(item || ""))) return [key, undefined];
      if (isGenericRevisionKey(key) && !(descriptor && key === "expected_revision")) return [key, undefined];
      if (!(descriptor && ["action_id", "object_key", "target_key"].includes(key))
        && scanPrivateFields({ [key]: null }).length) return [key, undefined];
      return [key, publicValue(item)];
    }).filter(([, item]) => item !== undefined));
  }
  return value;
}

export function firstSource(paths, data, envelope) {
  for (const path of paths || []) {
    const value = path === "$summary" ? envelope.summary : path === "$problems" ? envelope.problems : data[path];
    if (value !== undefined && value !== null) return value;
  }
  return undefined;
}

function projectedFilters(workspace, envelope, data, filterSources, runtime = {}) {
  const navigationFilters = runtime?.navigation?.filters && typeof runtime.navigation.filters === "object"
    ? runtime.navigation.filters
    : {};
  const source = { ...navigationFilters, ...(envelope.filters || {}) };
  const options = data.filters && typeof data.filters === "object" ? data.filters : {};
  for (const [name, optionKey] of Object.entries(filterSources || {})) {
    const rows = optionKey === "world_options" ? data.world_options : options[optionKey];
    if (!Array.isArray(rows)) continue;
    const current = source[name] && typeof source[name] === "object" ? source[name].value : source[name];
    source[name] = { value: current, options: rows };
  }
  return projectFilters(workspace, source);
}

export function createPageAdapter({ workspace, spec, paths, filterSources = {}, resolveSection }) {
  return (envelope = {}, runtime = {}) => {
    const data = envelope.data && typeof envelope.data === "object" ? envelope.data : {};
    const problems = Array.isArray(envelope.problems) ? publicValue(envelope.problems) || [] : [];
    const sections = spec.map(([id, component, required], order) => {
      let value = resolveSection ? resolveSection(id, data, envelope, runtime) : firstSource(paths[id], data, envelope);
      let missing = false;
      const failure = requiredSectionFailure(workspace, id);
      const fallback = optionalSectionFallback(workspace, id);
      value = publicValue(value);
      if (required && value === undefined) {
        missing = true;
        value = publicValue(failure?.fallback_value);
        problems.push({
          section: id,
          operation: failure?.label || copy("contracts.page_models.adapt.message.3724eb71e9"),
          message: failure?.message || copy("contracts.page_models.adapt.message.503e4b0641"),
          recovery: failure?.recovery || copy("contracts.page_models.adapt.message.5448ceb91a"),
          code: "page.section_missing",
          retryable: true,
        });
      } else if (!required && (value === undefined || value === null) && fallback?.include_when_missing) {
        value = publicValue(fallback.fallback_value);
      }
      return { id, component, required, order, value, missing, failure };
    }).filter((section) => section.required || section.value !== undefined);
    const missing = sections.some((section) => section.missing);
    const phase = missing ? "partial" : envelope.state || (sections.every((section) => Array.isArray(section.value) && !section.value.length) ? "empty" : "ready");
    return {
      workspace,
      phase,
      title: safeText(envelope.summary?.label, workspace),
      summary: safeText(envelope.summary?.text || envelope.summary?.summary),
      updatedAt: safeText(envelope.updated_at),
      stale: Boolean(envelope.stale),
      readonly: Boolean(envelope.readonly),
      permissions: publicValue(envelope.permissions) || {},
      filters: projectedFilters(workspace, envelope, data, filterSources, runtime),
      pagination: publicValue(data.pagination) || null,
      sections,
      actions: publicValue(data.available_actions || envelope.available_actions) || [],
      problems,
    };
  };
}
