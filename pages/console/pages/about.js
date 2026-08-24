import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderStatusBadge } from "../components/status.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { openDetail } from "../dialogs/detail-dialog.js";
import { el, label, pageRoot, rows, stateNotice, summary, value } from "./shared.js";

function featureState(state) {
  if (state === "已支持" || state === "当前能力") return "ready";
  if (state === "条件支持") return "warning";
  return "partial";
}

function supportGroup(title, values, tone) {
  const items = rows(values).filter((item) => typeof item === "string" && item.trim());
  if (!items.length) return null;
  return el("section", { class: "tavern-about-boundary-group", "data-support-tone": tone }, [
    el("h4", { text: title }),
    el("div", { class: "tavern-about-support-list" }, items.map((item) => el("div", { class: "tavern-about-support-row" }, [
      el("strong", { text: item }),
      renderStatusBadge({ state: tone === "ready" ? "ready" : tone === "conditional" ? "warning" : "readonly", label: title }),
    ]))),
  ]);
}

function safeResource(item) {
  try {
    const url = new URL(String(item?.url || ""));
    if (url.protocol !== "https:" || url.hostname !== "github.com") return null;
    return { label: item.label || copy("pages.about.renderAbout.message.62cbaf206e"), href: url.href };
  } catch {
    return null;
  }
}

function resourceLink(resource) {
  return el("a", { class: "tavern-about-resource", href: resource.href, target: "_blank", rel: "noreferrer", text: resource.label });
}

function diagnosticsPanel(diagnostics) {
  return el("section", { class: "tavern-about-diagnostics-detail" }, [
    el("header", {}, [
      el("h3", { text: label(diagnostics, copy("pages.about.renderAbout.text.017bbdd9ee")) }),
      renderStatusBadge({ state: "readonly", label: diagnostics.state || copy("pages.about.renderAbout.text.017bbdd9ee") }),
    ]),
    el("p", { text: summary(diagnostics, label(diagnostics)) }),
  ]);
}

function openDiagnostics(diagnostics, handlers, opener) {
  if (!diagnostics || !handlers.dialogs?.openDialog) return;
  openDetail(handlers.dialogs, {
    objectKey: "about:diagnostics",
    opener,
    title: copy("pages.about.renderAbout.text.017bbdd9ee"),
    specialization: "about-diagnostics",
    tabs: [{ id: "summary", label: copy("pages.about.renderAbout.text.017bbdd9ee") }],
    activeTab: "summary",
    permissions: { summary: true },
    lazyPanelLoader: () => diagnosticsPanel(diagnostics),
  });
}

function featureCard(item, index) {
  return el("article", { class: "tavern-about-feature", "data-feature-order": index + 1 }, [
    el("header", {}, [el("span", { text: String(index + 1).padStart(2, "0") }), renderStatusBadge({ state: featureState(item.state), label: item.state || copy("pages.about.renderAbout.message.637edeebe2") })]),
    el("h3", { text: label(item, copy("pages.about.renderAbout.message.637edeebe2")) }),
    el("p", { text: summary(item, copy("pages.about.renderAbout.message.17f6c4a750")) }),
  ]);
}

export function renderAbout(model, handlers = {}) {
  const root = pageRoot(model, "tavern-about");
  root.setAttribute("class", `${root.className} tavern-about-page`);
  root.append(stateNotice(model, copy("pages.about.renderAbout.message.67aae250b5")));
  const versionValue = value(model, "version");
  const version = typeof versionValue === "string" ? versionValue : label(versionValue, copy("pages.about.renderAbout.message.c4384d9deb"));
  const support = value(model, "support") || {};
  const features = rows(value(model, "features")).slice(0, 6);
  const resources = rows(value(model, "resources")).map(safeResource).filter(Boolean);
  const diagnostics = value(model, "diagnostics");
  const supportGroups = [
    supportGroup(copy("pages.about.support.supported"), support.supported, "ready"),
    supportGroup(copy("pages.about.support.conditional"), support.conditional, "conditional"),
    supportGroup(copy("pages.about.support.unverified"), support.unverified, "unverified"),
  ].filter(Boolean);
  const canOpenDiagnostics = Boolean(model.permissions?.can_view_diagnostics && diagnostics && handlers.dialogs?.openDialog);
  const diagnosticButton = () => renderButton({
    variant: "secondary",
    label: copy("pages.about.renderAbout.text.017bbdd9ee"),
    disabledReason: canOpenDiagnostics ? "" : copy("components.capability_hub.dialog_unavailable"),
    onActivate: (_intent, event) => openDiagnostics(diagnostics, handlers, event.currentTarget),
  });

  root.append(
    el("div", { class: "tavern-page-toolbar" }, [
      el("div", { class: "tavern-page-toolbar-copy" }, [el("h2", { text: model.title || copy("pages.about.renderAbout.message.3265f9846b") }), el("p", { text: model.summary || copy("pages.about.renderAbout.message.495b477de7") })]),
      el("div", { class: "tavern-about-toolbar-actions" }, [model.permissions?.can_view_diagnostics ? diagnosticButton() : null, ...resources.map(resourceLink)]),
    ]),
    el("div", { class: "tavern-page-split" }, [
      el("section", { class: "tavern-about-install-panel" }, [
        el("header", { class: "tavern-about-panel-head" }, [
          el("div", {}, [el("h3", { text: copy("pages.about.renderAbout.label.1333558e37") }), el("p", { text: copy("pages.about.renderAbout.text.476b4feb46") })]),
          renderStatusBadge({ state: "ready", label: version }),
        ]),
        el("div", { class: "tavern-about-hero" }, [
          el("span", { text: model.title || copy("pages.about.renderAbout.message.3265f9846b") }),
          el("h3", { text: model.summary || copy("pages.about.renderAbout.message.495b477de7") }),
          el("p", { text: copy("pages.about.renderAbout.message.5642813f21") }),
        ]),
        el("div", { class: "tavern-about-install-actions" }, [
          renderButton({ variant: "secondary", label: copy("pages.about.version.copy"), onActivate: () => navigator.clipboard?.writeText(version) }),
          model.permissions?.can_view_diagnostics ? diagnosticButton() : null,
        ]),
      ]),
      el("aside", { class: "tavern-about-boundary" }, [
        el("span", { text: "PRIVACY & SUPPORT" }),
        el("h3", { text: label(support, copy("pages.about.renderAbout.message.6205c36b9d")) }),
        el("p", { text: summary(support, copy("pages.about.renderAbout.message.5642813f21")) }),
        ...supportGroups,
      ]),
    ]),
  );

  const featureGrid = el("section", { class: "tavern-about-feature-grid", "aria-label": copy("pages.about.renderAbout.message.637edeebe2") });
  if (features.length) featureGrid.append(...features.map(featureCard));
  else featureGrid.append(renderStatePanel({ phase: "empty", emptyCopy: copy("pages.about.rc8.d17254e628") }));
  root.append(featureGrid);
  if (resources.length) root.append(el("section", { class: "tavern-about-resources" }, [el("div", {}, [el("h3", { text: copy("pages.about.rc8.faaedffc6e") }), el("p", { text: copy("pages.about.rc8.584a296f85") })]), el("div", {}, resources.map(resourceLink))]));
  return root;
}
