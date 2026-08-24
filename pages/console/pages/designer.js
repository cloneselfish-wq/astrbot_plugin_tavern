import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { renderStatusBadge } from "../components/status.js";
import { renderCapabilityHub } from "../components/capability-hub.js";
import { openEditor } from "../dialogs/editor-dialog.js";
import { el, label, pageRoot, rows, stateNotice, summary, value } from "./shared.js";

const stateMap = (state) => /阻塞|失败|缺少/.test(state) ? "warning" : /可继续|可检查|完成/.test(state) ? "ready" : "waiting";
const descriptorFor = (item, intent) => rows(item?.available_actions).find((action) => action?.transportReady === true && (!intent || action.intent === intent));
const bound = (descriptor, item) => descriptor ? { ...descriptor, object_key:item.key, id:descriptor.action_id || descriptor.intent } : null;

function actionTarget(intent, candidates=[]) {
  for (const item of candidates) {
    const descriptor=descriptorFor(item,intent);
    if (descriptor && item?.key) return {descriptor,item};
  }
  return null;
}

function activate(descriptor, item, handlers, opener) {
  const action=bound(descriptor,item);
  if (!action) return;
  openEditor(handlers.dialogs, { objectKey:item.key, revision:action.expected_revision, fields:rows(action.fields), opener, title:action.label, preview:() => ({summary:action.description || copy("pages.designer.activate.message.1901631aa6")}), submit:({draft,idempotencyKey})=>handlers.actions.execute(action,{opener,input:draft,idempotencyKey}) });
}

function actionButton(descriptor, item, handlers, variant="secondary") {
  if (!descriptor?.transportReady) return null;
  return renderButton({variant,label:descriptor.label,intent:bound(descriptor,item),onActivate:(_intent,event)=>activate(descriptor,item,handlers,event.currentTarget)});
}

function validationButton(target, handlers) {
  const descriptor=target?.descriptor;
  const item=target?.item;
  const ready = descriptor?.transportReady === true && item?.key && handlers.dialogs?.openDialog && handlers.actions?.execute;
  return renderButton({
    variant:"secondary",
    label:descriptor?.label || copy("pages.author_jobs.rc8.e3251f491d"),
    disabledReason:ready ? "" : item ? copy("pages.designer.rc8.a857505098") : copy("pages.designer.rc8.d9365f158c"),
    intent:ready ? bound(descriptor,item) : null,
    onActivate:(_intent,event)=>{
      if (!ready) return;
      const action=bound(descriptor,item);
      openEditor(handlers.dialogs, {
        objectKey:item.key,
        revision:action.expected_revision,
        fields:rows(action.fields),
        draft:{job_type:"full_preflight"},
        opener:event.currentTarget,
        title:action.label,
        preview:()=>({summary:action.description}),
        submit:({draft,idempotencyKey})=>handlers.actions.execute(action,{opener:event.currentTarget,input:{...draft,job_type:"full_preflight"},idempotencyKey}),
      });
    },
  });
}

function saveDraftButton(target, handlers, readonly=false) {
  const descriptor=target?.descriptor;
  const item=target?.item;
  const ready = !readonly && descriptor?.transportReady === true && item?.key && handlers.dialogs?.openDialog && handlers.actions?.execute;
  return renderButton({
    variant:"primary",
    label:copy("pages.designer.toolbar.save_draft"),
    disabledReason:ready ? "" : readonly ? copy("pages.designer.toolbar.save_readonly") : copy("pages.designer.toolbar.save_unavailable"),
    intent:ready ? bound(descriptor,item) : null,
    onActivate:(_intent,event)=>{
      if (!ready) return;
      activate(descriptor,item,handlers,event.currentTarget);
    },
  });
}

function flowStep(item, index, handlers, contextActions=[], context={}) {
  const descriptors=flowDescriptors(item,index,contextActions);
  const actions=descriptors.map((entry)=>actionButton(entry,item.key?item:context,handlers)).filter(Boolean);
  return el("li",{class:"tavern-designer-info-row","data-state":stateMap(item.state)},[
    el("div",{class:"tavern-designer-info-copy"},[
      el("strong",{text:label(item)}),
      el("span",{text:summary(item)}),
    ]),
    el("div",{class:"tavern-designer-inline-actions"},[
      renderStatusBadge({state:stateMap(item.state),label:item.state||copy("pages.designer.flowStep.message.8f31a776d5")}),
      ...actions,
    ]),
  ]);
}

function contentCard(item, handlers) {
  const actions=rows(item.available_actions).filter((entry)=>entry?.transportReady===true).slice(0,2).map((entry)=>actionButton(entry,item,handlers,entry.intent?.includes("retire")?"danger":"secondary"));
  return el("article",{class:"tavern-designer-module-card","data-object-key":item.key||label(item)},[
    el("header",{},[
      el("h3",{text:label(item)}),
      renderStatusBadge({state:stateMap(item.state),label:item.state||copy("pages.designer.contentCard.message.cdea037991")}),
    ]),
    el("p",{text:summary(item)}),
    actions.length?el("div",{class:"tavern-designer-module-actions"},actions):null,
  ]);
}

export function renderDesigner(model, handlers = {}) {
  const root = pageRoot(model,"tavern-designer");
  root.append(stateNotice(model,copy("pages.designer.renderDesigner.message.cbf02abb0c")));
  const worldFilter = model.filters.find((field)=>field.name==="world_key");
  const context = value(model,"context") || {};
  const contextActions = rows(context.available_actions).filter((entry)=>entry?.transportReady===true);
  const flow = rows(value(model,"flow"));
  const contentItems = rows(value(model,"content"));
  const problems = rows(value(model,"problems"));
  const reports = rows(value(model,"reports"));
  const authorPanels = designerCapabilityPanels({ context, contentItems, problems, reports });
  const worldOptions = rows(worldFilter?.options);
  const currentWorld = handlers.navigation?.filters?.world_key || worldFilter?.value || "";
  const selector = worldOptions.length ? el("select",{
    class:"tavern-control",
    "aria-label":copy("pages.designer.world_filter.label"),
    onChange:(event)=>{
      handlers.updateLocation?.({filters:{...(handlers.navigation?.filters||{}),world_key:event.currentTarget.value}},{replace:false});
      return handlers.refresh?.();
    },
  },[
    el("option",{value:"",selected:!currentWorld,text:copy("pages.designer.world_filter.placeholder")}),
    ...worldOptions.map((option)=>el("option",{value:option.value,selected:String(option.value)===String(currentWorld),text:option.label})),
  ]) : null;
  const actionOwners = [context,...flow,...contentItems];
  const validationTarget = actionTarget("author_job.create",actionOwners);
  const saveTarget = actionTarget("designer.field.save",actionOwners);
  const toolbar = el("header",{class:"tavern-page-toolbar tavern-designer-toolbar"},[
    authorContext(),
    el("div",{class:"tavern-filter-row tavern-designer-toolbar-actions"},[
      selector,
      validationButton(validationTarget,handlers),
      saveDraftButton(saveTarget,handlers,model.readonly===true),
    ]),
  ]);
  root.append(toolbar);
  const flowPanel = el("section",{class:"tavern-author-flow-panel tavern-designer-compact-panel"},[sectionHeading(copy("pages.designer.renderDesigner.text.a23a87f3eb"),model.summary),el("ol",{class:"tavern-author-flow"},flow.map((item,index)=>flowStep(item,index,handlers,contextActions,context)))]);
  if (flow.length !== 4) flowPanel.append(renderStatePanel({phase:"partial",operation:copy("pages.designer.rc8.1a6a643ad6"),problem:{message:copy("pages.designer.rc8.0905705c23"),recovery:copy("pages.designer.rc8.8674ea34f9")}}));
  const content = el("section",{class:"tavern-content-board tavern-designer-compact-panel"},[sectionHeading(copy("pages.designer.renderDesigner.text.b0b5a259b4"),copy("pages.designer.renderDesigner.text.100c056ad0")),el("div",{class:"tavern-designer-content-grid"},contentItems.map((item)=>contentCard(item,handlers)))]);
  if (!contentItems.length) content.append(renderStatePanel({phase:"empty",emptyCopy:copy("pages.designer.renderDesigner.emptyCopy.431dd4f6ce")}));
  const inspector = el("aside",{class:"tavern-author-inspector"},[
    reportPanel(reports,context,model.summary),
    advancedToolsPanel(problems,context,reports),
  ]);
  root.append(el("div",{class:"tavern-designer-page-split"},[
    el("main",{class:"tavern-designer-main tavern-designer-compact-stack"},[flowPanel,content]),
    inspector,
  ]));
  root.append(renderCapabilityHub({ panels:authorPanels, group:"author", title:copy("pages.designer.capability.title"), summary:copy("pages.designer.capability.summary"), handlers }));
  return root;
}

function advancedToolsPanel(problems,context,reports) {
  return el("details",{class:"tavern-designer-advanced-disclosure"},[
    el("summary",{},[
      el("span",{text:copy("pages.designer.renderDesigner.text.cc48ce6022")}),
      el("span",{text:label(context,copy("pages.designer.renderDesigner.message.e52b80e7a0"))}),
    ]),
    el("div",{class:"tavern-designer-advanced-body"},[
      problemPanel(problems),
      uiProfilePreview(context,reports),
    ]),
  ]);
}

function designerCapabilityPanels({ context, contentItems, problems, reports }) {
  const contextActions = rows(context?.available_actions).filter((action)=>action?.transportReady===true);
  const contentActions = contentItems.flatMap((item)=>rows(item?.available_actions)).filter((action)=>action?.transportReady===true);
  const actions = [...contextActions,...contentActions];
  const state = problems.length
    ? copy("pages.designer.capability.state.needs_check")
    : copy("pages.designer.capability.state.available");
  const stateToken = problems.length ? "warning" : "ready";
  return [
    {key:"world-package",group:"author",label:copy("pages.worlds.detail.title"),summary:copy("pages.designer.capability.world_package.summary"),state,stateToken,workspace:"worlds",facts:[{label:copy("pages.designer.capability.fact.content_boards"),value:contentItems.length},{label:copy("pages.designer.capability.fact.needs_check"),value:problems.length}]},
    {key:"author-edit",group:"author",label:copy("pages.worlds.detail.author_edit"),summary:copy("pages.designer.capability.author_edit.summary"),state:actions.length?copy("pages.designer.capability.state.actionable"):copy("pages.designer.capability.state.readonly"),stateToken:actions.length?"ready":"readonly",workspace:"designer",facts:[{label:copy("pages.designer.capability.fact.actions"),value:actions.length},{label:copy("pages.designer.capability.fact.current_world"),value:label(context)}]},
    {key:"author-artifact",group:"author",label:copy("pages.worlds.detail.author_artifact"),summary:copy("pages.designer.capability.author_artifact.summary"),state:reports.length?copy("pages.designer.capability.state.viewable"):copy("pages.designer.capability.state.no_artifacts"),stateToken:reports.length?"ready":"readonly",workspace:"author_jobs",facts:[{label:copy("pages.designer.capability.fact.reports"),value:reports.length},{label:copy("pages.designer.capability.fact.blockers"),value:problems.length}]},
    {key:"resolution",group:"author",label:copy("pages.designer.capability.resolution.label"),summary:copy("pages.designer.capability.resolution.summary"),state:contentItems.length?copy("pages.designer.capability.state.viewable"):copy("pages.designer.capability.state.no_content"),stateToken:contentItems.length?"ready":"readonly",workspace:"designer",facts:[{label:copy("pages.designer.capability.fact.content_sections"),value:contentItems.length},{label:copy("pages.designer.capability.fact.coverage_reports"),value:reports.length}]},
  ];
}

function sectionHeading(title,description="") {
  return el("header",{class:"tavern-designer-section-head"},[el("div",{},[el("h2",{text:title}),description?el("p",{text:description}):null])]);
}

function authorContext() {
  return el("div",{class:"tavern-page-toolbar-copy tavern-author-context-copy","aria-label":copy("pages.designer.renderDesigner.message.619976e313")},[
    el("h2",{text:copy("pages.designer.toolbar.title")}),
    el("p",{text:copy("pages.designer.toolbar.summary")} ),
  ]);
}

function flowDescriptors(item,index,contextActions) {
  const direct = rows(item.available_actions).filter((entry)=>entry?.transportReady===true);
  const all = [...direct,...contextActions];
  const matches = all.filter((entry)=>{
    const intent = String(entry.intent || "");
    if (index === 0) return intent === "designer.field.save" || intent === "designer.preset.save";
    if (index === 1) return intent.startsWith("resident_character.");
    if (index === 3) return intent === "designer.simulate";
    return !intent.startsWith("designer.") && !intent.startsWith("resident_character.");
  });
  const seen = new Set();
  return matches.filter((entry)=>{const key=entry.action_id||entry.intent;if(!key||seen.has(key))return false;seen.add(key);return true;});
}

function problemCard(item) {
  return el("article",{class:"tavern-designer-problem","data-state":stateMap(item.state)},[
    el("header",{},[el("strong",{text:label(item,copy("pages.designer.renderDesigner.label.be55279f1e"))}),renderStatusBadge({state:stateMap(item.state),label:item.state || copy("pages.designer.flowStep.message.8f31a776d5")})]),
    summary(item)?el("p",{text:summary(item)}):null,
  ]);
}

function problemPanel(problems) {
  const panel = el("section",{class:"tavern-designer-inspector-panel"},[sectionHeading(copy("pages.designer.rc8.333ef8f5d2"),copy("pages.designer.renderDesigner.text.cc48ce6022"))]);
  if (problems.length) panel.append(el("div",{class:"tavern-designer-problem-list"},problems.map(problemCard)));
  else panel.append(renderStatePanel({phase:"empty",emptyCopy:copy("pages.designer.renderDesigner.emptyCopy.c230891d45")}));
  return panel;
}

function matrixDetail(item) {
  const matrixRows = rows(item.rows);
  if (!matrixRows.length) return null;
  return el("details",{class:"tavern-designer-report-detail"},[
    el("summary",{text:copy("pages.designer.coverage_count",{p0:matrixRows.length})}),
    el("ul",{},matrixRows.map((row)=>el("li",{},[el("strong",{text:label(row,copy("pages.designer.rc8.2463c18bd2"))}),el("span",{text:rows(row.cells).map((cell)=>cell?.state).filter(Boolean).join(" · ")})]))),
  ]);
}

function reportCard(item,index) {
  const matrixRows = rows(item.rows);
  const reportLabel = label(item,matrixRows.length ? copy("pages.designer.rc8.a5c3e86bd8") : `${copy("pages.designer.renderDesigner.label.1e8ddd10ba")} ${index+1}`);
  const reportSummary = summary(item,matrixRows.length ? copy("pages.designer.coverage_summary",{p0:matrixRows.length}) : "");
  return el("article",{class:"tavern-designer-report"},[
    el("header",{},[el("strong",{text:reportLabel}),item.state?renderStatusBadge({state:stateMap(item.state),label:item.state}):null]),
    reportSummary?el("p",{text:reportSummary}):null,
    matrixDetail(item),
  ]);
}

function reportPanel(reports,context,modelSummary="") {
  const panel = el("section",{class:"tavern-designer-inspector-panel"},[sectionHeading(copy("pages.designer.rc8.87a02344c4"),copy("pages.designer.renderDesigner.label.ae72b532f4"))]);
  panel.append(el("div",{class:"tavern-designer-context-report"},[
    el("div",{},[
      el("small",{text:copy("pages.designer.renderDesigner.text.6ccb63ee27")}),
      el("strong",{text:label(context,copy("pages.designer.renderDesigner.message.e52b80e7a0"))}),
      summary(context,modelSummary) ? el("span",{text:summary(context,modelSummary)}):null,
    ]),
    renderStatusBadge({state:stateMap(context?.state),label:context?.state || copy("pages.designer.renderDesigner.message.c634a6dafd")}),
  ]));
  if (reports.length) panel.append(el("div",{class:"tavern-designer-report-list"},reports.map(reportCard)));
  else panel.append(renderStatePanel({phase:"empty",emptyCopy:copy("pages.designer.renderDesigner.emptyCopy.d45506a6f2")}));
  return panel;
}

const designerDensityLabels = Object.freeze({minimal:copy("pages.designer.rc8.75fc0214e4"),standard:copy("pages.designer.rc8.6bea77acef"),rich:copy("pages.designer.rc8.f54bab7a88")});
const detailLabels = Object.freeze({identity:copy("pages.designer.rc8.2a9c9e9976"),attributes:copy("pages.designer.rc8.86de52d178"),resources:copy("dialogs.session_detail.partyPanel.label.daa6d02a55"),statuses:copy("dialogs.session_detail.openReceipt.label.6320b4a872"),inventory:copy("pages.designer.rc8.7c89d6ebe1"),capabilities:copy("pages.worlds.renderWorlds.label.5c5374dd95"),relationships:copy("pages.designer.rc8.253aaff19a"),growth:copy("pages.designer.rc8.16cc0bf3dd"),history:copy("pages.designer.rc8.275feed2c1")});

function profileFrom(context,reports) {
  const direct = context.adaptive_ui || context.ui_profile || context.ui_profile_preview;
  if (direct && typeof direct === "object" && !Array.isArray(direct)) return direct;
  const report = reports.find((item)=>item?.ui_profile && typeof item.ui_profile === "object");
  return report?.ui_profile || null;
}

function uiProfilePreview(context,reports) {
  const profile = profileFrom(context,reports);
  const panel = el("section",{class:"tavern-designer-profile-preview"},[sectionHeading(copy("pages.designer.rc8.c8769fcbc1"),copy("pages.designer.rc8.8d5d6fbea0"))]);
  if (!profile) {
    const waitingForWorld = !context?.key || context?.state === "等待选择";
    panel.append(waitingForWorld
      ? renderStatePanel({phase:"empty",emptyCopy:copy("pages.designer.preview.waiting")})
      : renderStatePanel({phase:"partial",operation:copy("pages.designer.rc8.913816288e"),problem:{message:copy("pages.designer.rc8.fc6e085e82"),recovery:copy("pages.designer.rc8.0dbd7e8161")}}));
    return panel;
  }
  const density = profile.density_label || designerDensityLabels[profile.density];
  const facets = rows(profile.party?.identity_facets).map((entry)=>entry?.label).filter(Boolean);
  const lenses = rows(profile.lens_labels).filter(Boolean).length
    ? rows(profile.lens_labels).filter(Boolean)
    : rows(profile.live_lenses).map((entry)=>entry?.label).filter(Boolean);
  const visuals = rows(profile.visualizations).map((entry)=>entry?.title).filter(Boolean);
  const details = rows(profile.actor_detail?.sections).map((entry)=>detailLabels[entry]).filter(Boolean);
  const detailCount = Number.isInteger(profile.actor_detail_section_count) ? profile.actor_detail_section_count : null;
  const visualizationCount = Number.isInteger(profile.attribute_visualization_count) ? profile.attribute_visualization_count : null;
  const emptyPolicy = profile.empty_policy === "omit-unsupported" ? copy("pages.designer.rc8.64f3e49a07") : profile.empty_policy;
  const facts = [
    density ? [copy("pages.designer.rc8.23e662dc51"),density] : null,
    facets.length ? [copy("pages.designer.rc8.0e64dfa686"),facets.join("、")] : null,
    details.length ? [copy("pages.designer.rc8.d861fbf154"),details.join("、")] : detailCount !== null ? [copy("pages.designer.rc8.d861fbf154"),copy("pages.designer.section_count",{p0:detailCount})] : null,
    lenses.length ? [copy("pages.designer.rc8.3c2576ca8d"),lenses.join("、")] : null,
    visuals.length ? [copy("pages.designer.rc8.1c3a33f31f"),visuals.join("、")] : visualizationCount !== null ? [copy("pages.designer.rc8.91bc9e275a"),copy("pages.designer.item_count",{p0:visualizationCount})] : null,
    emptyPolicy ? [copy("pages.designer.rc8.51152e7acc"),emptyPolicy] : null,
  ].filter(Boolean);
  if (!facts.length) panel.append(renderStatePanel({phase:"empty",emptyCopy:copy("pages.designer.rc8.24a7e370df")}));
  else panel.append(el("dl",{class:"tavern-designer-profile-facts"},facts.map(([term,description])=>el("div",{},[el("dt",{text:term}),el("dd",{text:description})]))));
  return panel;
}
