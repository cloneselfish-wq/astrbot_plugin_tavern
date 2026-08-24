import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderFilterBar } from "../components/filters.js";
import { renderPagination } from "../components/pagination.js";
import { renderStatusBadge } from "../components/status.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { openDetail } from "../dialogs/detail-dialog.js";
import { openEditor } from "../dialogs/editor-dialog.js";
import { el, label, pageRoot, rows, stateNotice, summary, value } from "./shared.js";

function stateToken(state) {
  if (state === "可用") return "ready";
  if (state === "已停用") return "readonly";
  if (state === "异常") return "error";
  if (state === "需要关注") return "warning";
  return "unknown";
}

function apply(handlers, filters, { resetCursor = true } = {}) {
  handlers.updateLocation?.({ filters: { ...filters, ...(resetCursor ? { cursor: "" } : {}) } }, { replace: false });
  return handlers.refresh?.();
}

function filterDisclosure(form, values, fields) {
  const defaults = Object.fromEntries(fields.map((field) => [field.name, field.default]));
  const names = new Set(fields.map((field) => field.name));
  const active = Object.entries(values || {}).filter(([name, entry]) => names.has(name) && name !== "cursor" && String(entry ?? "") !== String(defaults[name] ?? "")).length;
  const compact = globalThis.matchMedia?.("(max-width: 760px)")?.matches === true;
  return el("details", { class: "tavern-module-filters", open: !compact || active > 0 }, [
    el("summary", { text: active ? copy("components.filters.mobile_summary_active", { p0: active }) : copy("components.filters.mobile_summary") }),
    form,
  ]);
}

function registryDetails(item) {
  const registry = rows(item.registry);
  const panel = el("section", { class: "tavern-module-detail-list tavern-module-registry" });
  if (!registry.length) {
    panel.append(renderStatePanel({ phase: "empty", emptyCopy: copy("pages.modules.rc8.d704cfa6f2") }));
    return panel;
  }
  panel.append(
    el("p", { class: "tavern-module-detail-boundary", text: copy("pages.modules.rc8.38fd063685") }),
    el("ol", {}, registry.map((entry) => el("li", {}, [
      el("div", {}, [el("strong", { text: label(entry, copy("pages.modules.renderModules.message.42ddbac0b6")) }), renderStatusBadge({ state: stateToken(entry.state), label: entry.state || copy("pages.modules.renderModules.message.cdea037991") })]),
      el("p", { text: summary(entry, copy("pages.modules.renderModules.message.f2b157afca")) }),
      entry.layer ? el("small", { text: entry.layer }) : null,
    ]))),
  );
  return panel;
}

function overviewDetails(item) {
  return el("section", { class: "tavern-module-overview-detail" }, [
    el("p", { text: summary(item, copy("pages.modules.renderModules.message.f2b157afca")) }),
    el("dl", {}, [
      el("div", {}, [el("dt", { text: copy("pages.modules.renderModules.label.bff69c15d1") }), el("dd", { text: item.layer || copy("pages.modules.renderModules.message.a8b0bd9c65") })]),
      el("div", {}, [el("dt", { text: copy("pages.modules.renderModules.label.7767404865") }), el("dd", { text: rows(item.dependencies).join("、") || copy("pages.modules.renderModules.message.1abaebff0c") })]),
      el("div", {}, [el("dt", { text: copy("pages.modules.renderModules.label.2377c95996") }), el("dd", { text: rows(item.consumers).join("、") || copy("pages.modules.renderModules.message.161d4b1726") })]),
    ]),
  ]);
}

function openModuleDetail(item, handlers, opener) {
  if (!handlers.dialogs?.openDialog) return;
  openDetail(handlers.dialogs, {
    objectKey: item.key,
    opener,
    title: label(item, copy("pages.modules.renderModules.message.42ddbac0b6")),
    tabs: [{ id: "overview", label: copy("pages.modules.rc8.23b6afc43b") }, { id: "registry", label: copy("pages.modules.registry_count", { p0: rows(item.registry).length }) }],
    activeTab: "overview",
    lazyPanelLoader: async (tab) => tab === "registry" ? registryDetails(item) : overviewDetails(item),
  });
}

function moduleActions(item, handlers) {
  return rows(item.available_actions).filter((action) => action?.transportReady === true && action.intent && Number.isInteger(action.expected_revision)).map((action) => renderButton({
    variant: "secondary",
    label: action.label,
    intent: { id: action.intent },
    onActivate: (_intent, event) => {
      const bound = { ...action, object_key: item.key, target_key: item.key };
      if (rows(bound.fields).length) openEditor(handlers.dialogs, {
        objectKey: item.key,
        revision: bound.expected_revision,
        fields: rows(bound.fields),
        opener: event.currentTarget,
        title: bound.label,
        preview: () => ({ summary: bound.description }),
        submit: ({ draft, idempotencyKey }) => handlers.actions.execute(bound, { opener: event.currentTarget, input: draft, idempotencyKey }),
      });
      else handlers.actions.execute(bound, { opener: event.currentTarget });
    },
  }));
}

function moduleCard(item, handlers) {
  const dependencies = rows(item.dependencies);
  const consumers = rows(item.consumers);
  const state = stateToken(item.state);
  return el("article", { class: "tavern-module-card", "data-module-state": state }, [
    el("header", {}, [el("h3", { text: label(item, copy("pages.modules.renderModules.message.42ddbac0b6")) }), renderStatusBadge({ state, label: item.state || copy("pages.modules.renderModules.message.cdea037991") })]),
    el("p", { text: summary(item, copy("pages.modules.renderModules.message.f2b157afca")) }),
    el("dl", { class: "tavern-module-relations" }, [
      el("div", {}, [el("dt", { text: copy("pages.modules.renderModules.label.7767404865") }), el("dd", { text: dependencies.join("、") || copy("pages.modules.renderModules.message.1abaebff0c") })]),
      el("div", {}, [el("dt", { text: copy("pages.modules.renderModules.label.2377c95996") }), el("dd", { text: consumers.join("、") || copy("pages.modules.renderModules.message.161d4b1726") })]),
    ]),
    el("footer", {}, [
      renderButton({ variant: "secondary", label: copy("pages.modules.rc8.80a5cd3d19"), onActivate: (_intent, event) => openModuleDetail(item, handlers, event.currentTarget) }),
      ...moduleActions(item, handlers),
    ]),
  ]);
}

export function renderModules(model, handlers = {}) {
  const root = pageRoot(model, "tavern-modules");
  root.setAttribute("class", `${root.className} tavern-modules-page`);
  root.append(stateNotice(model, copy("pages.modules.renderModules.message.fcf6281ed1")));
  const byName = Object.fromEntries(rows(model.filters).map((field) => [field.name, field]));
  const select = (name, title) => ({ name, label: title, type: "select", options: [{ value: "", label: copy("pages.modules.renderModules.label.8b65a8100d", { p0: title }) }, ...rows(byName[name]?.options)] });
  const filterFields = [
    { name: "q", label: copy("pages.modules.renderModules.label.5cf35c4007"), type: "search" },
    select("status", copy("pages.modules.renderModules.message.6320b4a872")),
    select("layer", copy("pages.modules.renderModules.message.bff69c15d1")),
    select("consumer", copy("pages.modules.renderModules.message.2377c95996")),
  ];
  const filters = handlers.navigation?.filters || {};
  const coverage = value(model, "coverage") || {};
  const modules = rows(value(model, "modules")).slice(0, 6);
  root.append(
    el("div", { class: "tavern-page-toolbar" }, [
      el("div", { class: "tavern-page-toolbar-copy" }, [el("h2", { text: model.title || copy("pages.modules.renderModules.message.22c280c97c") }), el("p", { text: model.summary || copy("pages.modules.renderModules.message.5bb0e70359") })]),
      filterDisclosure(renderFilterBar({ workspace: "modules", fields: filterFields, values: filters, onApply: (next) => apply(handlers, next), onClear: () => apply(handlers, {}) }), filters, filterFields),
    ]),
    el("section", { class: "tavern-density-strip", "aria-label": copy("pages.modules.renderModules.message.cca0b0d8e7") }, [
      el("article", {}, [el("small", { text: copy("pages.modules.rc8.ba32c6cf00") }), el("strong", { text: modules.length }), el("span", { text: copy("pages.modules.rc8.1045887375") })]),
      el("article", {}, [el("small", { text: copy("contracts.page_models.sectionValue.message.4d99c976be") }), el("strong", { text: coverage.available ?? copy("pages.modules.renderModules.message.a8b0bd9c65") }), el("span", { text: copy("pages.modules.renderModules.text.ca5c5ba2b9", { p0: coverage.available ?? copy("pages.modules.renderModules.message.a8b0bd9c65") }) })]),
      el("article", {}, [el("small", { text: copy("contracts.page_models.sectionValue.label.f121ab742c") }), el("strong", { text: coverage.attention ?? copy("pages.modules.renderModules.message.a8b0bd9c65") }), el("span", { text: copy("pages.modules.renderModules.text.b66d6d858b", { p0: coverage.attention ?? copy("pages.modules.renderModules.message.a8b0bd9c65") }) })]),
      el("article", {}, [el("small", { text: copy("pages.modules.renderModules.label.7767404865") }), el("strong", { text: modules.filter((item) => rows(item.dependencies).length).length }), el("span", { text: copy("pages.modules.rc8.38fd063685") })]),
      el("article", {}, [el("small", { text: copy("pages.modules.renderModules.label.2377c95996") }), el("strong", { text: modules.filter((item) => rows(item.consumers).length).length }), el("span", { text: copy("pages.modules.rc8.261e2155a0") })]),
    ]),
  );
  const grid = el("section", { class: "tavern-module-categories tavern-module-grid", "aria-label": copy("pages.modules.rc8.a02dc5cc78") });
  if (modules.length) grid.append(...modules.map((item) => moduleCard(item, handlers)));
  else grid.append(renderStatePanel({ phase: "empty", emptyCopy: copy("pages.modules.renderModules.text.ec2e91c5dc") }));
  root.append(grid);
  if (model.pagination) root.append(renderPagination({ workspace: "modules", ...model.pagination, onPage: (cursor) => apply(handlers, { ...filters, cursor }, { resetCursor: false }) }));
  return root;
}
