import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderDensityStrip } from "../components/cards.js";
import { renderCapabilityHub } from "../components/capability-hub.js";
import { renderFilterBar } from "../components/filters.js";
import { renderPagination } from "../components/pagination.js";
import { renderStatusBadge } from "../components/status.js";
import { openConfirm } from "../dialogs/confirm-dialog.js";
import { openEditor } from "../dialogs/editor-dialog.js";
import { openSessionDetail } from "../dialogs/session-detail.js";
import { el, pageRoot, rows, stateNotice, value } from "./shared.js";

function requiredErrors(fields, draft) {
  return Object.fromEntries(fields
    .filter((field) => field.required && (field.type === "checkbox" ? !draft[field.name] : !String(draft[field.name] ?? "").trim()))
    .map((field) => [field.name, copy("pages.sessions.action.required", { p0: field.label || copy("pages.sessions.action.required_field") })]));
}

function confirmDescriptor(action, runtime, opener, input = {}, idempotencyKey = crypto.randomUUID()) {
  return openConfirm(runtime.dialogs, {
    opener,
    operation: action.label,
    impact: action.description || copy("pages.sessions.action.impact"),
    unchanged: copy("pages.sessions.action.unchanged"),
    automatic: copy("pages.sessions.action.automatic"),
    recovery: copy("pages.sessions.action.recovery"),
    returnCheck: copy("pages.sessions.action.return_check"),
    confirmLabel: action.label,
    intent: { id: action.intent },
    idempotencyKey,
    onConfirm: ({ idempotencyKey: confirmedKey }) => runtime.actions?.execute(action, { opener, input, idempotencyKey: confirmedKey }),
  });
}

function executeDescriptor(descriptor, item, runtime, opener) {
  const action = { ...descriptor, object_key: item.key, target_key: item.key };
  const fields = rows(action.fields);
  if (!fields.length) return runtime.actions?.execute(action, { opener });
  return openEditor(runtime.dialogs, {
    objectKey: item.key,
    revision: action.expected_revision,
    fields,
    opener,
    title: action.label,
    validate: (draft) => requiredErrors(fields, draft),
    preview: () => ({
      summary: action.description || action.label,
      diff_summary: copy("pages.sessions.action.diff_summary"),
    }),
    submit: ({ draft, idempotencyKey }) => ({
      afterClose: () => confirmDescriptor(action, runtime, opener, draft, idempotencyKey),
    }),
  });
}

function safeActions(item, runtime) {
  return rows(item.available_actions)
    .filter((action) => action?.transportReady === true
      && action.intent
      && action.intent !== "session.token_quota.set"
      && Number.isInteger(action.expected_revision))
    .map((descriptor) => renderButton({
      variant: /放弃|完结/.test(descriptor.label) ? "danger" : "secondary",
      label: descriptor.label,
      intent: { id: descriptor.intent },
      onActivate: (_intent, event) => executeDescriptor(descriptor, item, runtime, event.currentTarget),
    }));
}

function sessionActions(item, model, runtime) {
  return [
    renderButton({
      label: copy("pages.sessions.renderSessions.label.ca48bafbfd"),
      onActivate: () => runtime.navigate?.("session_detail", { objectKey:item.key }),
    }),
    renderButton({
      variant:"secondary",
      label:copy("pages.sessions.group.details"),
      onActivate:(_intent,event) => openDetails(item, model, runtime, event.currentTarget),
    }),
    ...safeActions(item,runtime),
  ];
}

// Keep the action adapter explicit: every row consumes the same safe descriptor path.
const sessionRowContract = { rowActions:(item)=> (model, runtime) => sessionActions(item, model, runtime) };

function sessionProgress(item) {
  const details = [];
  if (item.round !== null && item.round !== undefined) {
    details.push(copy("pages.sessions.renderSessions.label.4890584bb5") + ` ${item.round}`);
  }
  if (item.scene_label) details.push(item.scene_label);
  if (item.progress_label) {
    const amount = item.progress_current !== null && item.progress_current !== undefined
      && item.progress_total !== null && item.progress_total !== undefined
      ? ` ${item.progress_current} / ${item.progress_total}`
      : "";
    details.push(`${item.progress_label}${amount}`);
  }
  if (item.risk_summary) details.push(item.risk_summary);
  return details;
}

function sessionPlayers(item) {
  const details = [];
  if (item.player_summary) details.push(item.player_summary);
  if (item.actor_label) {
    details.push(`${copy("pages.sessions.renderSessions.label.ef26096871")}：${item.actor_label}`);
  }
  if (item.readonly_reason) details.push(item.readonly_reason);
  return details;
}

function sessionRow(item, model, runtime) {
  const progress = sessionProgress(item);
  const players = sessionPlayers(item);
  return el("article", {
    class:"tavern-session-row",
    role:"listitem",
    "data-session-state":item.state || "readonly",
    "data-object-key":item.key || "",
  }, [
    el("div", { class:"tavern-session-row-main" }, [
      el("strong", { text:item.label || copy("pages.sessions.renderSessions.label.bfe958dea6") }),
      el("span", { text:item.summary || item.world_label || "" }),
    ]),
    el("div", { class:"tavern-session-row-progress", "data-column":"progress", "data-mobile-label":copy("pages.sessions.renderSessions.label.6320b4a872") }, [
      renderStatusBadge({ state:item.state || "readonly", label:item.state || copy("pages.sessions.renderSessions.label.6320b4a872") }),
      ...progress.map((entry) => el("span", { text:entry })),
    ]),
    el("div", { class:"tavern-session-row-players", "data-column":"players", "data-mobile-label":copy("pages.sessions.renderSessions.label.ef26096871") }, players.length
      ? players.map((entry, index) => index === 0 ? el("strong", { text:entry }) : el("span", { text:entry }))
      : [el("span", { text:copy("pages.sessions.renderSessions.empty_current_actor") })]),
    el("div", { class:"tavern-session-row-actions", "data-column":"actions" }, sessionRowContract.rowActions(item)(model, runtime)),
  ]);
}

function sessionList(groupRows, groupIndex, model, runtime) {
  const list = el("div", {
    class:"tavern-session-list",
    role:"list",
    "aria-label":copy("pages.sessions.renderSessions.label.bfe958dea6"),
    "data-session-list":String(groupIndex + 1),
  });
  list.append(el("div", { class:"tavern-session-list-head", "aria-hidden":"true" }, [
    el("span", { text:copy("pages.sessions.renderSessions.label.bfe958dea6") }),
    el("span", { text:`${copy("pages.sessions.renderSessions.label.6320b4a872")} / ${copy("pages.sessions.renderSessions.label.4890584bb5")}` }),
    el("span", { text:copy("pages.sessions.renderSessions.label.ef26096871") }),
    el("span", { text:copy("pages.sessions.group.details") }),
  ]));
  if (!groupRows.length) {
    list.append(el("p", { class:"tavern-session-list-empty", text:copy("pages.sessions.renderSessions.emptyCopy.2ba1ff7a16") }));
    return list;
  }
  list.append(...groupRows.map((item) => sessionRow(item, model, runtime)));
  return list;
}

function openDetails(item, model, runtime, opener) {
  const handlers = {
    ...runtime,
    navigation: { ...(runtime.navigation || {}), objectKey: item.key },
  };
  return openSessionDetail({
    model: { ...model, title: item.label },
    handlers,
    opener,
    activeTab: model.permissions?.can_manage ? "management" : "overview",
  }) || runtime.navigate?.("session_detail", { objectKey: item.key });
}

function applyFilters(runtime, filters) {
  const clean = Object.fromEntries(Object.entries(filters).filter(([, value]) => String(value).trim()));
  runtime.updateLocation?.({ filters: clean }, { replace: false });
  return runtime.refresh?.();
}

function groupSummary(group, groupRows) {
  return [
    `${copy("pages.sessions.group.visible")} ${group.visible_count ?? groupRows.length}`,
    group.running_count === null || group.running_count === undefined
      ? `${copy("pages.sessions.group.running")} ${copy("pages.sessions.group.count_unknown")}`
      : `${copy("pages.sessions.group.running")} ${group.running_count}`,
    group.quota_summary ? `${copy("pages.sessions.group.quota")} ${group.quota_summary}` : "",
  ].filter(Boolean).join(" · ");
}

function groupQuotaAction(groupRows, runtime) {
  for (const item of groupRows) {
    const descriptor = rows(item.available_actions).find((action) =>
      action?.transportReady === true
      && action.intent === "session.token_quota.set"
      && Number.isInteger(action.expected_revision));
    if (!descriptor) continue;
    return renderButton({
      variant:"secondary",
      label:descriptor.label,
      intent:{ id:descriptor.intent },
      onActivate:(_intent,event) => executeDescriptor(descriptor, item, runtime, event.currentTarget),
    });
  }
  return null;
}

export function renderSessions(model, runtime = {}) {
  const root = pageRoot(model, "tavern-sessions"); root.append(stateNotice(model, copy("pages.sessions.renderSessions.message.6e1bcfce62")));
  const block = (id, node, state = "ready") => { node.setAttribute("id", `tavern-sessions-${id}`); node.setAttribute("data-block", id); node.setAttribute("data-state", state); node.setAttribute("data-testid", `tavern-block-sessions-${id}`); return node; };
  const summarySection = value(model, "summary") || {};
  const filterSpec = Object.fromEntries(rows(model.filters).map((field) => [field.name, field]));
  const navigationFilters = runtime.navigation?.filters || {};
  const toolbar = block("status-world-group-filters", el("section", { class:"page-toolbar tavern-sessions-toolbar", "aria-labelledby":"tavern-sessions-toolbar-title" }));
  const toolbarCopy = el("div", { class:"tavern-page-toolbar-copy tavern-sessions-toolbar-copy" }, [
    el("h2", { id:"tavern-sessions-toolbar-title", text:copy("shell.registry.module.label.f01045feb6") }),
    el("p", { text:copy("pages.sessions.capability.summary") }),
  ]);
  const filters = renderFilterBar({ workspace:"sessions", fields:[
    { name:"q", type:"search", label:copy("pages.sessions.renderSessions.label.4d96f1ed08") },
    { name:"status", type:"select", label:copy("pages.sessions.renderSessions.label.6320b4a872"), options:[{value:"",label:copy("pages.sessions.renderSessions.label.0a379c1e73")}, ...rows(filterSpec.status?.options)] },
    { name:"world", type:"select", label:copy("pages.sessions.renderSessions.label.33650a3695"), options:[{value:"",label:copy("pages.sessions.renderSessions.label.0f5cf6c4df")}, ...rows(filterSpec.world?.options)] },
    { name:"group", type:"select", label:copy("pages.sessions.renderSessions.label.f3f8bcf3f5"), options:[{value:"",label:copy("pages.sessions.renderSessions.label.cab73196ff")}, ...rows(filterSpec.group?.options)] },
  ], values:navigationFilters, onApply:(nextFilters)=>applyFilters(runtime,nextFilters), onClear:()=>applyFilters(runtime,{}) });
  toolbar.append(toolbarCopy, filters);
  root.append(toolbar);
  root.append(block("density-strip", el("section", { class:"tavern-sessions-density", "aria-label":copy("pages.sessions.renderSessions.message.6e1bcfce62") }, [
    renderDensityStrip({ workspace:"sessions", stats:rows(summarySection.metrics).map((item,index)=>({ key:item.key || `state-${index}`, label:item.label, value:item.value })) }),
  ])));
  const capabilityPanels = rows(value(model, "capability_panels"));
  const capabilityHub = renderCapabilityHub({
    panels: capabilityPanels,
    group: "session",
    title: copy("pages.sessions.capability.title"),
    summary: copy("pages.sessions.capability.summary"),
    handlers: runtime,
  });
  if (capabilityHub) root.append(block("capability-hub", capabilityHub));
  const source = rows(value(model,"groups"));
  const canonicalGroups = source.filter((item) => item && Array.isArray(item.items));
  const fallback = new Map();
  if (!canonicalGroups.length) {
    for (const item of source) {
      const name = item.group_label || copy("pages.sessions.renderSessions.message.1f1195ff97");
      fallback.set(name, [...(fallback.get(name) || []), item]);
    }
  }
  const groups = canonicalGroups.length
    ? canonicalGroups
    : [...fallback.entries()].map(([name, items]) => ({
        key:name,
        label:name,
        platform_label:items[0]?.group_platform_label,
        visible_count:items[0]?.group_visible_count ?? items.length,
        running_count:items[0]?.group_running_count,
        quota_summary:items[0]?.group_quota_summary,
        items,
      }));
  const groupHost = block("group-header", el("section", { class:"tavern-sessions-groups" }), model.phase==="partial"?"partial":groups.length?"ready":"empty");
  groups.forEach((group, groupIndex) => {
    const name = group.label || copy("pages.sessions.renderSessions.message.1f1195ff97"); const groupRows = rows(group.items);
    const quotaAction = groupQuotaAction(groupRows, runtime);
    groupHost.append(el("section", { class:"session-group tavern-session-group" }, [
      el("header", { class:"session-group-head tavern-session-group-header" }, [
        el("div", { class:"tavern-session-group-identity" }, [
          el("span", { class:"tavern-story-kicker", text:group.platform_label || copy("pages.sessions.group.platform") }),
          el("h3", { text:name }),
          el("p", { text:groupSummary(group, groupRows) }),
        ]),
        quotaAction ? el("div", { class:"tavern-session-group-actions" }, [quotaAction]) : null,
      ]),
      el("div", { class:"list-panel tavern-sessions-table", "data-block":"session-table" }, [sessionList(groupRows, groupIndex, model, runtime)]),
    ]));
  });
  if (!groups.length && model.phase === "ready") groupHost.append(el("p", { class:"tavern-result", text:copy("pages.sessions.renderSessions.text.8f22fe19ff") })); root.append(groupHost);
  if (model.pagination) root.append(block("range-pagination", el("section", { class:"tavern-sessions-pagination" }, [renderPagination({ workspace:"sessions", cursor:runtime.navigation?.filters?.cursor, nextCursor:model.pagination.next_cursor, previousCursor:model.pagination.previous_cursor, hasMore:model.pagination.has_more, rangeLabel:copy("pages.sessions.renderSessions.message.33e8cc2612", {p0:model.pagination.visible_from||0,p1:model.pagination.visible_to||0,p2:model.pagination.total==null?"":copy("common.pagination.total.message",{p0:model.pagination.total})}), onPage:(cursor)=>applyFilters(runtime,{...(runtime.navigation?.filters||{}),cursor}) })])));
  if(model.phase==="permission")root.replaceChildren(stateNotice(model,copy("pages.sessions.renderSessions.message.6e1bcfce62")),...["status-world-group-filters","density-strip","capability-hub","group-header","session-table","range-pagination"].map((id)=>block(id,el("section",{class:"tavern-sessions-redacted"}),"permission")));
  return root;
}
