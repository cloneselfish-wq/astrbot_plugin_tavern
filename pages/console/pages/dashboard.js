import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderBusinessCard, renderDensityStrip, renderMetricStrip } from "../components/cards.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { renderStatusBadge } from "../components/status.js";
import { renderCapabilityEntry } from "../components/capability-hub.js";
import { formatUtc8Minute } from "../components/time.js";
import { openSessionConfigurationAction } from "../dialogs/session-detail.js";
import {
  el,
  label,
  pageRoot,
  rows,
  section,
  summary,
  value,
} from "./shared.js";

function canNavigate(handlers, workspace) {
  return typeof handlers.canNavigate === "function"
    ? handlers.canNavigate(workspace)
    : true;
}

function publicState(value) {
  const state = String(value || "").toLowerCase();
  if (["ready", "normal", copy("pages.dashboard.publicState.message.296de0e31f"), copy("pages.dashboard.publicState.message.4d99c976be")].includes(state)) return "ready";
  if (["running", copy("pages.dashboard.publicState.message.dc9591e56d"), copy("pages.dashboard.publicState.message.c346b67627")].includes(state)) return "running";
  if (["waiting", copy("pages.dashboard.publicState.message.999a459c3f"), copy("pages.dashboard.publicState.message.7bf25421c6"), copy("pages.dashboard.publicState.message.814b6a6c04")].includes(state)) return "waiting";
  if (["recovering", copy("pages.dashboard.publicState.message.7266e439cc"), copy("pages.dashboard.publicState.message.a3f9afd0aa")].includes(state)) return "recovering";
  if (["warning", copy("pages.dashboard.publicState.message.b7e3e715f1"), copy("pages.dashboard.publicState.message.eb0c326b60"), copy("pages.dashboard.publicState.message.8d12fc0d4e")].includes(state)) return "warning";
  if (["error", copy("pages.dashboard.publicState.message.28384d7afd"), copy("pages.dashboard.publicState.message.460b3574e4")].includes(state)) return "error";
  if (["readonly", copy("pages.dashboard.publicState.message.3b5ec3533b")].includes(state)) return "readonly";
  if (["stale", copy("pages.dashboard.publicState.message.9d2051861b")].includes(state)) return "stale";
  if (["conflict", copy("pages.dashboard.publicState.message.45fb956103")].includes(state)) return "conflict";
  return "unknown";
}

function mobileDisclosure({ className = "", summaryText, children = [] }) {
  const disclosure = el("details", {
    class: `tavern-dashboard-disclosure ${className}`.trim(),
  }, [
    el("summary", { text: summaryText }),
    el("div", { class: "tavern-dashboard-disclosure-content" }, children),
  ]);
  const media = globalThis.matchMedia?.("(max-width: 760px)");
  const syncBreakpoint = (matches) => {
    if (matches) disclosure.removeAttribute("open");
    else disclosure.setAttribute("open", "");
  };
  syncBreakpoint(Boolean(media?.matches));
  media?.addEventListener?.("change", (event) => syncBreakpoint(event.matches));
  return disclosure;
}

function layoutSelfCheck() {
  const output = el("output", {
    text: copy("pages.dashboard.layout_check.checking"),
  });
  const holder = el("div", {
    class: "tavern-layout-check",
    "aria-label": copy("pages.dashboard.layout_check.label"),
  }, [el("span", { text: copy("pages.dashboard.layout_check.label") }), output]);
  const check = () => {
    if (!holder.isConnected) return;
    const documentElement = document.documentElement;
    const passed = documentElement.scrollWidth <= documentElement.clientWidth + 1;
    output.textContent = passed
      ? copy("pages.dashboard.layout_check.passed")
      : copy("pages.dashboard.layout_check.failed");
    output.dataset.state = passed ? "ready" : "error";
  };
  globalThis.requestAnimationFrame?.(() => globalThis.requestAnimationFrame?.(check));
  return holder;
}

function sessionProgress(item) {
  const total = Number(item.progress_total);
  const current = Number(item.progress_current);
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(current)) return null;
  const safeCurrent = Math.max(0, Math.min(total, current));
  return el("div", { class: "tavern-stack" }, [
    el("div", { class: "tavern-progress-copy" }, [
      el("span", { text: item.progress_label || copy("pages.dashboard.sessionProgress.message.70836c76a1") }),
      el("span", { text: `${safeCurrent} / ${total}` }),
    ]),
    el("progress", {
      class: "tavern-progress",
      max: total,
      value: safeCurrent,
      "aria-label": item.progress_label || copy("pages.dashboard.sessionProgress.message.70836c76a1"),
    }, [document.createTextNode(`${safeCurrent} / ${total}`)]),


  ]);
}

function dashboardSessionCard(item, handlers, index = 0) {
  const objectKey = String(item.key || "");
  const playerCurrent = Number(item.player_current);
  const playerTotal = Number(item.player_total);
  const playerSummary = Number.isFinite(playerCurrent) && Number.isFinite(playerTotal)
    ? `${playerCurrent} / ${playerTotal}`
    : item.player_summary || null;
  const facts = [
    [copy("pages.dashboard.dashboardSessionCard.message.991e63fe29"), playerSummary],
    [copy("pages.dashboard.dashboardSessionCard.message.9fb1d89660"), item.round === undefined || item.round === null ? null : item.round],
    [copy("pages.dashboard.dashboardSessionCard.message.999a459c3f"), item.todo_count === undefined || item.todo_count === null ? null : copy("pages.dashboard.dashboardSessionCard.message.980fda45d8", {p0: item.todo_count})],
  ].filter(([, fact]) => fact !== undefined && fact !== null && fact !== "");
  const stateLabel = item.status || item.state || copy("pages.dashboard.dashboardSessionCard.message.cdea037991");
  const descriptors = rows(item.available_actions)
    .filter((action) => action?.transportReady === true && action.intent && Number.isInteger(action.expected_revision));
  const preferredIntents = /暂停/.test(stateLabel)
    ? ["session.lifecycle.reopen"]
    : ["session.lifecycle.close", "session.lifecycle.abort"];
  const preferredDescriptors = preferredIntents
    .map((intent) => descriptors.find((descriptor) => descriptor.intent === intent))
    .filter(Boolean);
  const actions = [
    renderButton({
      label: /暂停/.test(stateLabel)
        ? copy("pages.dashboard.dashboardSessionCard.label.69e5ea67f7")
        : copy("pages.dashboard.dashboardSessionCard.label.a19c5f3aa1"),
      onActivate: () => handlers.navigate?.("session_detail", { objectKey, lens: "party" }),
    }),
    ...preferredDescriptors.map((descriptor) => renderButton({
      variant: /abort|finish/.test(descriptor.intent) ? "danger" : "secondary",
      label: descriptor.label,
      intent: { id: descriptor.intent },
      onActivate: (_intent, event) => openSessionConfigurationAction(
        { ...descriptor, object_key: objectKey },
        { ...handlers, navigation: { ...(handlers.navigation || {}), objectKey } },
        event.currentTarget,
      ),
    })),
  ];
  return renderBusinessCard({
    kind: "session",
    className: "tavern-session-card",
    opaqueKey: `session-${index + 1}`,
    header: el("header", { class: "tavern-card-header tavern-session-head" }, [
      el("div", {}, [
        el("h3", { text: label(item, copy("pages.dashboard.dashboardSessionCard.message.d9ba09203e")) }),
        item.world_label ? el("p", { text: item.world_label }) : null,
      ]),
      renderStatusBadge({ state: publicState(stateLabel), label: stateLabel }),
    ]),
    body: el("div", { class: "tavern-session-body" }, [
      el("div", { class: "tavern-session-scene" }, [
        el("span", { text: copy("pages.dashboard.dashboardSessionCard.text.60912cb15e") }),
        el("strong", { text: item.scene_label || copy("pages.dashboard.dashboardSessionCard.message.d1c923b0e8") }),
      ]),
      facts.length ? el("dl", { class: "tavern-session-facts" }, facts.map(([term, fact]) =>
        el("div", {}, [el("dt", { text: term }), el("dd", { text: fact })]))) : null,
      sessionProgress(item),
    ]),
    actions,
  });
}

function actionableList(items, handlers) {
  const section = el("section", {
    class: "tavern-surface tavern-dashboard-urgent",
    "data-testid": "tavern-dashboard-urgent",
  }, [
    el("h2", { text: copy("pages.dashboard.actionableList.text.b7e3e715f1") }),
    el("p", {
      class: "tavern-list-meta",
      text: copy("pages.dashboard.actionableList.text.6568dd8198"),
    }),
  ]);
  if (!items.length) {
    section.append(renderStatePanel({
      phase: "empty",
      emptyCopy: copy("pages.dashboard.actionableList.emptyCopy.6bb84ae2f0"),
    }));
    return section;
  }
  for (const item of items.slice(0, 4)) {
    section.append(el("article", { class: "tavern-action-row" }, [
      el("div", {}, [
        el("strong", { text: label(item, copy("pages.dashboard.actionableList.message.5e415ae2bb")) }),
        el("p", { class: "tavern-list-meta", text: summary(item, copy("pages.dashboard.actionableList.message.445e7359cf")) }),
        item.object_label ? el("small", { text: item.object_label }) : null,
      ]),
      renderButton({
        variant: "inline",
        label: item.action_label || copy("pages.dashboard.actionableList.message.9a240c3b6f"),
        onActivate: () => handlers.navigate?.("todo", {
          objectKey: String(item.key || ""),
        }),
      }),
    ]));
  }
  return section;
}

function currentStoryCard(story, handlers) {
  if (!story) {
    return renderBusinessCard({
      kind: "current-story",
      className: "tavern-current-story",
      opaqueKey: "story-empty",
      kicker: copy("pages.dashboard.currentStoryCard.kicker"),
      title: copy("pages.dashboard.currentStoryCard.empty_title"),
      summary: copy("pages.dashboard.currentStoryCard.empty_summary"),
    });
  }
  const chips = [
    story.round_label,
    story.scene_label,
    story.actor_label,
  ].filter(Boolean).map((item) => ({ label: "", value: item }));
  return renderBusinessCard({
    kind: "current-story",
    className: "tavern-current-story",
    opaqueKey: "current-story",
    kicker: copy("pages.dashboard.currentStoryCard.kicker"),
    title: label(story, copy("pages.dashboard.currentStoryCard.message.4771a6ddfd")),
    summary: summary(story, copy("pages.dashboard.currentStoryCard.message.69c14652ef")),
    meta: chips,
    actions: [renderButton({
      variant: "primary",
      label: copy("pages.dashboard.currentStoryCard.label.6c8fc6dd98"),
      onActivate: () => handlers.navigate?.("session_detail", {
        objectKey: String(story.session_key || story.key || ""),
        lens: "party",
      }),
    })],
  });
}

function serviceSummary(services, handlers) {
  if (!services.length) return null;
  const healthAction = canNavigate(handlers, "health")
    ? renderButton({
        variant: "quiet",
        label: copy("pages.dashboard.serviceSummary.label.79110b506e"),
        onActivate: () => handlers.navigate?.("health"),
      })
    : null;
  const section = el("section", {
    class: "tavern-surface tavern-dashboard-services",
    "data-testid": "tavern-dashboard-services",
  }, [
    el("header", { class: "tavern-section-heading" }, [
      el("div", {}, [
        el("h2", { text: copy("pages.dashboard.serviceSummary.text.e8a4f7c09d") }),
        el("p", { text: copy("pages.dashboard.serviceSummary.text.07af747bc2") }),
      ]),
      healthAction,
    ]),
  ]);
  for (const item of services) {
    const state = publicState(item.status || item.state);
    const abnormal = !["ready", "running"].includes(state);
    section.append(el("article", {
      class: "tavern-service-row",
      "data-expanded": String(abnormal),
    }, [
      el("div", {}, [
        el("strong", { text: label(item, copy("pages.dashboard.serviceSummary.message.e8a4f7c09d")) }),
        el("p", { class: "tavern-list-meta", text: summary(item, copy("pages.dashboard.serviceSummary.message.df6fcea32b")) }),
        abnormal && item.automatic ? el("small", { text: item.automatic }) : null,
        abnormal && item.next_step ? el("small", { text: item.next_step }) : null,
      ]),
      renderStatusBadge({
        state,
        label: item.status || item.state || copy("pages.dashboard.serviceSummary.message.cdea037991"),
      }),
    ]));
  }
  return section;
}

function recentTimeline(items) {
  const section = el("section", {
    class: "tavern-surface tavern-dashboard-recent",
    "data-testid": "tavern-dashboard-recent",
  }, [
    el("h2", { text: copy("pages.dashboard.recentTimeline.text.cb29bae4c8") }),
    el("p", { class: "tavern-list-meta", text: copy("pages.dashboard.recentTimeline.text.d38770c9e1") }),
  ]);
  if (!items.length) {
    section.append(renderStatePanel({
      phase: "empty",
      emptyCopy: copy("pages.dashboard.recentTimeline.emptyCopy.90d4a483e1"),
    }));
    return section;
  }
  const list = el("ol", { class: "tavern-event-timeline" });
  for (const item of items.slice(0, 8)) {
    const timestamp = item.created_at || item.time || item.time_label || "";
    list.append(el("li", {}, [
      el("span", { class: "tavern-event-marker", "aria-hidden": "true" }),
      el("div", {}, [
        el("strong", { text: label(item, copy("pages.dashboard.recentTimeline.message.d99c65cf2e")) }),
        el("p", { text: summary(item) }),
      ]),
      timestamp ? el("time", { datetime: timestamp, text: formatUtc8Minute(timestamp) }) : null,
    ]));
  }
  section.append(list);
  return section;
}

function recoverySummary(services, handlers) {
  if (!services.length) return null;
  const recovering = services.filter((item) =>
    ["recovering", "warning", "error", "stale"].includes(publicState(item.status || item.state)));
  const section = el("aside", {
    class: "tavern-surface tavern-dashboard-recovery",
    "data-testid": "tavern-dashboard-recovery",
  }, [
    el("h2", { text: copy("pages.dashboard.recoverySummary.text.0292894cda") }),
  ]);
  if (!recovering.length) {
    section.append(el("p", { text: copy("pages.dashboard.recoverySummary.text.9413b306dc") }));
  } else {
    for (const item of recovering.slice(0, 3)) {
      section.append(el("div", { class: "tavern-list-row" }, [
        el("strong", { text: label(item, copy("pages.dashboard.recoverySummary.message.99033956c5")) }),
        renderStatusBadge({
          state: publicState(item.status || item.state),
          label: item.status || item.state || copy("pages.dashboard.recoverySummary.message.3b7e27cb43"),
        }),
      ]));
    }
  }
  if (canNavigate(handlers, "audit")) {
    section.append(renderButton({
      variant: "secondary",
      label: copy("pages.dashboard.recoverySummary.label.5aa38d876a"),
      onActivate: () => handlers.navigate?.("audit"),
    }));
  }
  return section;
}

export function renderDashboard(model, handlers = {}) {
  const root = pageRoot(model, "tavern-dashboard");
  const pageProblem = model.problems?.find((problem) => !problem?.section) || null;
  if (!["ready", "empty"].includes(model.phase)
      && !(model.phase === "partial" && !pageProblem)) {
    root.append(renderStatePanel({
      phase: model.phase,
      operation: copy("pages.dashboard.renderDashboard.message.ec1575c645"),
      problem: pageProblem || model.problems?.[0],
    }));
  }
  const block = (id, node, state = "ready") => {
    node.setAttribute("id", `tavern-dashboard-${id}`);
    node.setAttribute("data-block", id); node.setAttribute("data-state", state);
    node.setAttribute("data-testid", `tavern-block-dashboard-${id}`);
    return node;
  };
  const todayHeading = block("today-heading", el("header", { class: "tavern-dashboard-heading" }, [
    el("div", {}, [
      el("h2", { text: copy("pages.dashboard.renderDashboard.text.a7e291c1d2") }),
      el("p", { text: model.summary || copy("pages.dashboard.renderDashboard.message.3b7045d4e7") }),
    ]),
    model.permissions?.can_manage ? layoutSelfCheck() : null,
  ]));
  const metrics = rows(value(model, "metrics"));
  const density = rows(value(model, "density"));
  const metricBlock = block("primary-metrics", el("section", { class: "tavern-dashboard-metrics" }), metrics.length ? "ready" : "empty");
  if (metrics.length) metricBlock.append(renderMetricStrip({ workspace: "dashboard", metrics, max: 4 }));
  const densityBlock = block("density-strip", el("section", { class: "tavern-dashboard-density" }), density.length ? "ready" : "empty");
  if (density.length) densityBlock.append(renderDensityStrip({
    workspace: "dashboard",
    stats: density,
    onNavigate: (workspace) => handlers.navigate?.(workspace),
  }));
  const statusSection = el("section", { class: "tavern-dashboard-section" }, [todayHeading, metricBlock, densityBlock]);
  const sessionsModel = section(model, "sessions");
  const sessionsData = rows(sessionsModel?.value);
  const urgent = rows(value(model, "urgent"));
  const story = value(model, "current_story");
  const services = rows(value(model, "services"));
  const sessionCards = sessionsData.slice(0, 4).map((item, index) => dashboardSessionCard(item, handlers, index));
  const sessionsAction = sessionsData.length && canNavigate(handlers, "sessions")
    ? renderButton({ variant: "quiet",
        label: copy("pages.dashboard.renderDashboard.label.f7a9973cc3"),
        onActivate: () => handlers.navigate?.("sessions"),
      })
    : null;
  const sessions = block("session-grid", el("section", {
    class: "tavern-surface tavern-dashboard-sessions",
  }, [
    el("header", { class: "tavern-section-heading" }, [
      el("div", {}, [
        el("h2", { text: copy("pages.dashboard.renderDashboard.text.3ecd42d2d9") }),
        el("p", { text: copy("pages.dashboard.renderDashboard.text.23c8b4e46a") }),
      ]),
      sessionsAction,
    ]),
  ]), sessionsModel?.missing ? "partial" : sessionCards.length ? "ready" : "empty");
  if (sessionCards.length) {
    sessions.append(el("div", { class: "tavern-session-grid" }, sessionCards));
  } else if (sessionsModel?.missing) {
    const problem = model.problems?.find((candidate) => candidate?.section === "sessions") || {
      message: sessionsModel.failure?.message,
      recovery: sessionsModel.failure?.recovery,
    };
    sessions.append(renderStatePanel({
      phase: "partial",
      operation: sessionsModel.failure?.label || copy("pages.dashboard.renderDashboard.message.ec1575c645"),
      problem,
    }));
  } else {
    sessions.append(renderStatePanel({
      phase: "empty",
      emptyCopy: copy("pages.dashboard.renderDashboard.emptyCopy.2d185da7a6"),
      problem: {
        recovery: copy("pages.dashboard.renderDashboard.recovery.9433adc56a"),
      },
    }));
  }
  const storyBlock = block("current-story", currentStoryCard(story, handlers), story ? "ready" : "empty");
  const serviceSection = serviceSummary(services, handlers);
  const serviceBlock = block("service-summary", serviceSection || el("section", {
    class: "tavern-dashboard-service-block",
  }), services.length ? "ready" : "empty");
  const urgentSection = canNavigate(handlers, "todo") ? actionableList(urgent, handlers) : el("section", { class: "tavern-dashboard-actionable-block" });
  const urgentBlock = block("actionable-todo", urgentSection, urgent.length ? "ready" : "empty");
  const recent = rows(value(model, "recent_changes"));
  const recentSection = recent.length ? recentTimeline(recent) : null;
  const recovery = recoverySummary(services, handlers);
  const retention = el("aside", { class: "tavern-dashboard-retention-note" }, [
    el("strong", { text: copy("pages.dashboard.retention.label") }),
    document.createTextNode(copy("pages.dashboard.retention.summary")),
  ]);
  const lower = el("div", { class: "tavern-dashboard-lower" }, [recentSection, recovery]);
  const recentBlock = block("recent-timeline", mobileDisclosure({
    className: "tavern-dashboard-history-disclosure",
    summaryText: copy("pages.dashboard.recentTimeline.text.cb29bae4c8"),
    children: [lower],
  }), recent.length || recovery ? "ready" : "empty");
  const capabilityPanels = rows(value(model, "capability_panels"));
  const capabilitySection = renderCapabilityEntry({
    panels:capabilityPanels,
    title:copy("pages.dashboard.rc8.aceac63a9b"),
    summary:copy("pages.dashboard.rc8.6dc8987498"),
    handlers,
  });
  const integrityDisclosure = capabilitySection ? el("details", {
    class: "tavern-dashboard-integrity",
  }, [
    el("summary", { text: copy("pages.dashboard.rc8.aceac63a9b") }),
    el("div", { class: "tavern-dashboard-integrity-body" }, [capabilitySection]),
  ]) : null;
  const capabilityBlock = block("capability-launcher", integrityDisclosure || el("section", {
    class: "tavern-dashboard-capability-block",
  }), capabilityPanels.length ? "ready" : "empty");
  const primary = el("div", { class: "tavern-dashboard-primary" }, [sessions, urgentBlock]);
  const side = el("aside", { class: "tavern-dashboard-side" }, [storyBlock, serviceBlock, capabilityBlock]);
  const hasSide = Boolean(story || serviceSection || capabilitySection);
  const main = el("div", {
    class: "tavern-dashboard-grid",
    "data-has-side": String(hasSide),
  }, [primary, side]);
  const postscript = el("div", { class: "tavern-dashboard-postscript" }, [recentBlock]);
  const sourceNote = el("div", { class: "tavern-dashboard-source-note" }, [retention]);
  const layout = el("div", { class: "tavern-dashboard-layout" }, [
    statusSection,
    main,
    postscript,
    sourceNote,
  ]);
  root.append(layout);
  if (model.phase === "permission") {
    const denied = renderStatePanel({ phase:"permission", operation:copy("pages.dashboard.renderDashboard.message.ec1575c645"), problem:model.problems?.[0] });
    root.replaceChildren(denied, ...["today-heading","primary-metrics","density-strip","capability-launcher","session-grid","current-story","service-summary","actionable-todo","recent-timeline"].map((id)=>block(id,el("section",{class:"tavern-dashboard-redacted"}),"permission")));
  }
  return root;
}
