import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderDensityStrip } from "../components/cards.js";
import { renderPagination } from "../components/pagination.js";
import { renderStatusBadge } from "../components/status.js";
import { formatUtc8Minute } from "../components/time.js";
import { openEditor } from "../dialogs/editor-dialog.js";
import { icon } from "../visualizations/icons.js";
import { el, label, pageRoot, rows, stateNotice, summary, value } from "./shared.js";

function update(runtime, filters) { runtime.updateLocation?.({ filters }, { replace:false }); return runtime.refresh?.(); }

function executeDescriptor(descriptor, item, runtime, opener) {
  const action = { ...descriptor, object_key:item.key, target_key:item.key };
  if (!rows(action.fields).length) return runtime.actions?.execute(action, { opener });
  return openEditor(runtime.dialogs, {
    objectKey:item.key,
    revision:action.expected_revision,
    fields:rows(action.fields),
    opener,
    title:action.label,
    preview:() => ({ summary:action.description || action.label }),
    submit:({ draft, idempotencyKey }) => runtime.actions?.execute(action, { opener, input:draft, idempotencyKey }),
  });
}

function todoActions(item, runtime) {
  return rows(item.available_actions)
    .filter((action) => action?.transportReady === true && action.intent && Number.isInteger(action.expected_revision))
    .slice(0, 1)
    .map((descriptor) => renderButton({
      variant:"primary",
      label:descriptor.label,
      intent:{ id:descriptor.intent },
      onActivate:(_intent,event) => executeDescriptor(descriptor, item, runtime, event.currentTarget),
    }));
}

function todoIcon(item) {
  const state = `${item.object_kind || ""} ${item.state || ""}`.toLowerCase();
  if (/deliver|投递|消息/.test(state)) return icon("delivery");
  if (/character|角色|建卡|审核/.test(state)) return icon("characters");
  if (/permission|权限/.test(state)) return icon("lock");
  if (/error|失败|异常/.test(state)) return icon("warning");
  return icon("todo");
}

function navigationActions(item, runtime) {
  const navigation = item?.navigation;
  if (!navigation?.workspace || typeof runtime.canNavigate === "function" && !runtime.canNavigate(navigation.workspace)) return [];
  return [renderButton({
    variant:"secondary",
    label:navigation.label || copy("pages.sessions.group.details"),
    onActivate:() => runtime.navigate?.(navigation.workspace, navigation.context || {}),
  })];
}

function todoRow(item, runtime, { actionable = false, navigable = false } = {}) {
  const actions = actionable ? todoActions(item, runtime) : navigable ? navigationActions(item, runtime) : [];
  return el("article", { class:"tavern-todo-row", role:"listitem", "data-object-key":item.key || "" }, [
    el("span", { class:"tavern-todo-icon", "aria-hidden":"true" }, [todoIcon(item)]),
    el("div", { class:"tavern-todo-main" }, [
      el("strong", { text:label(item) }),
      el("span", { text:summary(item) }),
      el("div", { class:"tavern-todo-meta" }, [
        renderStatusBadge({ state:item.state || (actionable ? "waiting" : "readonly"), label:item.state || copy("pages.todo.renderTodo.label.ba40014ff4") }),
        item.updated_at ? el("time", { datetime:item.updated_at, text:formatUtc8Minute(item.updated_at) }) : null,
      ]),
    ]),
    actions.length ? el("div", { class:"tavern-todo-actions" }, actions) : null,
  ]);
}

function todoList(id, items, runtime, { actionable = false, navigable = false, emptyCopy }) {
  return items.length
    ? el("div", { class:"tavern-todo-list", id, role:"list" }, items.map((item) => todoRow(item, runtime, { actionable, navigable })))
    : el("p", { class:"tavern-todo-empty", text:emptyCopy });
}

function todoToolbarControls(runtime, statusOptions) {
  const current = String(runtime.navigation?.filters?.status || "");
  const options = [{ value:"", label:copy("pages.todo.renderTodo.label.267c4a7a86") }, ...rows(statusOptions)];
  const selector = el("select", {
    class:"tavern-control",
    name:"status",
    "aria-label":copy("pages.todo.renderTodo.label.ba40014ff4"),
    onChange:(event) => update(runtime, event.currentTarget.value ? { status:event.currentTarget.value } : {}),
  }, options.map((option) => el("option", {
    value:option.value,
    text:option.label,
    selected:String(option.value) === current,
  })));
  return el("div", { class:"filter-row tavern-todo-toolbar-controls" }, [
    selector,
    renderButton({
      variant:"quiet",
      label:copy("shell.refresh.idle"),
      onActivate:() => runtime.refresh?.(),
    }),
  ]);
}

export function renderTodo(model, runtime = {}) {
  const root = pageRoot(model, "tavern-todo"); root.append(stateNotice(model, copy("pages.todo.renderTodo.message.9db36c4b21")));
  const block = (id, node, state = "ready") => { node.setAttribute("id", `tavern-todo-${id}`); node.setAttribute("data-block", id); node.setAttribute("data-state", state); node.setAttribute("data-testid", `tavern-block-todo-${id}`); return node; };
  const statusOptions = rows(model.filters).find((field) => field.name === "status")?.options;
  const boundary = value(model,"delivery_boundary") || {};
  const queueRows = rows(boundary.queue).length ? rows(boundary.queue) : rows(value(model,"queue_summary"));
  const queueBlock = block("queue-metrics", el("section", { class:"tavern-todo-queue" }, [
    el("header", { class:"page-toolbar tavern-todo-toolbar" }, [
      el("div", { class:"tavern-page-toolbar-copy tavern-todo-section-heading" }, [el("h2", { text:copy("pages.todo.renderTodo.text.b7e3e715f1") }),el("p",{text:copy("pages.todo.renderTodo.text.1ef83b1eaa")})]),
      todoToolbarControls(runtime, statusOptions),
    ]),
    renderDensityStrip({ workspace:"todo", stats:queueRows.map((item,index)=>({key:item.key || `queue-${index}`,label:label(item),value:item.value})) }),
  ]), queueRows.length?"ready":"empty");
  root.append(queueBlock);
  const split = el("div", { class:"tavern-todo-shell" });
  const allItems = rows(value(model,"actionable"));
  const actionable = allItems.filter((item) => item.actionable === true || rows(item.available_actions).some((action)=>action?.transportReady===true));
  const actionableKeys = new Set(actionable.map((item) => String(item.key || "")).filter(Boolean));
  const blockers = rows(value(model,"blockers"))
    .filter((item) => !actionableKeys.has(String(item.key || "")));
  const primary = block("actionable-list", el("section", { class:"panel compact-panel tavern-todo-actionable" }, [
      el("h2", { text:copy("pages.todo.renderTodo.text.b7e3e715f1") }),
      todoList("todo-actionable", actionable, runtime, { actionable:true, emptyCopy:copy("pages.todo.renderTodo.emptyCopy.5410b2a001") }),
  ]), model.phase==="partial"?"partial":actionable.length?"ready":"empty");
  const secondary = el("div", { class:"tavern-todo-secondary" }, [
    block("runtime-queue", el("section", { class:"panel compact-panel tavern-todo-runtime" }, [
    el("h2", { text:copy("pages.todo.renderTodo.text.35ae434e69") }),
    todoList("todo-runtime", blockers, runtime, { navigable:true, emptyCopy:copy("pages.todo.renderTodo.emptyCopy.5410b2a001") }),
  ]), blockers.length?"ready":"empty"),
    block("delivery-boundary", el("aside", { class:"source-note tavern-todo-boundary" }, [
    el("h2", { text:boundary.label || copy("pages.todo.renderTodo.text.5f7cf1105b") }),
    el("p", { text:boundary.summary || copy("pages.todo.renderTodo.text.1ef83b1eaa") }),
    boundary.state ? el("p", { class:"tavern-todo-boundary-state", text:boundary.state }) : null,
  ]), boundary.summary?"ready":"empty"),
  ]);
  split.append(primary, secondary);
  root.append(split);
  if(model.pagination) root.append(block("pagination", el("section", { class:"tavern-todo-pagination" }, [renderPagination({workspace:"todo",cursor:runtime.navigation?.filters?.cursor,nextCursor:model.pagination.next_cursor,previousCursor:model.pagination.previous_cursor,hasMore:model.pagination.has_more,rangeLabel:copy("pages.todo.renderTodo.message.f1dacfa7de", {p0:model.pagination.visible_from||0,p1:model.pagination.visible_to||0,p2:model.pagination.total==null?"":copy("common.pagination.total.message",{p0:model.pagination.total})}),onPage:(cursor)=>update(runtime,{...(runtime.navigation?.filters||{}),cursor})})])));
  if(model.phase==="permission")root.replaceChildren(stateNotice(model,copy("pages.todo.renderTodo.message.9db36c4b21")),...["queue-metrics","actionable-list","runtime-queue","delivery-boundary","pagination"].map((id)=>block(id,el("section",{class:"tavern-todo-redacted"}),"permission")));
  return root;
}
