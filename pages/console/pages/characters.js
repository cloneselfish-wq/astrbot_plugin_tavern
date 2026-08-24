import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderMetricStrip } from "../components/cards.js";
import { renderPagination } from "../components/pagination.js";
import { formatUtc8Minute } from "../components/time.js";
import { openDetail } from "../dialogs/detail-dialog.js";
import { openEditor } from "../dialogs/editor-dialog.js";
import { el,label,pageRoot,rows,stateNotice,summary,value } from "./shared.js";

const badgeState=(label)=>label==="待审核"?"warning":label==="已批准"?"ready":label==="填写中"?"waiting":label==="已退回"?"error":"readonly";
function update(runtime,filters,{resetCursor=false}={}){const next={...filters};if(resetCursor)delete next.cursor;runtime.updateLocation?.({filters:next},{replace:false});return runtime.refresh?.();}
function executeDescriptor(descriptor,item,runtime,opener){const action={...descriptor,object_key:item.key,target_key:item.key};if(!rows(action.fields).length)return runtime.actions?.execute(action,{opener});return openEditor(runtime.dialogs,{objectKey:item.key,revision:action.expected_revision,fields:rows(action.fields),opener,title:action.label,preview:()=>({summary:action.description||action.label}),submit:({draft,idempotencyKey})=>runtime.actions?.execute(action,{opener,input:draft,idempotencyKey})});}
function actions(item,runtime){if(item.readonly)return[];return rows(item.available_actions).filter((a)=>a?.transportReady===true&&a.intent&&Number.isInteger(a.expected_revision)).map((descriptor)=>renderButton({variant:/通过/.test(descriptor.label)?"primary":/退场|取消/.test(descriptor.label)?"danger":"secondary",label:descriptor.label,intent:{id:descriptor.intent},onActivate:(_intent,event)=>executeDescriptor(descriptor,item,runtime,event.currentTarget)}));}
function characterSummary(item){return [summary(item),item.submitted_at?copy("pages.characters.submitted_at",{p0:formatUtc8Minute(item.submitted_at)}):""].filter(Boolean).join(" · ");}
function characterName(item){const name=label(item).replace(/^[「〔【『<〈《]+|[」〕】』>〉》]+$/g,"");return `「${name}」`;}
function definition(detail){return el("div",{class:"tavern-character-definition-item"},[el("dt",{text:detail.label}),el("dd",{},[el("strong",{text:detail.summary}),detail.state?el("small",{text:detail.state}):null])]);}
function summaryLine(name,content){return el("div",{class:"tavern-character-summary-line"},[el("span",{text:name}),el("strong",{text:content})]);}
function optionalLine(name,content){return content?summaryLine(name,content):null;}
function characterDetailRows(item){
  return [
    item.player_label?{label:copy("pages.characters.card.player"),summary:item.player_label}:null,
    item.concept_label?{label:copy("pages.characters.card.concept"),summary:item.concept_label}:null,
    {label:copy("pages.characters.renderCharacters.label.49ef0b6964"),summary:item.stage||copy("pages.characters.renderCharacters.message.94b085f6d2")},
    {label:copy("pages.characters.renderCharacters.label.0a9b2948a5"),summary:item.pending_fields?copy("pages.characters.renderCharacters.message.980fda45d8",{p0:item.pending_fields}):copy("pages.characters.renderCharacters.message.484d556139")},
    item.strengths_summary?{label:copy("pages.characters.card.strengths"),summary:item.strengths_summary}:null,
    item.limits_summary?{label:copy("pages.characters.card.limits"),summary:item.limits_summary}:null,
    {label:copy("pages.characters.renderCharacters.label.7b66be73c7"),summary:item.ready?copy("pages.characters.renderCharacters.message.8bef70b009"):copy("pages.characters.renderCharacters.message.19f9e72de5")},
    ...rows(item.detail),
  ].filter(Boolean);
}
export function renderCharacterDetail(item){
  return el("div",{class:"tavern-character-detail-view","data-phase":"ready"},[
    el("dl",{class:"tavern-character-definition-grid"},characterDetailRows(item).map(definition)),
  ]);
}
function openCharacterDetail(item,runtime,opener){
  return openDetail(runtime.dialogs,{objectKey:item.key,opener,title:characterName(item),specialization:"actor",activeTab:"profile",tabs:[{id:"profile",label:copy("pages.sessions.group.details")}],lazyPanelLoader:()=>renderCharacterDetail(item),permissions:{profile:true}});
}
function filterSelect({name,labelText,options,filters,runtime}){
  return el("select",{class:"tavern-control",name,"aria-label":labelText,onChange:(event)=>update(runtime,{...filters,[name]:event.currentTarget.value},{resetCursor:true})},options.map((option)=>el("option",{value:option.value,selected:String(filters[name]||"")===String(option.value),text:option.label})));
}
function secondarySearch(runtime,filters){
  const searchLabel=copy("pages.characters.renderCharacters.label.f1c9a873a1");
  const input=el("input",{class:"tavern-control",name:"q",type:"search",value:filters.q||"",placeholder:searchLabel,"aria-label":searchLabel});
  const form=el("form",{class:"tavern-character-search-form",onSubmit:(event)=>{event.preventDefault();update(runtime,{...filters,q:event.currentTarget.elements.q.value},{resetCursor:true});}},[
    input,
    renderButton({variant:"secondary",label:copy("components.primitives.renderFilterBar.label.dd0a97ab36"),buttonType:"submit"}),
    renderButton({variant:"quiet",label:copy("components.primitives.renderFilterBar.label.bce2377283"),onActivate:()=>{input.value="";update(runtime,{...filters,q:""},{resetCursor:true});}}),
  ]);
  return el("details",{class:"tavern-character-secondary-search"},[el("summary",{text:searchLabel}),form]);
}
function reviewCard(item,index,runtime){
  const detail=rows(item.detail);
  const available=actions(item,runtime);
  const cardActions=[renderButton({variant:"quiet",label:copy("pages.sessions.group.details"),onActivate:(_intent,event)=>openCharacterDetail(item,runtime,event.currentTarget)}),...available];
  return el("article",{class:"tavern-character-review-card","data-review":`character-${index + 1}`},[
    el("div",{class:"tavern-character-card-body"},[
      el("header",{class:"tavern-character-card-header"},[
        el("div",{},[el("h3",{text:characterName(item)}),el("p",{text:characterSummary(item)})]),
        el("span",{class:"tavern-character-state","data-state":badgeState(item.state),text:item.state||copy("pages.characters.renderCharacters.message.ff6faf3070")}),
      ]),
      el("div",{class:"tavern-character-summary-list"},[
        optionalLine(copy("pages.characters.card.player"),item.player_label),optionalLine(copy("pages.characters.card.concept"),item.concept_label),
        summaryLine(copy("pages.characters.renderCharacters.label.49ef0b6964"),item.stage||copy("pages.characters.renderCharacters.message.94b085f6d2")),
        summaryLine(copy("pages.characters.renderCharacters.label.0a9b2948a5"),item.pending_fields?copy("pages.characters.renderCharacters.message.980fda45d8",{p0:item.pending_fields}):copy("pages.characters.renderCharacters.message.484d556139")),
        optionalLine(copy("pages.characters.card.strengths"),item.strengths_summary),optionalLine(copy("pages.characters.card.limits"),item.limits_summary),
        summaryLine(copy("pages.characters.renderCharacters.label.7b66be73c7"),item.ready?copy("pages.characters.renderCharacters.message.8bef70b009"):copy("pages.characters.renderCharacters.message.19f9e72de5")),
      ]),
      detail.length?el("details",{class:"tavern-character-disclosure"},[
        el("summary",{},[el("span",{text:copy("pages.sessions.group.details")}),el("span",{text:item.state||copy("pages.characters.renderCharacters.message.ff6faf3070")})]),
        el("div",{class:"tavern-character-disclosure-body"},[el("dl",{class:"tavern-character-definition-grid"},detail.map(definition))]),
      ]):null,
      el("div",{class:"tavern-character-card-actions","aria-label":copy("pages.characters.card.actions_aria",{p0:label(item)})},[
        el("div",{class:"tavern-character-action-heading"},[el("strong",{text:copy("pages.characters.card.review_actions")}),el("span",{text:item.readonly?(item.readonly_reason||copy("pages.characters.card.readonly")):available.length?item.state||copy("pages.characters.card.actionable"):copy("pages.characters.card.no_actions")})]),
        el("div",{class:"tavern-character-action-row"},cardActions),
      ]),
    ]),
  ]);
}

export function renderCharacters(model,runtime={}){
  const root=pageRoot(model,"tavern-characters");root.append(stateNotice(model,copy("pages.characters.renderCharacters.message.69fb967967")));
  const summarySection=value(model,"summary")||{};
  const sessionFilter=rows(model.filters).find((field)=>field.name==="session_key");
  const sessionOptions=sessionFilter?.options;
  const statusOptions=rows(model.filters).find((field)=>field.name==="status")?.options;
  const metrics=rows(summarySection.metrics);
  const privacy=value(model,"privacy")||{};
  const filters=runtime.navigation?.filters||{};
  const toolbar=characterBlock("filters",el("section",{class:"page-toolbar tavern-characters-toolbar"},[
    el("div",{class:"tavern-page-toolbar-copy"},[el("h2",{text:model.title||copy("shell.registry.module.label.f01045feb6")}),el("p",{text:model.summary||copy("pages.characters.renderCharacters.message.9713d3cb21")})]),
    el("div",{class:"filter-row tavern-character-primary-filters"},[
      filterSelect({name:"session_key",labelText:sessionFilter?.label||copy("shell.registry.module.label.f01045feb6"),options:[{value:"",label:sessionFilter?.label||copy("shell.registry.module.label.f01045feb6")},...rows(sessionOptions)],filters,runtime}),
      filterSelect({name:"status",labelText:copy("pages.characters.renderCharacters.label.e355214630"),options:[{value:"",label:copy("pages.characters.renderCharacters.label.0a379c1e73")},...rows(statusOptions)],filters,runtime}),
    ]),
  ]),"ready");
  root.append(toolbar);
  root.append(secondarySearch(runtime,filters));
  if(metrics.length)root.append(characterBlock("queue-metrics",el("section",{class:"tavern-characters-queue"},[renderMetricStrip({workspace:"characters",metrics:metrics.map((item,index)=>({key:item.key||`queue-${index}`,label:item.label,value:item.value}))})]),"ready"));
  const cards=rows(value(model,"review_cards"));
  const reviewCards=cards.map((item,index)=>reviewCard(item,index,runtime));
  const grid=characterBlock("review-card-grid",el("section",{class:"tavern-character-review","aria-label":copy("pages.characters.renderCharacters.message.9713d3cb21")},[cards.length?el("div",{class:"tavern-character-grid"},reviewCards):el("p",{class:"tavern-result",text:copy("pages.characters.renderCharacters.text.f58881cae3")})]),cards.length?"ready":"empty");
  root.append(grid);
  root.append(characterBlock("privacy-boundary",el("section",{class:"source-note tavern-privacy-notice tavern-characters-privacy","aria-label":privacy.label||copy("pages.characters.privacy.heading")},[el("h2",{text:privacy.label||copy("pages.characters.privacy.heading")}),el("p",{text:privacy.summary||copy("pages.characters.privacy.fallback")}),privacy.state?el("span",{class:"tavern-privacy-state",text:privacy.state}):null]),privacy.summary?"ready":"partial"));
  if(model.pagination)root.append(el("section",{class:"tavern-characters-pagination"},[renderPagination({workspace:"characters",cursor:runtime.navigation?.filters?.cursor,nextCursor:model.pagination.next_cursor,previousCursor:model.pagination.previous_cursor,hasMore:model.pagination.has_more,rangeLabel:copy("pages.characters.renderCharacters.message.f1dacfa7de",{p0:model.pagination.visible_from||0,p1:model.pagination.visible_to||0,p2:model.pagination.total==null?"":copy("common.pagination.total.message",{p0:model.pagination.total})}),onPage:(cursor)=>update(runtime,{...(runtime.navigation?.filters||{}),cursor})})]));
  if(model.phase==="permission")root.replaceChildren(stateNotice(model,copy("pages.characters.renderCharacters.message.69fb967967")),...["filters","queue-metrics","review-card-grid","privacy-boundary"].map((id)=>characterBlock(id,el("section",{class:"tavern-characters-redacted"}),"permission")));
  return root;
}

function characterBlock(id,node,state){node.setAttribute("id",`tavern-characters-${id}`);node.setAttribute("data-block",id);node.setAttribute("data-state",state);node.setAttribute("data-testid",`tavern-block-characters-${id}`);return node;}
