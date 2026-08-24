import { copy } from "../copy/catalog.js";
// AstrBot rewrites module specifiers with a page-scoped asset token that is valid
// for 60 seconds. Register every page module while that token is fresh, then keep
// the renderers in memory. PageModel data and heavy detail payloads remain lazy.
import { renderDashboard } from "./dashboard.js";
import { renderSessions } from "./sessions.js";
import { renderTodo } from "./todo.js";
import { renderTendencies } from "./tendencies.js";
import { renderCharacters } from "./characters.js";
import { renderWorlds } from "./worlds.js";
import { renderDesigner } from "./designer.js";
import { renderAuthorJobs } from "./author-jobs.js";
import { renderLiveSession, renderSessionSelection } from "./live-session.js";
import { renderMemories } from "./memories.js";
import { renderAudit } from "./audit.js";
import { renderHealth } from "./health.js";
import { renderSettings } from "./settings.js";
import { renderModules } from "./modules.js";
import { renderAbout } from "./about.js";

const PAGE_RENDERERS = Object.freeze({
  dashboard: renderDashboard,
  sessions: renderSessions,
  todo: renderTodo,
  tendencies: renderTendencies,
  characters: renderCharacters,
  worlds: renderWorlds,
  designer: renderDesigner,
  author_jobs: renderAuthorJobs,
  session_detail: renderLiveSession,
  memories: renderMemories,
  audit: renderAudit,
  health: renderHealth,
  settings: renderSettings,
  modules: renderModules,
  about: renderAbout,
});

export const PAGE_KEYS = Object.freeze(Object.keys(PAGE_RENDERERS));
export { renderSessionSelection };
export async function pageRenderer(key) {
  const renderer = PAGE_RENDERERS[key];
  if (typeof renderer !== "function") throw new Error(`未知控制台页面：${key}`);
  return renderer;
}
if (PAGE_KEYS.length !== 15) throw new Error(copy("pages.index.rc8.24812911c6"));
