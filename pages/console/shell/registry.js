import { copy } from "../copy/catalog.js";
const roles = Object.freeze({
  everyone: Object.freeze(["admin", "author", "host", "player", "readonly"]),
  operators: Object.freeze(["admin", "host"]),
  creators: Object.freeze(["admin", "author"]),
  admins: Object.freeze(["admin"]),
  player: Object.freeze(["player"]),
});

export const NAVIGATION_GROUPS = Object.freeze([
  { label: copy("shell.registry.module.label.75b269496f"), items: ["dashboard", "sessions", "todo", "tendencies", "characters"] },
  { label: copy("shell.registry.module.label.bd523d114d"), items: ["worlds", "designer", "author_jobs", "session_detail", "memories"] },
  { label: copy("shell.registry.module.label.80ff737418"), items: ["audit", "health", "settings", "modules", "about"] },
]);

export const WORKSPACES = Object.freeze({
  dashboard: { label: copy("shell.registry.module.label.a33db57305"), pageTitle: copy("pages.dashboard.renderDashboard.text.faecd366eb"), roles: roles.everyone, endpoint: "dashboard/surfaces/dashboard" },
  sessions: { label: copy("shell.registry.module.label.f01045feb6"), roles: roles.operators, endpoint: "dashboard/surfaces/sessions" },
  todo: { label: copy("shell.registry.module.label.48b5ebba62"), roles: roles.operators, endpoint: "dashboard/surfaces/todo" },
  tendencies: { label: copy("shell.registry.module.label.046bdc069e"), roles: roles.player, endpoint: "dashboard/surfaces/tendencies" },
  characters: { label: copy("shell.registry.module.label.538426df39"), roles: roles.operators, endpoint: "dashboard/surfaces/characters" },
  worlds: { label: copy("shell.registry.module.label.8cbd5ffd49"), roles: roles.creators, endpoint: "dashboard/surfaces/worlds" },
  designer: { label: copy("shell.registry.module.label.4807145979"), roles: roles.creators, endpoint: "dashboard/surfaces/designer" },
  author_jobs: { label: copy("shell.registry.module.label.5c8d5c1278"), roles: roles.creators, endpoint: "dashboard/surfaces/author_jobs" },
  session_detail: { label: copy("shell.registry.module.label.fee03c832e"), roles: roles.everyone, endpoint: "dashboard/session-summary" },
  memories: { label: copy("shell.registry.module.label.b80e99482a"), roles: roles.operators, endpoint: "dashboard/surfaces/memories" },
  audit: { label: copy("shell.registry.module.label.fef09b552b"), roles: roles.admins, endpoint: "dashboard/surfaces/audit" },
  health: { label: copy("shell.registry.module.label.dba13c2886"), roles: roles.admins, endpoint: "dashboard/surfaces/health" },
  settings: { label: copy("shell.registry.module.label.7f4a0f0636"), roles: roles.admins, endpoint: "dashboard/surfaces/settings" },
  modules: { label: copy("shell.registry.module.label.b9c928ec20"), roles: roles.creators, endpoint: "dashboard/surfaces/modules" },
  about: { label: copy("shell.registry.module.label.429d8c4b4c"), roles: roles.everyone, endpoint: "dashboard/surfaces/about" },
});

// These workspaces remain registered for a later repair cycle, but are not
// exposed or routable while their authoring flows are temporarily withdrawn.
const TEMPORARILY_HIDDEN_WORKSPACES = new Set(["designer", "author_jobs"]);

const navigationItems = NAVIGATION_GROUPS.flatMap((group) => group.items);
if (
  Object.keys(WORKSPACES).length !== 15
  || navigationItems.length !== 15
  || new Set(navigationItems).size !== 15
  || navigationItems.some((workspace) => !WORKSPACES[workspace])
) {
  throw new Error("The RC8 navigation registry must cover fifteen unique workspaces");
}

export function principalRoles(principal = {}) {
  const result = new Set(Array.isArray(principal.roles) ? principal.roles.map(String) : []);
  for (const [flag, role] of [["is_admin","admin"],["is_author","author"],["is_host","host"],["is_player","player"],["is_readonly","readonly"]]) if (principal[flag]) result.add(role);
  if (!result.size) result.add("readonly");
  return result;
}

export function canAccess(workspace, principal) {
  const current = principalRoles(principal);
  return (WORKSPACES[workspace]?.roles || []).some((role) => current.has(role));
}

export function visibleGroups(principal) {
  return NAVIGATION_GROUPS
    .map((group) => ({
      ...group,
      items: group.items.filter((workspace) => (
        !TEMPORARILY_HIDDEN_WORKSPACES.has(workspace)
        && canAccess(workspace, principal)
      )),
    }))
    .filter((group) => group.items.length);
}

// Counts are derived after role projection so hidden workspaces never leak through badges.
export function visibleWorkspaceCount(principal) {
  return visibleGroups(principal).reduce((total, group) => total + group.items.length, 0);
}
