import { copy } from "../copy/catalog.js";
import { icon } from "../visualizations/icons.js";
import { principalRoles, visibleGroups, visibleWorkspaceCount, WORKSPACES } from "./registry.js";
import { formatUtc8Minute } from "../components/time.js";

const SHELL_ICONS = Object.freeze({
  dashboard: "home", sessions: "story", todo: "bell", tendencies: "user",
  characters: "user", worlds: "worlds", designer: "worlds", author_jobs: "story",
  session_detail: "bell", memories: "story", audit: "story", health: "health",
  settings: "settings", modules: "settings", about: "shield",
});

const SHELL_GLYPHS = Object.freeze({
  menu: ['<path d="M4 7h16M4 12h16M4 17h16"/>', "1.8"],
  home: ['<path d="m3 11 9-7 9 7v9H7v-7h10v7"/>', "1.7"],
  story: ['<path d="M5 4h11a3 3 0 0 1 3 3v13H7a3 3 0 0 1-3-3V5a1 1 0 0 1 1-1Zm2 4h8M7 12h8"/>', "1.7"],
  worlds: ['<circle cx="12" cy="12" r="9"/><path d="M3.5 9h17M3.5 15h17M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>', "1.7"],
  user: ['<circle cx="12" cy="8" r="4"/><path d="M4.5 20c.8-4 3.3-6 7.5-6s6.7 2 7.5 6"/>', "1.7"],
  bell: ['<path d="M6 9a6 6 0 0 1 12 0c0 7 3 7 3 7H3s3 0 3-7Zm4 10h4"/>', "1.7"],
  health: ['<path d="M3 12h4l2-5 4 10 2-5h6"/>', "1.7"],
  settings: ['<circle cx="12" cy="12" r="3"/><path d="M19 14.5 21 16l-2 3-2.4-1a8 8 0 0 1-2.1 1.2L14 22h-4l-.5-2.8A8 8 0 0 1 7.4 18L5 19l-2-3 2-1.5a8 8 0 0 1 0-2.5L3 10l2-3 2.4 1a8 8 0 0 1 2.1-1.2L10 4h4l.5 2.8A8 8 0 0 1 16.6 8L19 7l2 3-2 2a8 8 0 0 1 0 2.5Z"/>', "1.7"],
  sun: ['<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M19 5l-1.5 1.5M6.5 17.5 5 19"/>', "1.7"],
  refresh: ['<path d="M20 7v5h-5M4 17v-5h5M18.5 10A7 7 0 0 0 6 7l-2 3M5.5 14A7 7 0 0 0 18 17l2-3"/>', "1.7"],
  shield: ['<path d="M12 2 5 6v6c0 5 3 8 7 10 4-2 7-5 7-10V6l-7-4Zm0 5v10M8 11h8"/>', "1.6"],
});

const SHELL_STROKE = Object.freeze({
  menu: ["round", "miter"], home: ["butt", "round"], story: ["round", "round"],
  worlds: ["butt", "miter"], user: ["round", "miter"], bell: ["butt", "round"],
  health: ["butt", "round"], settings: ["butt", "miter"], sun: ["round", "miter"],
  refresh: ["round", "round"], shield: ["butt", "round"],
});

function shellIcon(name) {
  const node = icon(name);
  const glyph = SHELL_GLYPHS[name];
  if (glyph) {
    node.innerHTML = glyph[0];
    node.setAttribute("stroke-width", glyph[1]);
    node.setAttribute("stroke-linecap", SHELL_STROKE[name][0]);
    node.setAttribute("stroke-linejoin", SHELL_STROKE[name][1]);
  }
  return node;
}

export function connectionStatusLabel(state) {
  return ({
    connected: copy("shell.app_shell.connectionStatusLabel.message.03aca9ea13"),
    connecting: copy("shell.app_shell.connectionStatusLabel.message.694186911a"),
    degraded: copy("shell.app_shell.connectionStatusLabel.message.4c234f9a0a"),
    "authentication-required": copy("shell.app_shell.connectionStatusLabel.message.3499131624"),
  })[state] || copy("shell.app_shell.connectionStatusLabel.message.41d0ea1212");
}

function element(tag, attributes = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.filter(Boolean));
  return node;
}

function badgeValue(value) {
  const count = Number(value);
  return Number.isInteger(count) && count > 0 ? Math.min(count, 99) : null;
}

function roleView(principal) {
  const labels = {
    admin: copy("shell.role_view.admin"), author: copy("shell.role_view.author"),
    host: copy("shell.role_view.host"), player: copy("shell.role_view.player"),
    readonly: copy("shell.role_view.readonly"),
  };
  const role = ["admin", "author", "host", "player", "readonly"]
    .find((candidate) => principalRoles(principal).has(candidate)) || "readonly";
  const select = element("select", {
    class: "tavern-control",
    "aria-disabled": "true",
    tabindex: "-1",
    "aria-label": copy("shell.role_view.label"),
  }, [element("option", { value: role, text: labels[role] })]);
  select.addEventListener("mousedown", (event) => event.preventDefault());
  return element("label", { class: "tavern-role-preview" }, [
    element("span", { text: copy("shell.role_view.label") }), select,
  ]);
}

export function renderShell(root, principal, navigation, handlers) {
  const opener = document.activeElement;
  const sidebar = element("aside", { class: "tavern-sidebar", "aria-label": copy("shell.app_shell.renderShell.message.9bae680405") });
  sidebar.append(element("div", { class: "tavern-brand" }, [
    // Let CSS load the packaged logo through AstrBot's asset rewrite pass.
    // A DOM-created relative <img> keeps the iframe URL and breaks in host mode.
    element("span", { class: "tavern-brand-mark", role: "img", "aria-label": "AI Tavern" }),
    element("span", { class: "tavern-brand-copy" }, [
      element("span", { class: "tavern-brand-eyebrow", text: copy("shell.brand.eyebrow") }),
      element("strong", { text: copy("shell.app_shell.renderShell.text.78e6b9eabf") }),
    ]),
  ]));
  const nav = element("nav", { class: "tavern-nav" });
  for (const group of visibleGroups(principal)) {
    const groupNode = element("section", { class: "tavern-nav-group" }, [element("h2", { class: "tavern-nav-title", text: group.label })]);
    for (const workspace of group.items) {
      const count = badgeValue(handlers.badges?.get?.(workspace) ?? handlers.badges?.[workspace]);
      const item = element("button", { class: "tavern-nav-item", type: "button", "data-workspace": workspace, ...(navigation.workspace === workspace ? { "aria-current": "page" } : {}) }, [
        element("span", { class: "tavern-nav-icon" }, [shellIcon(SHELL_ICONS[workspace] || workspace)]),
        element("span", { class: "tavern-nav-label", text: WORKSPACES[workspace].label }),
        count === null ? null : element("span", { class: "tavern-nav-badge", text: count === 99 ? "99+" : String(count) }),
      ]);
      item.addEventListener("click", () => handlers.navigate(workspace));
      groupNode.append(item);
    }
    nav.append(groupNode);
  }
  const statusState = handlers.connection || "connecting";
  const statusLabel = element("strong", { text: connectionStatusLabel(statusState) });
  const statusMeta = element("small", { text: copy("shell.status.pages", { p0: visibleWorkspaceCount(principal) }) });
  const status = element("div", {
    class: "tavern-sidebar-status",
    "data-state": statusState,
  }, [element("span", { class: "tavern-connection-pulse", "aria-hidden": "true" }), element("span", { class: "tavern-sidebar-status-copy" }, [statusLabel, statusMeta])]);
  sidebar.append(nav, status);
  const menu = element("button", { class: "tavern-button tavern-icon-button tavern-menu-button", type: "button", "aria-label": copy("shell.app_shell.renderShell.message.38e4f2ee1b") }, [shellIcon("menu"), element("span", { class: "tavern-visually-hidden", text: copy("shell.app_shell.renderShell.text.4ce4cafdd0") })]);
  const theme = element("button", { class: "tavern-button tavern-icon-button", type: "button", "aria-label": copy("shell.app_shell.renderShell.text.7a1604323e") }, [shellIcon("sun"), element("span", { class: "tavern-visually-hidden", text: copy("shell.app_shell.renderShell.text.7a1604323e") })]);
  const refreshLabel = element("span", { text: copy("shell.refresh.idle") });
  const refresh = element("button", { class: "tavern-button tavern-refresh-button", type: "button" }, [shellIcon("refresh"), refreshLabel]);
  const scrim = element("button", { class: "tavern-nav-scrim", type: "button", "aria-label": copy("shell.app_shell.renderShell.message.baf9f5c82a") });
  const closeDrawer = ({ restore = true } = {}) => { delete document.body.dataset.navOpen; menu.setAttribute("aria-expanded", "false"); if (restore) menu.focus(); };
  menu.setAttribute("aria-expanded", "false"); menu.setAttribute("aria-controls", "tavern-primary-nav"); nav.id = "tavern-primary-nav";
  menu.addEventListener("click", () => { document.body.dataset.navOpen = "true"; menu.setAttribute("aria-expanded", "true"); nav.querySelector(".tavern-nav-item")?.focus(); });
  scrim.addEventListener("click", closeDrawer);
  nav.addEventListener("click", (event) => { if (event.target.closest(".tavern-nav-item")) closeDrawer({ restore: false }); });
  nav.addEventListener("keydown", (event) => {
    const items = [...nav.querySelectorAll(".tavern-nav-item")]; const index = items.indexOf(document.activeElement); if (event.key === "Escape") { event.preventDefault(); closeDrawer(); return; } if (index < 0) return;
    let next = null; if (["ArrowDown","ArrowRight"].includes(event.key)) next = (index + 1) % items.length; if (["ArrowUp","ArrowLeft"].includes(event.key)) next = (index - 1 + items.length) % items.length; if (event.key === "Home") next = 0; if (event.key === "End") next = items.length - 1; if (next !== null) { event.preventDefault(); items[next].focus(); }
  });
  theme.addEventListener("click", handlers.theme);
  refresh.addEventListener("click", handlers.refresh);
  const title = element("h1", { text: WORKSPACES[navigation.workspace].pageTitle || WORKSPACES[navigation.workspace].label });
  const date = new Date();
  const topbar = element("header", { class: "tavern-topbar" }, [
    element("div", { class: "tavern-topbar-title" }, [element("time", { datetime: date.toISOString(), text: formatUtc8Minute(date) }), title]),
    element("div", { class: "tavern-topbar-actions" }, [menu, theme, refresh, roleView(principal)]),
  ]);
  const main = element("main", { id: "tavern-main", class: "tavern-main", tabindex: "-1" });
  const content = element("div", { class: "tavern-content" }, [topbar, main]);
  const workspaceNode = element("div", { class: "tavern-workspace" }, [content]);
  const app = element("div", { class: "tavern-app" }, [sidebar, scrim, workspaceNode]);
  root.replaceChildren(app);
  if (opener?.dataset?.workspace) sidebar.querySelector(`[data-workspace="${opener.dataset.workspace}"]`)?.focus();
  const setConnection = (state) => {
    status.dataset.state = state;
    statusLabel.textContent = connectionStatusLabel(state);
  };
  const setRefreshState = (state) => {
    const key = ["refreshing", "complete", "failed"].includes(state) ? state : "idle";
    refresh.dataset.state = key;
    refreshLabel.textContent = key === "refreshing"
      ? copy("shell.refresh.refreshing")
      : key === "complete"
        ? copy("shell.refresh.complete")
        : key === "failed"
          ? copy("shell.refresh.failed")
          : copy("shell.refresh.idle");
    refresh.setAttribute("aria-label", refreshLabel.textContent);
  };
  const setBadge = (workspace, value) => {
    const item = nav.querySelector(`[data-workspace="${workspace}"]`);
    if (!item) return;
    item.querySelector(".tavern-nav-badge")?.remove();
    const count = badgeValue(value);
    if (count !== null) item.append(element("span", { class: "tavern-nav-badge", text: count === 99 ? "99+" : String(count) }));
  };
  const setNavigation = (next) => {
    for (const item of nav.querySelectorAll(".tavern-nav-item")) {
      if (item.dataset.workspace === next.workspace) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }
    title.textContent = WORKSPACES[next.workspace].pageTitle || WORKSPACES[next.workspace].label;
  };
  return { app, sidebar, title, main, refresh, menu, closeDrawer, status, setConnection, setRefreshState, setBadge, setNavigation };
}
