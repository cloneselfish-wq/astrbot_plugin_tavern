import { createPageAdapter } from "./core.js";
export const toModulesPageModel=createPageAdapter({workspace:"modules",spec:[["modules","ModuleCardGrid",true],["coverage","CapabilitySummary",false],["pagination","Pagination",false]],paths:{modules:["items"],coverage:["coverage","context"],pagination:["pagination"]},filterSources:{status:"statuses",layer:"layers",consumer:"consumers"}});
