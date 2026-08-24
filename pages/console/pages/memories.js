import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderFilterBar } from "../components/filters.js";
import { renderPagination } from "../components/pagination.js";
import { renderStatusBadge } from "../components/status.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { formatUtc8Minute } from "../components/time.js";
import { openDetail } from "../dialogs/detail-dialog.js";
import { openEditor } from "../dialogs/editor-dialog.js";
import { el, label, pageRoot, rows, stateNotice, summary, value } from "./shared.js";

const FILTER_LABELS = {
  session_key: copy("pages.memories.module.message.bfe958dea6"),
  q: copy("pages.memories.module.message.fbed18d390"),
  scope: copy("pages.memories.module.message.ae72b532f4"),
  type: copy("pages.memories.type"),
  importance: copy("pages.memories.module.message.86bd937524"),
  tag: copy("pages.memories.module.message.1d0fd5f933"),
  governance: copy("pages.memories.module.message.d97516289c"),
  page_size: copy("pages.memories.module.message.7a35603b46"),
};

function descriptorFor(item) {
  return rows(item?.available_actions).find((action) => action?.transportReady === true && action.intent === "memory.govern" && Number.isInteger(action.expected_revision));
}

function filterFields(model) {
  return rows(model.filters).filter((field) => !["cursor", "session_key"].includes(field.name)).map((field) => ({
    ...field,
    label: FILTER_LABELS[field.name] || field.name,
    type: ["q", "tag"].includes(field.name) ? "search" : field.name === "page_size" ? "select" : field.type,
    options: field.name === "page_size"
      ? [10, 20, 40, 80].map((size) => ({ value: size, label: copy("pages.memories.filters.label.9c36867c2b", { p0: size }) }))
      : [{ value: "", label: copy("pages.memories.filters.label.5c55a67935") }, ...rows(field.options)],
  }));
}

function update(handlers, values, { resetCursor = true } = {}) {
  handlers.updateLocation?.({ filters: { ...(handlers.navigation?.filters || {}), ...values, ...(resetCursor ? { cursor: "" } : {}) } }, { replace: false });
  return handlers.refresh?.();
}

function filterDisclosure(form, values, fields) {
  const defaults = Object.fromEntries(fields.map((field) => [field.name, field.default]));
  const active = Object.entries(values || {}).filter(([name, entry]) => name !== "cursor" && String(entry ?? "") !== String(defaults[name] ?? "")).length;
  const compact = globalThis.matchMedia?.("(max-width: 760px)")?.matches === true;
  return el("details", { class: "tavern-memory-filters", open: !compact || active > 0 }, [
    el("summary", { text: active ? copy("components.filters.mobile_summary_active", { p0: active }) : copy("components.filters.mobile_summary") }),
    form,
  ]);
}

function govern(item, handlers, opener) {
  const descriptor = descriptorFor(item);
  if (!descriptor) return;
  const action = { ...descriptor, object_key: item.key, target_key: item.key };
  openEditor(handlers.dialogs, {
    objectKey: item.key,
    revision: action.expected_revision,
    fields: rows(action.fields),
    opener,
    title: copy("pages.memories.governanceButton.title.e02d92f91d"),
    preview: (draft) => ({ summary: copy("pages.memories.governanceButton.summary.34f2adbf3b", { p0: draft.operation || copy("pages.memories.governanceButton.message.04fbe1f842") }) }),
    submit: ({ draft, idempotencyKey }) => handlers.actions.execute(action, { opener, input: draft, idempotencyKey }),
  });
}

function memoryState(item) {
  const raw = String(item.state || "");
  if (raw.includes("冲突")) return "warning";
  if (raw.includes("失效")) return "readonly";
  return raw ? "ready" : "unknown";
}

function memorySource(item, handlers, pageOpener) {
  const tags = item.tag_summary || rows(item.tags).filter(Boolean).join("、") || copy("pages.memories.tags_empty");
  const descriptor = descriptorFor(item);
  return el("div", { class: "tavern-memory-source", "aria-label": copy("pages.memories.rc8.6b51ce779a") }, [
    el("dl", {}, [
      el("div", {}, [el("dt", { text: copy("pages.memories.scope") }), el("dd", { text: item.scope || copy("pages.memories.scope_unknown") })]),
      el("div", {}, [el("dt", { text: copy("pages.memories.type") }), el("dd", { text: item.type || copy("pages.memories.type_unknown") })]),
      el("div", {}, [el("dt", { text: copy("pages.memories.tags") }), el("dd", { text: tags })]),
      el("div", {}, [el("dt", { text: copy("pages.memories.renderMemories.label.0a5f9a8929") }), el("dd", { text: item.updated_at ? formatUtc8Minute(item.updated_at) : copy("pages.memories.displayRows.message.4bdc19b47d") })]),
    ]),
    el("p", { text: item.source_summary || copy("pages.memories.rc8.bfa2d760cc") }),
    descriptor ? el("div", { class: "tavern-memory-source-actions" }, [
      renderButton({
        variant: "secondary",
        label: copy("pages.memories.governanceButton.label.10f759ed55"),
        intent: { id: descriptor.intent },
        onActivate: () => govern(item, handlers, pageOpener),
      }),
    ]) : null,
  ]);
}

function openMemorySource(item, handlers, opener) {
  if (!handlers.dialogs?.openDialog) return;
  openDetail(handlers.dialogs, {
    opener,
    title: label(item, copy("pages.memories.rc8.3bcbb9e06e")),
    specialization: "memory-source",
    tabs: [{ id: "source", label: copy("pages.memories.rc8.6b51ce779a") }],
    activeTab: "source",
    permissions: { source: true },
    lazyPanelLoader: () => memorySource(item, handlers, opener),
  });
}

function memoryRow(item, handlers) {
  const flags = [item.pinned ? copy("pages.memories.displayRows.message.fb47db5e65") : "", item.locked ? copy("pages.memories.displayRows.message.56cee90958") : ""].filter(Boolean);
  const descriptor = descriptorFor(item);
  const directGovernance = descriptor && ["warning", "readonly"].includes(memoryState(item));
  return el("article", { class: "tavern-memory-entry", "data-memory-state": memoryState(item) }, [
    el("div", { class: "tavern-memory-entry-state" }, [
      renderStatusBadge({ state: memoryState(item), label: item.state || copy("pages.memories.displayRows.message.cdea037991") }),
      flags.length ? el("small", { text: flags.join(" · ") }) : null,
    ]),
    el("div", { class: "tavern-memory-entry-main" }, [
      el("h3", { text: label(item, copy("pages.memories.rc8.3bcbb9e06e")) }),
      el("p", { text: summary(item, `${item.scope || copy("pages.memories.scope_unknown")} · ${item.type || copy("pages.memories.type_unknown")}`) }),
      el("div", { class: "tavern-memory-entry-meta" }, [
        el("span", { text: item.importance_label || copy("pages.memories.displayRows.message.a8b0bd9c65") }),
        el("span", { text: item.tag_summary || rows(item.tags).join("、") || copy("pages.memories.tags_empty") }),
        el("time", { text: item.updated_at ? formatUtc8Minute(item.updated_at) : copy("pages.memories.displayRows.message.4bdc19b47d") }),
      ]),
    ]),
    el("div", { class: "tavern-memory-entry-actions" }, [
      directGovernance ? renderButton({
        variant: "secondary",
        label: copy("pages.memories.governanceButton.title.e02d92f91d"),
        intent: { id: descriptor.intent },
        onActivate: (_intent, event) => govern(item, handlers, event.currentTarget),
      }) : null,
      renderButton({
        variant: "secondary",
        label: copy("pages.memories.rc8.6b51ce779a"),
        onActivate: (_intent, event) => openMemorySource(item, handlers, event.currentTarget),
      }),
      descriptor && !directGovernance ? renderButton({
        variant: "secondary",
        label: copy("pages.memories.governanceButton.label.10f759ed55"),
        intent: { id: descriptor.intent },
        onActivate: (_intent, event) => govern(item, handlers, event.currentTarget),
      }) : null,
    ]),
  ]);
}

function density(items) {
  const counts = [
    [copy("pages.memories.rc8.9ac37e5ba2"), items.length, copy("pages.memories.rc8.8cec95ac0f")],
    [copy("pages.memories.current_session"), items.filter((item) => String(item.scope || "").includes("副本")).length, copy("pages.memories.rc8.8cec95ac0f")],
    [copy("pages.memories.rc8.2013adbbe4"), items.filter((item) => String(item.state).includes("冲突")).length, copy("pages.memories.rc8.bb4e299644")],
    [copy("pages.memories.rc8.2fe5a8d0ee"), items.filter((item) => String(item.state).includes("失效")).length, copy("pages.memories.rc8.d7bdae3821")],
  ];
  return el("section", { class: "tavern-density-strip tavern-memory-density", "aria-label": copy("pages.memories.rc8.36657e69a6") }, counts.map(([title, count, detail]) => el("article", { class: "tavern-density-stat" }, [el("small", { text: title }), el("strong", { text: count }), el("span", { text: detail })])));
}

export function renderMemories(model, handlers = {}) {
  const root = pageRoot(model, "tavern-memories");
  root.setAttribute("class", `${root.className} tavern-memories-page`);
  root.append(stateNotice(model, copy("pages.memories.renderMemories.message.34200f77ad")));
  const fields = filterFields(model);
  const navigationFilters = handlers.navigation?.filters || {};
  const items = rows(value(model, "facts"));
  const filters = filterDisclosure(renderFilterBar({
      workspace: "memories",
      fields,
      values: navigationFilters,
      onApply: (values) => update(handlers, values),
      onClear: () => update(handlers, { q: "", scope: "", type: "", importance: "", tag: "", governance: "" }),
    }), navigationFilters, fields);
  root.append(
    el("header", { class: "tavern-page-toolbar tavern-memory-toolbar" }, [
      el("div", { class: "tavern-page-toolbar-copy" }, [el("h2", { text: model.title || copy("pages.memories.rc8.2f7ce40ff1") }), el("p", { text: model.summary || copy("pages.memories.renderMemories.text.53c1d29c64") })]),
      filters,
    ]),
    density(items),
  );
  const list = el("section", { id: "memories", class: "tavern-memory-ledger", "aria-label": copy("pages.memories.rc8.6ebfb5150b") });
  if (items.length) list.append(...items.map((item) => memoryRow(item, handlers)));
  else list.append(renderStatePanel({ phase: "empty", emptyCopy: copy("pages.memories.renderMemories.emptyCopy.1a790cb26c") }));
  root.append(list);
  if (model.pagination) root.append(renderPagination({ workspace: "memories", ...model.pagination, onPage: (cursor) => update(handlers, { cursor }, { resetCursor: false }) }));
  if (model.problems?.length && model.phase === "ready") root.append(renderStatePanel({ phase: "partial", operation: copy("pages.memories.renderMemories.operation.84a069f0e0"), problem: model.problems[0] }));
  return root;
}
