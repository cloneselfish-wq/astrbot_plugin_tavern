import { createPageAdapter } from "./core.js";

const adaptAuthorJobs=createPageAdapter({workspace:"author_jobs",spec:[["jobs","JobCardGrid",true],["pagination","Pagination",false]],paths:{jobs:["items"],pagination:["pagination"]},filterSources:{world_key:"world_options",status:"statuses",type:"types",time:"times"}});

export function toAuthorJobsPageModel(envelope={},runtime={}) {
  const model=adaptAuthorJobs(envelope,runtime);
  const world=model.filters.find((field)=>field.name==="world_key")?.value;
  return {
    ...model,
    actions:world?model.actions.filter((action)=>action?.intent==="author_job.create"&&action?.transportReady===true):[],
  };
}
