import { copy } from "../copy/catalog.js";
const filter = (type, defaultValue, {
  minimum = null,
  maximum = null,
  values = [],
} = {}) => Object.freeze({
  type,
  default: defaultValue,
  minimum,
  maximum,
  values: Object.freeze([...values]),
});

const FILTER_TYPES = Object.freeze({
  q: filter("search", ""),
  status: filter("select", ""),
  world: filter("select", ""),
  group: filter("select", ""),
  author: filter("select", ""),
  capability: filter("select", ""),
  scope: filter("select", ""),
  importance: filter("select", ""),
  tag: filter("search", ""),
  governance: filter("select", ""),
  mode: filter("select", ""),
  object: filter("search", ""),
  actor: filter("search", ""),
  action: filter("select", ""),
  time: filter("select", ""),
  layer: filter("select", ""),
  consumer: filter("select", ""),
  type: filter("select", ""),
  cursor: filter("cursor", ""),
  page_size: filter("integer", 25, { minimum: 1, maximum: 100 }),
  round: filter("integer", null, { minimum: 0 }),
  session_key: filter("opaque-key", ""),
  world_key: filter("opaque-key", ""),
  object_key: filter("opaque-key", ""),
  // Lens keys are compiled from the installed world's ui_profile.  The
  // router validates a bounded identifier here; the live renderer then
  // intersects it with the server-projected lens list.
  lens: filter("identifier", "party"),
});

const PAGE_FILTER_NAMES = Object.freeze({
  dashboard: [],
  sessions: ["q", "status", "world", "group", "cursor", "page_size"],
  todo: ["q", "status", "cursor", "page_size"],
  tendencies: ["session_key"],
  characters: ["session_key", "q", "status", "cursor", "page_size"],
  worlds: ["q", "status", "author", "capability", "cursor", "page_size"],
  designer: ["world_key", "object_key"],
  author_jobs: ["world_key", "q", "status", "type", "time", "cursor", "page_size"],
  session_detail: ["session_key", "lens", "q", "round", "actor", "type", "cursor", "page_size"],
  memories: ["session_key", "q", "scope", "importance", "tag", "governance", "cursor", "page_size"],
  audit: ["session_key", "q", "status", "object", "actor", "action", "time", "cursor", "page_size"],
  health: ["session_key"],
  settings: ["group"],
  modules: ["q", "status", "layer", "consumer", "cursor", "page_size"],
  about: [],
});

const PAGE_FILTER_OVERRIDES = Object.freeze({
  settings: Object.freeze({
    group: filter("enum", "permissions", {
      values: ["permissions", "model", "context", "time", "recovery", "panel"],
    }),
  }),
});

function workspaceFilters(workspace, names) {
  return Object.freeze(Object.fromEntries(names.map((name) => {
    const base = PAGE_FILTER_OVERRIDES[workspace]?.[name] || FILTER_TYPES[name];
    if (!base) throw new Error(`No filter contract for ${workspace}.${name}`);
    return [name, Object.freeze({
      ...base,
      permission: `${workspace}.view`,
      cursor_reset: name !== "cursor",
    })];
  })));
}

export const FILTER_POLICIES = Object.freeze(Object.fromEntries(
  Object.entries(PAGE_FILTER_NAMES).map(([workspace, names]) => [
    workspace,
    workspaceFilters(workspace, names),
  ]),
));

const required = (label, message, recovery, fallbackValue) => Object.freeze({
  label,
  missing_phase: "partial",
  message,
  recovery,
  fallback_value: fallbackValue,
  synthetic_business_data: false,
});

export const REQUIRED_SECTION_FAILURES = Object.freeze({
  dashboard: Object.freeze({
    sessions: required(
      copy("contracts.page_model_policy.required.message.3ecd42d2d9"),
      copy("contracts.page_model_policy.required.message.8fe1933685"),
      copy("contracts.page_model_policy.required.message.4b6d320b37"),
      [],
    ),
  }),
  sessions: Object.freeze({
    groups: required(copy("contracts.page_model_policy.required.message.8cfa6649a3"), copy("contracts.page_model_policy.required.message.bf08a3dd5f"), copy("contracts.page_model_policy.required.message.9668ac9abf"), []),
  }),
  todo: Object.freeze({
    actionable: required(copy("contracts.page_model_policy.required.message.5e415ae2bb"), copy("contracts.page_model_policy.required.message.0e12add0f0"), copy("contracts.page_model_policy.required.message.600d638f5e"), []),
  }),
  tendencies: Object.freeze({
    privacy: required(copy("contracts.page_model_policy.required.message.320990bd50"), copy("contracts.page_model_policy.required.message.3eba2bf713"), copy("contracts.page_model_policy.required.message.06eb573447"), {}),
    observations: required(copy("contracts.page_model_policy.required.message.eaf5181782"), copy("contracts.page_model_policy.required.message.494bb61872"), copy("contracts.page_model_policy.required.message.0617b7fa00"), []),
  }),
  characters: Object.freeze({
    review_cards: required(copy("contracts.page_model_policy.required.message.ec300fe187"), copy("contracts.page_model_policy.required.message.d7341b007d"), copy("contracts.page_model_policy.required.message.58f2317b03"), []),
  }),
  worlds: Object.freeze({
    world_cards: required(copy("contracts.page_model_policy.required.message.8cbd5ffd49"), copy("contracts.page_model_policy.required.message.acf9a71c5b"), copy("contracts.page_model_policy.required.message.29a44a1418"), []),
  }),
  designer: Object.freeze({
    context: required(copy("contracts.page_model_policy.required.message.eaeb4b7f90"), copy("contracts.page_model_policy.required.message.2ef1734c01"), copy("contracts.page_model_policy.required.message.b84bbdce45"), {}),
    flow: required(copy("contracts.page_model_policy.required.message.a23a87f3eb"), copy("contracts.page_model_policy.required.message.21d3d161f4"), copy("contracts.page_model_policy.required.message.5813b02a1a"), []),
  }),
  author_jobs: Object.freeze({
    jobs: required(copy("contracts.page_model_policy.required.message.5c8d5c1278"), copy("contracts.page_model_policy.required.message.e7bd8dba01"), copy("contracts.page_model_policy.required.message.5d7e353370"), []),
  }),
  session_detail: Object.freeze({
    story: required(copy("contracts.page_model_policy.required.message.4771a6ddfd"), copy("contracts.page_model_policy.required.message.a75c5f2688"), copy("contracts.page_model_policy.required.message.b50b1c0d7b"), {}),
    turn: required(copy("contracts.page_model_policy.required.message.7714978461"), copy("contracts.page_model_policy.required.message.748f187ee1"), copy("contracts.page_model_policy.required.message.83b1557f8c"), {}),
    lens: required(copy("contracts.page_model_policy.required.message.0e3228bf40"), copy("contracts.page_model_policy.required.message.f1e7bf9426"), copy("contracts.page_model_policy.required.message.5f33b18763"), {}),
  }),
  memories: Object.freeze({
    facts: required(copy("contracts.page_model_policy.required.message.b80e99482a"), copy("contracts.page_model_policy.required.message.e93786786c"), copy("contracts.page_model_policy.required.message.a7e75a272d"), []),
  }),
  audit: Object.freeze({}),
  health: Object.freeze({
    services: required(copy("contracts.page_model_policy.required.message.e8a4f7c09d"), copy("contracts.page_model_policy.required.message.e7e54117a5"), copy("contracts.page_model_policy.required.message.f755a0390a"), []),
  }),
  settings: Object.freeze({
    groups: required(copy("contracts.page_model_policy.required.message.4a48d745e3"), copy("contracts.page_model_policy.required.message.2384e21a23"), copy("contracts.page_model_policy.required.message.2a625a0a83"), []),
  }),
  modules: Object.freeze({
    modules: required(copy("contracts.page_model_policy.required.message.b9c928ec20"), copy("contracts.page_model_policy.required.message.decba20147"), copy("contracts.page_model_policy.required.message.aa99530626"), []),
  }),
  about: Object.freeze({
    version: required(copy("contracts.page_model_policy.required.message.2da3906a24"), copy("contracts.page_model_policy.required.message.54b3d9d932"), copy("contracts.page_model_policy.required.message.63e62f731c"), {}),
    support: required(copy("contracts.page_model_policy.required.message.a095dd139f"), copy("contracts.page_model_policy.required.message.1d1aa27384"), copy("contracts.page_model_policy.required.message.0b70dde4cf"), {}),
  }),
});

export const OPTIONAL_SECTION_FALLBACKS = Object.freeze({
  session_detail: Object.freeze({
    decision: Object.freeze({
      include_when_missing: true,
      fallback_value: Object.freeze({
        choices: Object.freeze([]),
        actions: Object.freeze([]),
        empty: true,
        message: copy("contracts.page_model_policy.required.message.393a70043d"),
      }),
    }),
  }),
});

function rawFilterValue(source, name) {
  const candidate = source?.[name];
  if (candidate && typeof candidate === "object" && !Array.isArray(candidate) && "value" in candidate) {
    return candidate.value;
  }
  return candidate;
}

function normalizeFilterValue(policy, value) {
  if (value === undefined || value === null || value === "") {
    return policy.default;
  }
  if (policy.type === "integer") {
    const parsed = Number(value);
    if (!Number.isInteger(parsed)) return policy.default;
    if (policy.minimum !== null && parsed < policy.minimum) return policy.default;
    if (policy.maximum !== null && parsed > policy.maximum) return policy.maximum;
    return parsed;
  }
  const text = String(value);
  if (policy.type === "identifier" && !/^[a-z][a-z0-9_-]{0,31}$/.test(text)) {
    return policy.default;
  }
  if (policy.type === "enum" && !policy.values.includes(text)) {
    return policy.default;
  }
  return text;
}

export function projectFilters(workspace, source = {}) {
  const policies = FILTER_POLICIES[workspace] || {};
  return Object.entries(policies).map(([name, policy]) => {
    const raw = source?.[name];
    const options = raw && typeof raw === "object" && !Array.isArray(raw) && Array.isArray(raw.options)
      ? raw.options.map((option) => ({ ...option }))
      : [];
    return {
      name,
      type: policy.type,
      default: policy.default,
      value: normalizeFilterValue(policy, rawFilterValue(source, name)),
      permission: policy.permission,
      cursorReset: policy.cursor_reset,
      minimum: policy.minimum,
      maximum: policy.maximum,
      values: [...policy.values],
      options,
    };
  });
}

export function requiredSectionFailure(workspace, section) {
  return REQUIRED_SECTION_FAILURES[workspace]?.[section] || null;
}

export function optionalSectionFallback(workspace, section) {
  return OPTIONAL_SECTION_FALLBACKS[workspace]?.[section] || null;
}

export const PAGE_MODEL_POLICY_MANIFEST = Object.freeze({
  schema: "tavern-page-model-policy/1.0.0",
  filters: FILTER_POLICIES,
  required_sections: REQUIRED_SECTION_FAILURES,
  optional_fallbacks: OPTIONAL_SECTION_FALLBACKS,
});

