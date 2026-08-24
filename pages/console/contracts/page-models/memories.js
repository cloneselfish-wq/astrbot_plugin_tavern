import { createPageAdapter } from "./core.js";
export const toMemoriesPageModel=createPageAdapter({workspace:"memories",spec:[["facts","MemoryDataList",true],["pagination","Pagination",false]],paths:{facts:["items"],pagination:["pagination"]},filterSources:{scope:"scopes",importance:"importances",tag:"tags",governance:"governances"}});
