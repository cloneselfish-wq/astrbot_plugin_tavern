import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { el, label, pageRoot, rows, stateNotice, summary, value } from "./shared.js";

function evidenceAction(item, runtime) {
  const descriptor = rows(item.available_actions).find((action) => (
    action?.transportReady === true
    && action.intent
    && Number.isInteger(action.expected_revision)
    && !rows(action.fields).length
  ));
  return descriptor ? [renderButton({
    variant: "secondary",
    label: descriptor.label,
    intent: { id: descriptor.intent },
    onActivate: (_intent, event) => runtime.actions?.execute(
      { ...descriptor, target_key: item.key },
      { opener: event.currentTarget },
    ),
  })] : [];
}

function observationCard(item, index) {
  return el("article", {
    class: "tavern-observation-card",
    "data-observation": `observation-${index + 1}`,
  }, [
    el("strong", { text: label(item) }),
    el("span", {
      class: "tavern-observation-state",
      "data-state": item.state === "证据不足" ? "waiting" : "ready",
      text: item.state || copy("pages.tendencies.renderTendencies.message.f69dcc7758"),
    }),
    el("p", { text: summary(item, copy("pages.tendencies.renderTendencies.message.24b4076d51")) }),
  ]);
}

function evidenceRows(items, runtime, emptyCopy) {
  if (!items.length) {
    return el("p", { class: "tavern-tendencies-empty", text: emptyCopy });
  }
  return el("div", { class: "tavern-tendencies-info-list" }, items.map((item) => {
    const actions = evidenceAction(item, runtime);
    return el("div", { class: "info-row tavern-tendencies-info-row" }, [
      el("div", { class: "tavern-tendencies-info-copy" }, [
        el("strong", { text: label(item, copy("pages.tendencies.renderTendencies.label.2e9f011743")) }),
        el("span", { text: summary(item, copy("pages.tendencies.renderTendencies.label.4262c45dc7")) }),
      ]),
      actions.length ? el("div", { class: "tavern-tendencies-info-actions" }, actions) : null,
    ]);
  }));
}

export function renderTendencies(model, runtime = {}) {
  const root = pageRoot(model, "tavern-tendencies");
  root.append(stateNotice(model, copy("pages.tendencies.renderTendencies.message.0d5c543935")));
  const selector = rows(model.filters).find((field) => field.name === "session_key");
  const selected = runtime.navigation?.filters || {};
  root.append(tendencyBlock("toolbar", el("section", { class: "page-toolbar tavern-tendencies-toolbar" }, [
    el("div", { class: "tavern-page-toolbar-copy" }, [
      el("h2", { text: model.title || copy("pages.tendencies.renderTendencies.text.f69dcc7758") }),
      el("p", { text: model.summary || copy("pages.tendencies.renderTendencies.message.4e8b47bcb2") }),
    ]),
    el("div", { class: "filter-row tavern-tendencies-toolbar-actions" }, [
      selector ? el("select", {
        class: "tavern-control",
        "aria-label": selector.label || copy("pages.tendencies.renderTendencies.text.f69dcc7758"),
        onChange: (event) => {
          runtime.updateLocation?.({ filters: { ...selected, session_key: event.currentTarget.value } }, { replace: false });
          runtime.refresh?.();
        },
      }, [{ value: "", label: selector.label }, ...rows(selector.options)].map((option) => el("option", {
        value: option.value,
        selected: String(selected.session_key ?? selector.value ?? "") === String(option.value),
        text: option.label,
      }))) : null,
      renderButton({
        variant: "secondary",
        label: copy("pages.dashboard.renderDashboard.label.f7a9973cc3"),
        onActivate: () => runtime.refresh?.(),
      }),
    ]),
  ]), "ready"));

  const observations = rows(value(model, "observations"));
  root.append(tendencyBlock("observation-grid", el("section", { class: "panel compact-panel tavern-tendencies-observations" }, [
    el("header", { class: "tavern-tendencies-heading" }, [
      el("div", {}, [
        el("h2", { text: copy("pages.tendencies.renderTendencies.text.f69dcc7758") }),
        el("p", { text: copy("pages.tendencies.renderTendencies.message.4e8b47bcb2") }),
      ]),
      el("span", { class: "tavern-tendencies-count", text: String(observations.length) }),
    ]),
    observations.length
      ? el("div", { class: "tavern-observation-grid" }, observations.map(observationCard))
      : el("p", { class: "tavern-tendencies-empty", text: copy("pages.tendencies.renderTendencies.message.24b4076d51") }),
  ]), observations.length ? "ready" : "empty"));

  const active = rows(value(model, "active_evidence"));
  const ignored = rows(value(model, "ignored_evidence"));
  root.append(el("div", { class: "tavern-tendencies-page-split" }, [
    tendencyBlock("active-evidence", el("section", { class: "panel compact-panel tavern-tendencies-evidence" }, [
      el("header", { class: "tavern-tendencies-evidence-heading" }, [
        el("h2", { text: copy("pages.tendencies.renderTendencies.text.083f5084f7") }),
        el("span", { text: String(active.length) }),
      ]),
      evidenceRows(active, runtime, copy("pages.tendencies.renderTendencies.emptyCopy.55f286362b")),
    ]), active.length ? "ready" : "empty"),
    tendencyBlock("ignored-evidence", el("section", { class: "panel compact-panel tavern-tendencies-evidence tavern-ignored-evidence" }, [
      el("header", { class: "tavern-tendencies-evidence-heading" }, [
        el("h2", { text: copy("pages.tendencies.renderTendencies.text.38a3a49efc", { p0: ignored.length }) }),
        el("span", { text: String(ignored.length) }),
      ]),
      evidenceRows(ignored, runtime, copy("pages.tendencies.renderTendencies.message.24b4076d51")),
    ]), ignored.length ? "ready" : "empty"),
  ]));

  const privacy = value(model, "privacy");
  root.append(tendencyBlock("privacy-notice", el("section", {
    class: "source-note tavern-privacy-notice tavern-tendencies-privacy",
    role: "note",
  }, [
    el("h2", { text: copy("pages.tendencies.renderTendencies.text.9d06d61ee4") }),
    el("p", { text: typeof privacy === "string" ? privacy : summary(privacy, label(privacy, copy("pages.tendencies.renderTendencies.message.4e8b47bcb2"))) }),
  ]), privacy ? "ready" : "partial"));

  if (model.phase === "permission") {
    root.replaceChildren(
      stateNotice(model, copy("pages.tendencies.renderTendencies.message.0d5c543935")),
      ...["toolbar", "observation-grid", "active-evidence", "ignored-evidence", "privacy-notice"]
        .map((id) => tendencyBlock(id, el("section", { class: "tavern-tendencies-redacted" }), "permission")),
    );
  }
  return root;
}

function tendencyBlock(id, node, state) {
  node.setAttribute("id", `tavern-tendencies-${id}`);
  node.setAttribute("data-block", id);
  node.setAttribute("data-state", state);
  node.setAttribute("data-testid", `tavern-block-tendencies-${id}`);
  return node;
}
