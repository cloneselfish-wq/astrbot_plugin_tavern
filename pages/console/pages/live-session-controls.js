import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderStatusBadge } from "../components/status.js";
import { openSessionConfigurationAction } from "../dialogs/session-detail.js";
import { el, label, rows } from "./shared.js";

export function actorPanel(turn) {
  const order = rows(turn?.order);
  const current = turn?.current_name || turn?.actor_label || turn?.label
    || copy("pages.live_session.actorPanel.message.c787b97972");
  const remaining = Number(turn?.remaining_seconds);
  return el("section", {
    class: "tavern-surface tavern-live-actor",
    "data-testid": "tavern-live-actor",
  }, [
    el("header", { class: "tavern-section-heading" }, [
      el("div", {}, [
        el("p", { class: "tavern-page-eyebrow", text: copy("pages.live_session.actorPanel.text.3499bd1d2d") }),
        el("h2", { text: `「${String(current).replace(/^「|」$/g, "")}」` }),
      ]),
      Number.isFinite(remaining) && remaining >= 0
        ? renderStatusBadge({ state: "running", label: copy("pages.live_session.actorPanel.message.3a4c61693e", { p0: remaining }) })
        : null,
    ]),
    order.length ? el("ol", { class: "tavern-turn-order" }, order.map((item) =>
      el("li", { "data-current": String(Boolean(item.current)) }, [
        el("strong", { text: item.label }),
        item.state ? el("span", { text: item.state }) : null,
      ]))) : null,
  ]);
}

export function descriptorFor(model, intent) {
  return rows(model.actions).find((action) =>
    (action.intent || action.action) === intent
    && action.transportReady === true
    && Number.isInteger(action.expected_revision)) || null;
}

function executeMode(descriptor, mode, handlers, opener) {
  if (!descriptor || !handlers.actions) return null;
  const objectKey = handlers.navigation?.objectKey || "";
  return handlers.actions.execute({
    ...descriptor,
    action: descriptor.action || descriptor.intent,
    object_key: objectKey,
    target_key: objectKey,
  }, { opener, input: { mode } });
}

export function narrativeModePanel(narrativeMode, model, handlers) {
  const modeDescriptor = descriptorFor(model, "session.narrative_mode.save");
  const options = rows(narrativeMode?.options);
  const selectedIndex = options.findIndex((option) => option.mode === narrativeMode.mode);
  const modeOptions = el("div", {
    class: "tavern-narrative-modes",
    role: modeDescriptor ? "radiogroup" : "list",
    "aria-label": copy("pages.live_session.controlPanel.text.16e8f15cb9"),
  });
  for (const [index, option] of options.entries()) {
    const selected = option.mode === narrativeMode.mode;
    const content = [
      el("strong", { text: option.label }),
      Number.isFinite(Number(option.minimum)) && Number.isFinite(Number(option.maximum))
        ? el("span", { text: copy("pages.live_session.narrative_mode.range", { p0: option.minimum, p1: option.maximum }) })
        : null,
      option.description ? el("small", { text: option.description }) : null,
    ];
    if (modeDescriptor) {
      const button = el("button", {
        class: "tavern-narrative-mode",
        type: "button",
        role: "radio",
        "aria-checked": String(selected),
        "data-selected": String(selected),
        "data-narrative-mode": option.mode,
        "data-live-focus": `narrative-mode:${option.mode}`,
        tabindex: selected || (selectedIndex < 0 && index === 0) ? "0" : "-1",
      }, content);
      button.addEventListener("click", (event) => executeMode(modeDescriptor, option.mode, handlers, event.currentTarget));
      modeOptions.append(button);
    } else {
      modeOptions.append(el("article", {
        class: "tavern-narrative-mode",
        "data-selected": String(selected),
      }, content));
    }
  }
  modeOptions.addEventListener("keydown", (event) => {
    if (!modeDescriptor) return;
    const buttons = [...modeOptions.querySelectorAll('[role="radio"]')];
    const index = buttons.indexOf(document.activeElement);
    if (index < 0) return;
    let next = null;
    if (["ArrowRight", "ArrowDown"].includes(event.key)) next = (index + 1) % buttons.length;
    if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = (index - 1 + buttons.length) % buttons.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = buttons.length - 1;
    if (next === null) return;
    event.preventDefault();
    for (const [buttonIndex, button] of buttons.entries()) button.tabIndex = buttonIndex === next ? 0 : -1;
    buttons[next].focus();
    buttons[next].click();
  });
  if (!options.length) {
    modeOptions.append(el("p", { text: narrativeMode?.label || copy("pages.live_session.controlPanel.message.4872ff8d9b") }));
  }
  return el("section", {
    class: "tavern-surface tavern-live-narrative-mode",
    "data-testid": "tavern-live-narrative-mode",
  }, [
    el("header", { class: "tavern-section-heading" }, [
      el("div", {}, [
        el("h2", { text: copy("pages.live_session.controlPanel.text.16e8f15cb9") }),
        el("p", { text: copy("pages.live_session.narrative_mode.summary") }),
      ]),
      narrativeMode?.label ? renderStatusBadge({ state: "running", label: narrativeMode.label }) : null,
    ]),
    modeOptions,
  ]);
}

export function controlPanel(session, reminder, model, handlers) {
  const reminderDescriptor = descriptorFor(model, "session.generation_reminder.save");
  const reminderFacts = el("dl", { class: "tavern-live-definition" }, [
    el("div", {}, [el("dt", { text: copy("pages.live_session.rc8.bc26ecbf81") }), el("dd", { text: reminder?.enabled ? copy("pages.live_session.rc8.dfb802238b") : copy("pages.live_session.rc8.6744b4c6a9") })]),
    el("div", {}, [el("dt", { text: copy("pages.live_session.rc8.32c493ff09") }), el("dd", { text: copy("pages.live_session.reminder.interval", { p0: Number(reminder?.interval_seconds) || 60 }) })]),
    el("div", {}, [el("dt", { text: copy("pages.live_session.rc8.a177f3150a") }), el("dd", { text: reminder?.source_label || copy("pages.live_session.rc8.bbfd71b211") })]),
    el("div", {}, [el("dt", { text: copy("pages.live_session.rc8.d2b84e4a68") }), el("dd", { text: copy("pages.live_session.rc8.ea427f374b") })]),
  ]);
  const edit = reminderDescriptor ? renderButton({
    variant: "secondary",
    label: reminderDescriptor.label,
    intent: { id: reminderDescriptor.intent },
    onActivate: (_intent, event) => openSessionConfigurationAction(reminderDescriptor, handlers, event.currentTarget),
  }) : null;
  if (edit) edit.dataset.liveFocus = "generation-reminder:edit";
  return el("section", {
    class: "tavern-surface tavern-live-control",
    "data-testid": "tavern-live-control",
  }, [
    el("header", { class: "tavern-section-heading" }, [
      el("div", {}, [
        el("h2", { text: copy("pages.live_session.controlPanel.text.fee03c832e") }),
        el("p", { text: copy("pages.live_session.control.summary") }),
      ]),
    ]),
    el("p", { class: "tavern-live-control-state", text: session?.input_locked
      ? copy("pages.live_session.controlPanel.message.0c65416f4a")
      : copy("pages.live_session.controlPanel.message.35f47f9f98") }),
    reminderFacts,
    edit,
  ]);
}

export function pressurePanel(pressure, uiProfile) {
  const items = rows(pressure?.items);
  const declared = new Set(rows(uiProfile?.live_lenses).map((lens) => lens?.key || lens?.id));
  const supported = ["quests", "clocks", "challenge"].some((lens) => declared.has(lens));
  if (!items.length && !supported && Number(pressure?.active_timers || 0) <= 0) return null;
  const section = el("section", {
    class: "tavern-surface tavern-live-pressure",
    "data-testid": "tavern-live-pressure",
  }, [el("h2", { text: copy("pages.live_session.pressurePanel.text.9ef22ba928") })]);
  if (!items.length) {
    section.append(el("p", {
      text: Number(pressure?.active_timers || 0) > 0
        ? copy("pages.live_session.pressurePanel.message.a82e3073d1", { p0: pressure.active_timers })
        : copy("pages.live_session.pressurePanel.message.bdb336c0f3"),
    }));
    return section;
  }
  for (const item of items) {
    section.append(el("article", { class: "tavern-pressure-row" }, [
      el("div", {}, [
        el("strong", { text: label(item, copy("pages.live_session.pressurePanel.message.d28c889060")) }),
        el("small", { text: item.remaining_label || item.state || "" }),
      ]),
      renderStatusBadge({
        state: item.state === "进行中" ? "running" : "warning",
        label: item.state || copy("pages.live_session.pressurePanel.message.cdea037991"),
      }),
    ]));
  }
  return section;
}

export function mobileSecondaryDisclosure(node, summaryText, className) {
  if (!node) return null;
  const disclosure = el("details", { class: `tavern-live-secondary-disclosure ${className}` }, [
    el("summary", { text: summaryText }),
    el("div", { class: "tavern-live-secondary-content" }, [node]),
  ]);
  const media = globalThis.matchMedia?.("(max-width: 760px)");
  const syncBreakpoint = (matches) => matches
    ? disclosure.removeAttribute("open")
    : disclosure.setAttribute("open", "");
  const onBreakpoint = (event) => syncBreakpoint(event.matches);
  syncBreakpoint(Boolean(media?.matches));
  media?.addEventListener?.("change", onBreakpoint);
  disclosure.dispose = () => media?.removeEventListener?.("change", onBreakpoint);
  return disclosure;
}
