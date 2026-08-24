import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderBusinessCard } from "../components/cards.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { renderFilterBar } from "../components/filters.js";
import { renderPagination } from "../components/pagination.js";
import { formatUtc8Minute } from "../components/time.js";
import { openConfirm } from "../dialogs/confirm-dialog.js";
import { openDetail } from "../dialogs/detail-dialog.js";
import { el, label, pageRoot, rows, stateNotice, summary, value } from "./shared.js";
import { renderProgress } from "../visualizations/progress.js";

const statusState=(state)=>/执行|运行/.test(state)?"running":/恢复|重试/.test(state)?"recovering":/完成/.test(state)?"ready":/失败|停止/.test(state)?"error":"waiting";
const filterLabels={world_key:copy("pages.author_jobs.statusState.message.33650a3695"),q:copy("pages.author_jobs.statusState.message.5d02c355dc"),status:copy("pages.author_jobs.statusState.message.6320b4a872"),type:copy("pages.author_jobs.statusState.message.ba40014ff4"),time:copy("pages.author_jobs.statusState.message.0a5f9a8929"),page_size:copy("pages.author_jobs.statusState.message.7a35603b46")};
const optionSource={world_key:"world_options",status:"statuses",type:"types",time:"times"};
const descriptorFor=(item)=>rows(item?.available_actions).find((action)=>action?.transportReady===true&&["author_job.cancel","author_job.retry"].includes(action.intent));
const createDescriptorFor=(model)=>rows(model?.actions).find((action)=>action?.transportReady===true&&action.intent==="author_job.create");
const bind=(descriptor,item)=>descriptor?{...descriptor,object_key:item.key,id:descriptor.action_id||descriptor.intent}:null;

function filters(model){return model.filters.filter((field)=>field.name!=="cursor").map((field)=>({...field,label:filterLabels[field.name]||field.name,type:field.name==="q"?"search":["world_key","page_size"].includes(field.name)?"select":field.type,options:field.name==="page_size"?[10,20,40,80].map((size)=>({value:size,label:copy("pages.author_jobs.filters.label.8eaa29c22a", {p0: size})})):[{value:"",label:copy("pages.author_jobs.filters.label.5c55a67935")},...rows(field.options)]}));}
function update(handlers, values,{resetCursor=true}={}){handlers.updateLocation?.({filters:{...(handlers.navigation?.filters||{}),...values,...(resetCursor?{cursor:""}:{})}});return handlers.refresh?.();}
function progress(item){return renderProgress({label:copy("pages.author_jobs.progress.label.40ad740f5c", {p0: label(item)}),current:item.progress_current,total:item.progress_total,state:item.state_label||""});}
function executeJob(descriptor,item,handlers,opener){const action=bind(descriptor,item);if(descriptor.intent!=="author_job.cancel")return handlers.actions.execute(action,{opener});const idempotencyKey=crypto.randomUUID();return openConfirm(handlers.dialogs,{opener,operation:copy("pages.author_jobs.executeJob.operation.1c7193b256"),impact:copy("pages.author_jobs.executeJob.message.0518423c26"),unchanged:copy("pages.author_jobs.executeJob.message.ee523ae0fa"),automatic:copy("pages.author_jobs.executeJob.message.7408dcef39"),recovery:copy("pages.author_jobs.executeJob.recovery.9ace888225"),returnCheck:copy("pages.author_jobs.executeJob.message.3443684d39"),confirmLabel:copy("pages.author_jobs.executeJob.message.2dc132ba07"),intent:{id:"author_job.cancel"},idempotencyKey,onConfirm:({idempotencyKey:confirmedKey})=>handlers.actions.execute(action,{opener,idempotencyKey:confirmedKey})});}
function selectedWorld(model){return model.filters.find((field)=>field.name==="world_key")?.value||"";}
function executeCreate(descriptor,worldKey,handlers,opener){
  if(!descriptor||!worldKey||!handlers.actions?.execute)return null;
  return handlers.actions.execute({...descriptor,object_key:worldKey,id:descriptor.action_id||descriptor.intent},{opener,input:{job_type:"full_preflight"},idempotencyKey:crypto.randomUUID()});
}
function createAction(model,handlers){
  const worldKey=selectedWorld(model);
  const descriptor=createDescriptorFor(model);
  const canCreate=Boolean(worldKey&&descriptor&&!model.readonly&&handlers.actions?.execute);
  const message=!worldKey
    ?copy("pages.author_jobs.rc8.c7724461bc")
    :canCreate
      ?copy("pages.author_jobs.rc8.06e378e089")
      :copy("pages.author_jobs.rc8.51ebd574cd");
  const button=renderButton({
    variant:"primary",
    label:worldKey?copy("pages.author_jobs.rc8.3c6c615a59"):copy("pages.author_jobs.rc8.a5f20c06a8"),
    disabledReason:canCreate?"":message,
    intent:canCreate?{id:descriptor.intent}:null,
    onActivate:(_intent,event)=>canCreate&&executeCreate(descriptor,worldKey,handlers,event.currentTarget),
  });
  button.dataset.authorAction="create-full-preflight";
  button.title=message;
  return button;
}
function jobCard(item,handlers){
  const descriptor=descriptorFor(item);
  const failed=/失败|停止/.test(item.state||"");
  const actions=[renderButton({variant:"secondary",label:jobDetailLabel(failed),onActivate:(_intent,event)=>openJobDetail(item,handlers,event.currentTarget,failed)})];
  if(descriptor)actions.push(renderButton({variant:descriptor.intent==="author_job.cancel"?"danger":"secondary",label:descriptor.label,intent:bind(descriptor,item),onActivate:(_intent,event)=>executeJob(descriptor,item,handlers,event.currentTarget)}));
  const body=el("div",{class:"tavern-job-body"},[
    progress(item),
    jobFacts(item),
    nextStepSummary(item),
  ]);
  return renderBusinessCard({kind:"author-job",className:"tavern-author-job-card",opaqueKey:item.key||label(item),kicker:item.type_label||label(item),title:label(item),summary:summary(item),state:{state:statusState(item.state),label:item.state||copy("pages.author_jobs.jobCard.message.251bc40e70")},body,actions});
}

/*
 * Action consumer source locations are owned by shared migration manifests.
 * Keep this gap until the integration owner regenerates those manifests after
 * all page waves land. Runtime behavior does not depend on line positions.
 *
 * This page consumes only server-projected PageModel sections and
 * transport-ready world-create or task-row cancel/retry descriptors.
 *
 * Do not place business logic, endpoint calls, or stable identifiers here.
 *
 * See the action-consumer-registry contract.
 * The renderer below remains the sole page entry point.
 */
export function renderAuthorJobs(model,handlers={}){
  const root=pageRoot(model,"tavern-author-jobs");
  const items=rows(value(model,"jobs"));
  root.append(stateNotice(model,copy("pages.author_jobs.renderAuthorJobs.message.5958720e18")));
  root.append(el("div",{class:"tavern-page-toolbar"},[
    el("div",{class:"tavern-page-toolbar-copy"},[el("h2",{text:model.title||copy("pages.author_jobs.renderAuthorJobs.message.5c8d5c1278")}),model.summary?el("p",{text:model.summary}):null]),
    el("div",{class:"tavern-author-job-toolbar-actions"},[
      renderFilterBar({workspace:"author_jobs",fields:filters(model),values:Object.fromEntries(model.filters.map((field)=>[field.name,field.value])),onApply:(values)=>update(handlers,values),onClear:()=>{handlers.updateLocation?.({filters:{}});return handlers.refresh?.();}}),
      createAction(model,handlers),
    ]),
  ]));
  const ordered=items;
  const grid=el("section",{class:"tavern-card-grid tavern-job-grid","aria-label":copy("pages.author_jobs.renderAuthorJobs.message.5c8d5c1278")},ordered.map((item)=>jobCard(item,handlers)));
  if(!items.length&&["ready","empty"].includes(model.phase))grid.append(renderStatePanel({phase:"empty",emptyCopy:selectedWorld(model)?copy("pages.author_jobs.rc8.446a876f62"):copy("pages.author_jobs.rc8.5e3dffa032")}));
  root.append(grid);
  if(model.pagination)root.append(renderPagination({workspace:"author_jobs",...model.pagination,onPage:(cursor)=>update(handlers,{cursor},{resetCursor:false})}));
  return root;
}

function integer(value) {
  return Number.isInteger(value) ? value : null;
}

function jobFacts(item) {
  const facts=[{label:copy("pages.author_jobs.jobCard.type"),value:item.type_label||label(item)}];
  const attempts=integer(item.attempts);
  const maximum=integer(item.max_attempts);
  if(attempts!==null||maximum!==null)facts.push({label:copy("pages.author_jobs.jobCard.text.58a823852f"),value:attempts!==null&&maximum!==null?copy("pages.author_jobs.jobCard.text.d833987afd",{p0:attempts,p1:maximum}):String(attempts??maximum)});
  if(item.updated_at)facts.push({label:copy("pages.author_jobs.jobCard.text.0a5f9a8929"),value:formatUtc8Minute(item.updated_at)});
  return el("dl",{class:"tavern-job-facts"},facts.flatMap((fact)=>[el("dt",{text:fact.label}),el("dd",{text:fact.value})]));
}

function failurePanel(item,failed) {
  if(!failed)return null;
  return el("section",{class:"tavern-job-recovery"},[
    el("h4",{text:copy("pages.author_jobs.jobCard.failure")}),
    el("p",{text:item.failure_reason||copy("pages.author_jobs.jobCard.failure_unknown")}),
  ]);
}

function nextStepSummary(item) {
  const automaticFirst=/恢复|重试/.test(item.state||"");
  const showsAutomatic=Boolean(item.automatic_action)&&(automaticFirst||!item.next_step);
  const next=automaticFirst?(item.automatic_action||item.next_step):(item.next_step||item.automatic_action);
  if(!next)return null;
  const heading=showsAutomatic?copy("pages.author_jobs.jobCard.automatic"):copy("pages.author_jobs.jobCard.next_step");
  return el("section",{class:"tavern-job-next-summary"},[
    el("strong",{text:heading}),
    el("p",{text:next}),
  ]);
}

function recoveryGuide(item) {
  const automatic=item.automatic_action;
  const next=item.next_step;
  if(!automatic&&!next)return renderStatePanel({phase:"partial",operation:copy("pages.author_jobs.rc8.bc0027f392"),problem:{message:copy("pages.author_jobs.rc8.d778503161"),recovery:copy("pages.author_jobs.rc8.6fe665935c")}});
  return el("section",{class:"tavern-job-next"},[
    automatic?el("p",{},[el("strong",{text:copy("pages.author_jobs.jobCard.automatic")}),document.createTextNode(automatic)]):null,
    next?el("p",{},[el("strong",{text:copy("pages.author_jobs.jobCard.next_step")}),document.createTextNode(next)]):null,
  ]);
}

function artifactSection(item) {
  const artifacts=rows(item.artifacts);
  const section=el("section",{class:"tavern-job-artifacts","data-phase":artifacts.length?"ready":"empty"},[el("h4",{text:copy("pages.author_jobs.jobCard.artifacts")})]);
  if(!artifacts.length){section.append(el("p",{class:"tavern-job-artifacts-empty",text:copy("pages.author_jobs.rc8.8cac1d37a5")}));return section;}
  section.append(el("ul",{},artifacts.map((artifact)=>el("li",{},[
    el("div",{},[el("strong",{text:artifact.label||copy("pages.author_jobs.jobCard.artifact_unknown")}),artifact.summary?el("span",{text:artifact.summary}):null]),
    artifact.state||artifact.updated_at?el("small",{text:[artifact.state,artifact.updated_at ? formatUtc8Minute(artifact.updated_at) : ""].filter(Boolean).join(" · ")}):null,
  ]))));
  return section;
}

function jobGuidancePanel(item,failed) {
  return el("section",{class:"tavern-job-detail-panel","data-phase":item.automatic_action||item.next_step?"ready":"partial"},[
    failurePanel(item,failed),
    recoveryGuide(item),
  ]);
}

function jobDetailLabel(failed) {
  const subject=failed?copy("pages.author_jobs.jobCard.failure"):copy("pages.author_jobs.jobCard.artifact_unknown");
  return copy("pages.worlds.detail.open_jobs").replace(copy("pages.author_jobs.renderAuthorJobs.message.5c8d5c1278"),subject);
}

function openJobDetail(item,handlers,opener,failed) {
  if(!handlers.dialogs?.openDialog)return;
  const guidanceLabel=failed?copy("pages.author_jobs.jobCard.failure"):copy("pages.author_jobs.jobCard.next_step");
  const facts=[
    {label:copy("pages.author_jobs.jobCard.type"),value:item.type_label||label(item)},
    {label:copy("pages.author_jobs.statusState.message.6320b4a872"),value:item.state||copy("pages.author_jobs.jobCard.message.251bc40e70")},
    item.updated_at?{label:copy("pages.author_jobs.jobCard.text.0a5f9a8929"),value:formatUtc8Minute(item.updated_at)}:null,
  ].filter(Boolean);
  return openDetail(handlers.dialogs,{
    objectKey:item.key,
    opener,
    title:label(item),
    kicker:item.type_label||label(item),
    specialization:"author-job",
    tabs:[
      {id:"guidance",label:guidanceLabel},
      {id:"artifacts",label:copy("pages.author_jobs.jobCard.artifacts")},
    ],
    activeTab:failed?"guidance":"artifacts",
    summaryFacts:facts,
    footerLabel:copy("components.capability_hub.close_details"),
    permissions:{guidance:true,artifacts:true},
    lazyPanelLoader:async (tab)=>tab==="artifacts"?artifactSection(item):jobGuidancePanel(item,failed||Boolean(item.failure_reason)),
  });
}
