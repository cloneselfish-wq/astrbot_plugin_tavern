import { toAboutPageModel } from "./page-models/about.js";
import { toAuditPageModel } from "./page-models/audit.js";
import { toAuthorJobsPageModel } from "./page-models/author-jobs.js";
import { toCharactersPageModel } from "./page-models/characters.js";
import { toDashboardPageModel } from "./page-models/dashboard.js";
import { toDesignerPageModel } from "./page-models/designer.js";
import { toHealthPageModel } from "./page-models/health.js";
import { toLiveSessionPageModel } from "./page-models/live-session.js";
import { toMemoriesPageModel } from "./page-models/memories.js";
import { toModulesPageModel } from "./page-models/modules.js";
import { toSessionsPageModel } from "./page-models/sessions.js";
import { toSettingsPageModel } from "./page-models/settings.js";
import { toTendenciesPageModel } from "./page-models/tendencies.js";
import { toTodoPageModel } from "./page-models/todo.js";
import { toWorldsPageModel } from "./page-models/worlds.js";

export {
  toAboutPageModel, toAuditPageModel, toAuthorJobsPageModel,
  toCharactersPageModel, toDashboardPageModel, toDesignerPageModel,
  toHealthPageModel, toLiveSessionPageModel, toMemoriesPageModel,
  toModulesPageModel, toSessionsPageModel, toSettingsPageModel,
  toTendenciesPageModel, toTodoPageModel, toWorldsPageModel,
};

export const PAGE_MODEL_ADAPTERS = Object.freeze({
  dashboard: toDashboardPageModel,
  sessions: toSessionsPageModel,
  todo: toTodoPageModel,
  tendencies: toTendenciesPageModel,
  characters: toCharactersPageModel,
  worlds: toWorldsPageModel,
  designer: toDesignerPageModel,
  author_jobs: toAuthorJobsPageModel,
  session_detail: toLiveSessionPageModel,
  memories: toMemoriesPageModel,
  audit: toAuditPageModel,
  health: toHealthPageModel,
  settings: toSettingsPageModel,
  modules: toModulesPageModel,
  about: toAboutPageModel,
});
