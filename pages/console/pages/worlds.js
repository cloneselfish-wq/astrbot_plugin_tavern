import { copy } from "../copy/catalog.js";
import { renderButton } from "../components/buttons.js";
import { renderStatePanel } from "../components/empty-problem.js";
import { renderPagination } from "../components/pagination.js";
import { renderStatusBadge } from "../components/status.js";
import { formatUtc8Minute } from "../components/time.js";
import { openCapabilityDialog } from "../dialogs/capability-dialog.js";
import { renderWorldReadmeDocument } from "../visualizations/world-readme.js";
import { openEditor } from "../dialogs/editor-dialog.js";
import { el, label, pageRoot, rows, stateNotice, summary, value } from "./shared.js";

const statusState = (state) => state === "可以开团"
  ? "ready"
  : state === "已归档"
    ? "readonly"
    : state === "需要修复"
      ? "warning"
      : "unknown";

const descriptorFor = (item, intent) => rows(item?.available_actions)
  .find((action) => action?.transportReady === true && (!intent || action.intent === intent));

const bindDescriptor = (descriptor, item) => descriptor ? {
  ...descriptor,
  object_key: item.key,
  id: descriptor.action_id || descriptor.intent,
} : null;

const hasModuleCounts = (modules) => [modules?.enabled, modules?.declared]
  .every((entry) => entry !== null
    && entry !== undefined
    && entry !== ""
    && Number.isFinite(Number(entry)));

const densityLabels = Object.freeze({
  minimal: copy("pages.designer.rc8.75fc0214e4"),
  standard: copy("pages.designer.rc8.6bea77acef"),
  rich: copy("pages.designer.rc8.f54bab7a88"),
});

// These workspaces remain registered for a later repair pass, but must not be
// reachable from world cards or detail panels while their navigation entries
// are temporarily hidden.
const temporarilyHiddenWorkspaces = new Set(["designer", "author_jobs"]);

function publicProfile(item) {
  const profile = item?.adaptive_ui || item?.ui_profile || item?.ui_profile_summary;
  return profile && typeof profile === "object" && !Array.isArray(profile) ? profile : null;
}

function moduleValue(item) {
  const modules = item?.module_summary || {};
  return hasModuleCounts(modules)
    ? `${modules.enabled} / ${modules.declared}`
    : modules.state || copy("pages.worlds.worldCard.message.b33c23d659");
}

function contentEntries(item) {
  return rows(item?.content_summary).filter((entry) => entry?.label
    && entry?.value !== null
    && entry?.value !== undefined
    && Number(entry.value) >= 0);
}

function contentTotal(item) {
  return contentEntries(item).reduce((total, entry) => total + Number(entry.value || 0), 0);
}

function capabilityEntries(item) {
  return rows(item?.capability_summary).filter((entry) => entry?.label);
}

function profileLenses(item) {
  const profile = publicProfile(item);
  if (!profile) return [];
  const declared = rows(profile.lens_labels).filter(Boolean);
  return declared.length
    ? declared
    : rows(profile.live_lenses).map((entry) => entry?.label).filter(Boolean);
}

function executeDescriptor(descriptor, item, handlers, opener) {
  const bound = bindDescriptor(descriptor, item);
  if (!bound) return;
  if (rows(bound.fields).length) {
    const githubPreview = bound.intent === "github.world.preview";
    const githubCommit = bound.intent === "github.world.commit";
    const github = githubPreview || githubCommit;
    const fields = rows(bound.fields).map((field) => ({
      ...field,
      wide: field.name === "repo_url",
    }));
    if (githubPreview) {
      fields.push({
        name: "content_scope",
        type: "text",
        label: copy("pages.worlds.import.scope_label"),
        value: copy("pages.worlds.import.scope_value"),
        hint: copy("pages.worlds.import.scope_hint"),
        disabled: true,
        uiOnly: true,
      });
    }
    openEditor(handlers.dialogs, {
      objectKey: item.key,
      revision: bound.expected_revision,
      fields,
      opener,
      title: github ? copy("pages.worlds.import.editor_title") : bound.label,
      kicker: github ? copy("pages.worlds.import.editor_kicker") : "",
      specialization: github ? "world-import" : "",
      shellFooter: github,
      contextFacts: github ? [
        { label: copy("pages.worlds.import.context_state"), value: item.state || copy("pages.worlds.import.context_read") },
        { label: copy("pages.worlds.import.context_scope"), value: copy("pages.worlds.import.context_readonly") },
        { label: copy("pages.worlds.import.context_conflict"), value: copy("pages.worlds.import.context_preserve") },
      ] : [],
      intro: github ? {
        kicker: githubPreview ? copy("pages.worlds.import.intro_kicker") : copy("pages.worlds.import.commit_kicker"),
        title: githubPreview ? copy("pages.worlds.import.intro_title") : copy("pages.worlds.import.commit"),
        summary: githubPreview
          ? copy("pages.worlds.import.intro_summary")
          : copy("pages.worlds.import.commit_summary"),
      } : null,
      feedback: github ? {
        title: copy("pages.worlds.import.feedback_title"),
        summary: githubPreview
          ? copy("pages.worlds.import.feedback_summary")
          : copy("pages.worlds.import.commit_feedback_summary"),
      } : null,
      labels: github ? {
        cancel: copy("pages.worlds.import.cancel"),
        preview: copy("pages.worlds.import.preview_impact"),
        submit: copy("pages.worlds.import.confirm"),
      } : {},
      preview: () => ({
        summary: bound.description || copy("pages.worlds.executeDescriptor.message.bd65468b63"),
      }),
      submit: ({ draft, idempotencyKey }) => handlers.actions.execute(bound, {
        opener,
        input: draft,
        idempotencyKey,
      }),
    });
    return;
  }
  handlers.actions.execute(bound, { opener });
}

function markWorldAction(button, action) {
  if (button) button.dataset.worldAction = action;
  return button;
}

function definitionGrid(entries) {
  return el("dl", { class: "tavern-world-definition-grid" }, entries
    .filter((entry) => entry?.[1] !== null && entry?.[1] !== undefined && entry?.[1] !== "")
    .map(([term, description]) => el("div", { class: "tavern-world-definition-item" }, [
      el("dt", { text: term }),
      el("dd", { text: description }),
    ])));
}

function cardDisclosure(item) {
  const details = [];
  const contents = contentEntries(item);
  if (contents.length) {
    details.push(el("div", { class: "tavern-world-info-row" }, [
      el("div", {}, [
        el("strong", { text: copy("pages.worlds.card.content_complete") }),
        el("span", { text: copy("pages.worlds.card.content_complete_detail") }),
      ]),
      el("b", { text: copy("pages.worlds.detail.category_count", { p0: contents.length }) }),
    ]));
  }
  details.push(el("div", { class: "tavern-world-info-row" }, [
    el("div", {}, [
      el("strong", { text: copy("pages.worlds.card.module_optional") }),
      el("span", { text: copy("pages.worlds.card.module_optional_detail") }),
    ]),
    el("b", { text: moduleValue(item) }),
  ]));
  return el("details", { class: "tavern-world-disclosure" }, [
    el("summary", {}, [
      el("span", { text: copy("pages.worlds.card.checked") }),
      el("span", { text: copy("pages.worlds.card.on_demand") }),
    ]),
    el("div", { class: "tavern-world-disclosure-body" }, details),
  ]);
}

function cardTags(item) {
  const selected = rows(item?.display_tags).map((entry) => entry?.label).filter(Boolean);
  const declared = capabilityEntries(item).map((entry) => entry.label);
  const tags = (selected.length
    ? selected
    : declared.length
      ? declared
      : contentEntries(item).map((entry) => entry.label)).slice(0, 4);
  return tags.length ? el("div", { class: "tavern-world-tags" }, tags.map((entry) =>
    el("span", { class: "tavern-world-tag", text: entry }))) : null;
}

function capabilityList(entries, emptyText = copy("pages.worlds.detail.no_public_data")) {
  const visible = rows(entries).filter((entry) => entry?.label);
  if (!visible.length) {
    return el("div", { class: "tavern-capability-list" }, [
      el("div", {}, [el("span", { text: emptyText }), el("strong", { text: copy("pages.worlds.detail.not_provided") })]),
    ]);
  }
  return el("div", { class: "tavern-capability-list" }, visible.map((entry) => el("div", {}, [
    el("span", {}, [
      el("span", { text: entry.label }),
      entry.detail ? el("small", { text: entry.detail }) : null,
    ]),
    el("strong", { text: entry.value ?? entry.state ?? copy("pages.worlds.detail.not_provided") }),
  ])));
}

function capabilityStatGrid(entries, emptyText = copy("pages.worlds.detail.no_public_data")) {
  const visible = rows(entries).filter((entry) => entry?.label);
  return el("div", { class: "tavern-capability-stat-grid" }, visible.length
    ? visible.map((entry) => el("div", {}, [
      el("span", { text: entry.label }),
      el("strong", { text: entry.value ?? entry.state ?? copy("pages.worlds.detail.not_provided") }),
    ]))
    : [el("div", {}, [
      el("span", { text: emptyText }),
      el("strong", { text: copy("pages.worlds.detail.not_provided") }),
    ])]);
}

function capabilityHero({ kicker, heading, description, score, scoreLabel }) {
  return el("header", { class: "tavern-capability-hero" }, [
    el("div", { class: "tavern-capability-hero-copy" }, [
      el("span", { class: "tavern-story-kicker", text: kicker }),
      el("h3", { text: heading }),
      el("p", { text: description }),
    ]),
    el("div", { class: "tavern-capability-score" }, [
      el("span", {}, [el("strong", { text: score }), el("small", { text: scoreLabel })]),
    ]),
  ]);
}

function capabilitySection(title, description, badge, body, className = "") {
  return el("article", { class: `tavern-capability-section ${className}`.trim() }, [
    el("header", { class: "tavern-capability-section-head" }, [
      el("div", {}, [el("h4", { text: title }), el("p", { text: description })]),
      badge ? renderStatusBadge({ state: "ready", label: badge }) : null,
    ]),
    body,
  ]);
}

function boundary() {
  return el("div", { class: "tavern-capability-boundary" }, [
    el("strong", { text: copy("pages.worlds.detail.boundary_title") }),
    el("span", { text: copy("pages.worlds.detail.boundary_summary") }),
  ]);
}

function worldPackagePanel(item) {
  const contents = contentEntries(item);
  const capabilities = capabilityEntries(item);
  const lenses = profileLenses(item);
  const gameplay = item.gameplay_profile || {};
  const gameplayFacts = [
    { label: copy("pages.worlds.rc8.c694571ba3"), value: gameplay.tone },
    { label: copy("pages.worlds.rc8.84f8a22337"), value: gameplay.core_loop },
    { label: copy("pages.worlds.rc8.1ea0fc9a85"), value: gameplay.recommended_for },
    ...rows(gameplay.special_rules).map((rule, index) => ({ label: `特别规则 ${index + 1}`, value: rule })),
  ].filter((entry) => entry.value);
  const ending = contents.find((entry) => String(entry.label).includes("结局"));
  const sections = [
    capabilitySection(copy("pages.worlds.rc8.51fa8ef392"), copy("pages.worlds.rc8.78a0114de3"), gameplayFacts.length ? `${gameplayFacts.length} 项` : copy("pages.worlds.detail.not_provided"), capabilityList(gameplayFacts, copy("pages.worlds.rc8.7420307616")), "tavern-capability-section-wide"),
    capabilitySection(copy("pages.worlds.detail.content_makeup"), copy("pages.worlds.detail.content_makeup_summary"), copy("pages.worlds.detail.category_count", { p0: contents.length }), capabilityStatGrid(contents.map((entry) => ({ label: entry.label, value: entry.value })), copy("pages.worlds.detail.no_content_stats")), "tavern-capability-section-wide"),
    capabilitySection(copy("pages.worlds.detail.declared_capabilities"), copy("pages.worlds.detail.declared_capabilities_summary"), capabilities.length ? `${capabilities.length} 项` : copy("pages.worlds.detail.not_provided"), capabilityList(capabilities.map((entry) => ({ label: entry.label, value: entry.state, detail: entry.summary })), copy("pages.worlds.detail.no_capabilities")), "tavern-capability-section-wide"),
    capabilitySection(copy("pages.worlds.detail.openings_endings"), copy("pages.worlds.detail.openings_endings_summary"), item.state, capabilityList([
      { label: copy("pages.worlds.detail.players"), value: item.player_summary },
      { label: copy("pages.worlds.rc8.32776e0a9a"), value: item.content_version_label },
      { label: copy("pages.worlds.detail.openings_endings"), value: ending ? copy("pages.worlds.detail.ending_count", { p0: ending.value }) : copy("pages.worlds.detail.not_provided") },
    ])),
    capabilitySection(copy("pages.worlds.detail.runtime_projection"), copy("pages.worlds.detail.runtime_projection_summary"), copy("pages.worlds.detail.lens_count", { p0: lenses.length }), el("div", { class: "tavern-capability-flow" }, lenses.length
      ? lenses.map((lens, index) => el("div", {}, [el("small", { text: String(index + 1).padStart(2, "0") }), el("strong", { text: lens }), el("span", { text: copy("pages.worlds.card.on_demand") })]))
      : [el("div", {}, [el("strong", { text: copy("pages.worlds.detail.no_public_data") })])]), "tavern-capability-section-wide"),
  ];
  return el("section", { class: "tavern-capability-panel-body", "data-phase": "ready" }, [
    capabilityHero({ kicker: copy("pages.worlds.detail.world_kicker"), heading: copy("pages.worlds.detail.world_heading"), description: copy("pages.worlds.detail.world_summary"), score: contentTotal(item) || contents.length || copy("pages.worlds.detail.not_provided"), scoreLabel: copy("pages.worlds.detail.content_makeup") }),
    el("div", { class: "tavern-capability-section-grid" }, sections),
    boundary(),
  ]);
}

function navigationButton(workspace, labelText, handlers, enabled = true) {
  if (!enabled || temporarilyHiddenWorkspaces.has(workspace)) return null;
  return renderButton({ variant: "secondary", label: labelText, onActivate: () => {
    handlers.dialogs?.close?.("capability-navigate");
    handlers.navigate?.(workspace);
  } });
}

function authorEditPanel(item, handlers, detailAccess) {
  const actions = detailAccess.editActions;
  const actionButtons = actions.length ? actions.map((descriptor) => renderButton({
    variant: descriptor.intent === "world.archive" ? "danger" : "secondary",
    label: descriptor.label,
    intent: bindDescriptor(descriptor, item),
    onActivate: (_intent, event) => executeDescriptor(descriptor, item, handlers, event.currentTarget),
  })) : [el("p", { class: "tavern-world-unavailable", text: copy("pages.worlds.detail.no_public_data") })];
  return el("section", { class: "tavern-capability-panel-body", "data-phase": "ready" }, [
    capabilityHero({ kicker: copy("pages.worlds.detail.author_kicker"), heading: copy("pages.worlds.detail.author_heading"), description: copy("pages.worlds.detail.author_summary"), score: actions.length, scoreLabel: copy("pages.worlds.detail.available_operations") }),
    el("div", { class: "tavern-capability-section-grid" }, [
      capabilitySection(copy("pages.worlds.detail.available_operations"), copy("pages.worlds.detail.available_operations_summary"), actions.length ? String(actions.length) : copy("pages.worlds.detail.not_provided"), el("div", { class: "tavern-world-action-stack" }, actionButtons)),
      capabilitySection(copy("pages.worlds.detail.author_workspace"), copy("pages.worlds.detail.author_workspace_summary"), "", navigationButton("designer", copy("pages.worlds.detail.open_author_workspace"), handlers, detailAccess.canAuthor)),
    ]),
    boundary(),
  ]);
}

function artifactPanel(item, handlers, detailAccess) {
  const jobs = rows(item.author_jobs);
  const artifacts = jobs.flatMap((job) => rows(job.artifacts));
  const access = item.author_jobs_access || "unavailable";
  const body = [];
  if (access === "unavailable") {
    body.push(renderStatePanel({
      phase: "partial",
      operation: copy("pages.worlds.detail.artifact_read_operation"),
      problem: {
        message: copy("pages.worlds.detail.artifact_unavailable"),
        recovery: copy("pages.worlds.detail.artifact_recovery"),
      },
    }));
  } else if (!jobs.length) {
    body.push(el("p", { class: "tavern-world-unavailable", text: copy("pages.worlds.detail.artifact_empty") }));
  } else {
    body.push(...jobs.map((job) => capabilitySection(
      job.type_label || copy("pages.worlds.detail.author_artifact"),
      job.summary,
      job.state,
      el("div", { class: "tavern-world-action-stack" }, [
        capabilityList([
          { label: copy("pages.author_jobs.jobCard.type"), value: job.type_label },
          { label: copy("pages.author_jobs.jobCard.text.58a823852f"), value: `${job.attempts ?? 0} / ${job.max_attempts ?? 0}` },
          job.updated_at ? { label: copy("pages.author_jobs.jobCard.text.0a5f9a8929"), value: formatUtc8Minute(job.updated_at) } : null,
          job.failure_reason ? { label: copy("pages.author_jobs.jobCard.failure"), value: job.failure_reason } : null,
          { label: copy("pages.author_jobs.jobCard.automatic"), value: job.automatic_action },
          { label: copy("pages.author_jobs.jobCard.next_step"), value: job.next_step },
        ].filter(Boolean)),
        capabilityList(rows(job.artifacts).map((artifact) => ({
          label: artifact.label,
          value: artifact.state,
          detail: [artifact.summary, artifact.updated_at ? formatUtc8Minute(artifact.updated_at) : ""].filter(Boolean).join(" · "),
        })), copy("pages.author_jobs.rc8.8cac1d37a5")),
      ]),
      "tavern-capability-section-wide",
    )));
  }
  body.push(navigationButton("author_jobs", copy("pages.worlds.detail.open_jobs"), handlers, detailAccess.canOpenAuthorJobs));
  return el("section", { class: "tavern-capability-panel-body", "data-phase": "ready" }, [
    capabilityHero({ kicker: copy("pages.worlds.detail.artifact_kicker"), heading: copy("pages.worlds.detail.artifact_heading"), description: copy("pages.worlds.detail.artifact_summary"), score: artifacts.length || jobs.length, scoreLabel: copy("pages.worlds.detail.author_artifact") }),
    el("div", { class: "tavern-capability-section-grid" }, [
      capabilitySection(copy("pages.worlds.detail.author_artifact"), label(item), jobs.length ? String(jobs.length) : copy("pages.worlds.detail.not_provided"), el("div", { class: "tavern-world-action-stack" }, body), "tavern-capability-section-wide"),
    ]),
    boundary(),
  ]);
}

function resolutionPanel(item) {
  const profile = publicProfile(item) || {};
  const density = profile.density_label || densityLabels[profile.density];
  const visualizations = rows(profile.visualizations).map((entry) => entry?.title).filter(Boolean);
  const facts = [
    { label: copy("pages.worlds.rc8.44979ba1f1"), value: density },
    { label: copy("pages.designer.rc8.3c2576ca8d"), value: profileLenses(item).join("、") },
    { label: copy("pages.designer.rc8.1c3a33f31f"), value: visualizations.join("、") },
  ].filter((entry) => entry.value);
  const declared = item.resolution_details || {};
  if (declared.default_difficulty !== null && declared.default_difficulty !== undefined) {
    facts.push({ label: copy("pages.worlds.resolution.default_difficulty"), value: String(declared.default_difficulty) });
  }
  if (declared.minimum_difficulty !== null && declared.maximum_difficulty !== null
      && declared.minimum_difficulty !== undefined && declared.maximum_difficulty !== undefined) {
    facts.push({ label: copy("pages.worlds.resolution.difficulty_range"), value: `${declared.minimum_difficulty}—${declared.maximum_difficulty}` });
  }
  const relations = rows(declared.relations).map((entry) => ({
    label: `${entry.source} → ${entry.target}`,
    value: entry.result,
    summary: entry.summary,
  }));
  const difficulties = rows(declared.difficulties).map((entry) => ({
    label: entry.label,
    value: String(entry.value),
  }));
  const outcomes = rows(declared.outcomes).map((entry) => ({
    label: entry.label,
    value: entry.summary,
  }));
  const reactions = rows(declared.reactions).map((entry) => ({
    label: `${entry.source} + ${entry.target}`,
    value: entry.result,
    detail: entry.summary,
  }));
  const elements = rows(declared.elements).map((entry) => ({
    label: entry.label,
    value: entry.meaning,
    detail: entry.boundary ? `边界：${entry.boundary}` : "",
  }));
  const affinities = rows(declared.affinities).map((entry) => ({
    label: entry.label,
    value: entry.summary,
  }));
  const gameplayModules = rows(declared.gameplay_modules).map((entry) => ({
    label: entry.label,
    value: entry.state,
    detail: entry.summary,
  }));
  const coverage = declared.coverage || {};
  const gaps = rows(coverage.gaps);
  const elementalState = declared.elemental_enabled
    ? `${elements.length} 元素 · ${relations.length} 定向作用 · ${reactions.length} 反应`
    : copy("pages.worlds.rc8.a7c50edb20");
  const sections = [
    capabilitySection(copy("pages.worlds.rc8.2be0530685"), copy("pages.worlds.rc8.bb6f08a7aa"), `${coverage.covered ?? 0} / ${coverage.total ?? 0}`, capabilityList([
      { label: copy("pages.worlds.rc8.4ae1bfcb6b"), value: `${coverage.covered ?? 0} 项` },
      { label: copy("pages.worlds.rc8.2822c6173f"), value: gaps.length ? gaps.join("、") : copy("pages.characters.renderCharacters.message.484d556139") },
    ]), "tavern-capability-section-wide"),
    capabilitySection(copy("pages.worlds.detail.ui_preview"), copy("pages.worlds.detail.ui_preview_summary"), facts.length ? String(facts.length) : copy("pages.worlds.detail.not_provided"), capabilityList(facts, copy("pages.worlds.detail.no_public_data")), "tavern-capability-section-wide"),
    capabilitySection(copy("pages.worlds.resolution.difficulties"), copy("pages.worlds.resolution.difficulties_summary"), difficulties.length ? `${difficulties.length} 级` : copy("pages.worlds.detail.not_provided"), capabilityList(difficulties, copy("pages.worlds.resolution.no_difficulties"))),
    capabilitySection(copy("pages.worlds.resolution.outcomes"), copy("pages.worlds.resolution.outcomes_summary"), outcomes.length ? `${outcomes.length} 项` : copy("pages.worlds.detail.not_provided"), capabilityList(outcomes, copy("pages.worlds.resolution.no_outcomes"))),
  ];
  if (declared.elemental_enabled) {
    sections.push(
      capabilitySection(copy("pages.worlds.rc8.553db4362a"), copy("pages.worlds.rc8.08720f65fe"), elementalState, capabilityList(elements, copy("pages.worlds.rc8.e3e3f20705")), "tavern-capability-section-wide"),
      capabilitySection(copy("pages.worlds.rc8.8dd6ebabee"), copy("pages.worlds.rc8.9d4e0cf861"), relations.length ? `${relations.length} 条` : copy("pages.worlds.rc8.524a0bd8be"), capabilityList(relations, copy("pages.worlds.rc8.482ad5923a")), "tavern-capability-section-wide"),
      capabilitySection(copy("pages.worlds.rc8.c03b28a6a3"), copy("pages.worlds.rc8.628c6b1908"), affinities.length ? `${affinities.length} 类` : copy("pages.worlds.rc8.524a0bd8be"), capabilityList(affinities, copy("pages.worlds.rc8.0e113da0d9"))),
      capabilitySection(copy("pages.worlds.rc8.70bcaaa7f5"), copy("pages.worlds.rc8.1b4e3c340c"), declared.exposure_summary || copy("pages.worlds.rc8.524a0bd8be"), capabilityList([{ label: copy("pages.worlds.rc8.a085c8587b"), value: declared.exposure_summary || copy("pages.worlds.rc8.76f1645e11") }])),
      capabilitySection(copy("pages.worlds.rc8.02bf85b9af"), copy("pages.worlds.rc8.d122937fb3"), reactions.length ? `${reactions.length} 条` : copy("pages.worlds.rc8.524a0bd8be"), capabilityList(reactions, copy("pages.worlds.rc8.7cb6431ba5")), "tavern-capability-section-wide"),
    );
  } else {
    sections.push(capabilitySection(
      copy("pages.worlds.rc8.67e2544bf9"),
      copy("pages.worlds.rc8.6dbc6d67dd"),
      copy("pages.worlds.rc8.2746d99580"),
      capabilityList([{ label: copy("pages.designer.capability.fact.current_world"), value: elementalState }]),
      "tavern-capability-section-wide",
    ));
  }
  sections.push(capabilitySection(copy("pages.worlds.rc8.ed8d2a51f4"), copy("pages.worlds.rc8.b3835afe97"), gameplayModules.length ? `${gameplayModules.length} 项` : copy("pages.worlds.rc8.524a0bd8be"), capabilityList(gameplayModules, copy("pages.worlds.rc8.5c7df3e322")), "tavern-capability-section-wide"));
  return el("section", { class: "tavern-capability-panel-body", "data-phase": "ready" }, [
    capabilityHero({ kicker: "CORE GAMEPLAY", heading: copy("pages.worlds.detail.resolution"), description: copy("pages.worlds.rc8.690e9f3595"), score: `${coverage.percent ?? 0}%`, scoreLabel: copy("pages.worlds.rc8.c18197dd56") }),
    el("div", { class: "tavern-capability-section-grid" }, sections),
    boundary(),
  ]);
}

async function worldSettingPanel(item, handlers) {
  const root = el("section", { class: "tavern-capability-panel-body", "data-phase": "loading" }, [
    capabilityHero({ kicker: "PACKAGE README", heading: copy("pages.worlds.rc8.11cd242fd5"), description: copy("pages.worlds.rc8.a13391cb7c"), score: "…", scoreLabel: copy("pages.worlds.rc8.2d1341db77") }),
  ]);
  try {
    const descriptor = item.actions?.read_world_setting;
    if (!descriptor?.world_key) throw new Error(copy("pages.worlds.rc8.ab4c393f1b"));
    const payload = await handlers.client.get(descriptor.target, { query: {
      world_key: descriptor.world_key,
      expected_revision: descriptor.expected_revision,
    }, operation: copy("pages.worlds.rc8.c05b453e3f") });
    root.dataset.phase = "ready";
    const sectionNodes = rows(payload.sections).map((section) => {
      const body = el("div", { class: "tavern-world-readme-copy" }, [
        el("p", { class: "tavern-world-readme-intro", text: section.summary || copy("pages.worlds.rc8.bbb0b92459") }),
      ]);
      const button = renderButton({ variant: "secondary", label: copy("pages.worlds.rc8.60cdcdfa42"), onActivate: async () => {
        button.disabled = true;
        body.textContent = copy("pages.worlds.rc8.651e6dff79");
        try {
          const action = section.read_action;
          const detail = await handlers.client.get(action.target, { query: {
            world_key: action.world_key,
            section_key: action.section_key,
            expected_revision: action.expected_revision,
            expected_readme_revision: action.expected_readme_revision,
          }, operation: `读取世界设定章节「${section.title}」` });
          const markdown = rows(detail.blocks).map((block) => block.text || "").join("\n\n");
          body.replaceChildren(renderWorldReadmeDocument(markdown, { title: detail.title || section.title }));
          button.remove();
        } catch (error) {
          body.replaceChildren(renderStatePanel({ phase: "error", operation: copy("pages.worlds.rc8.d397c5c349"), problem: { message: error.message, recovery: copy("pages.worlds.rc8.f6c31e0572") } }));
          button.disabled = false;
        }
      }});
      return capabilitySection(section.title, section.summary || copy("pages.worlds.rc8.a0cfbba995"), "", el("div", {}, [body, button]), "tavern-capability-section-wide");
    });
    root.replaceChildren(
      capabilityHero({ kicker: "PACKAGE README", heading: copy("pages.worlds.rc8.11cd242fd5"), description: payload.summary, score: rows(payload.sections).length, scoreLabel: copy("pages.worlds.rc8.50685561fc") }),
      el("div", { class: "tavern-capability-section-grid" }, sectionNodes),
    );
  } catch (error) {
    root.dataset.phase = "error";
    root.replaceChildren(renderStatePanel({ phase: "error", operation: copy("pages.worlds.rc8.1fae97a887"), problem: { message: error.message, recovery: copy("pages.worlds.rc8.068931f891") } }));
  }
  return root;
}

function worldDetailPanel(tab, item, handlers, detailAccess) {
  if (tab === "world-setting") return worldSettingPanel(item, handlers);
  if (tab === "author-edit") return authorEditPanel(item, handlers, detailAccess);
  if (tab === "author-artifact") return artifactPanel(item, handlers, detailAccess);
  if (tab === "resolution") return resolutionPanel(item);
  return worldPackagePanel(item);
}

function worldDetailAccess(model, item) {
  const canView = model?.permissions?.can_view !== false;
  const pageReadonly = Boolean(model?.readonly || model?.permissions?.can_manage === false);
  const editActions = rows(item.available_actions).filter((entry) => entry?.transportReady === true);
  const access = item.author_jobs_access || "unavailable";
  return {
    canAuthor: canView && !pageReadonly && item.readonly !== true && editActions.length > 0,
    canOpenAuthorJobs: canView && access !== "restricted",
    editActions,
    permissions: {
      "world-package": canView,
      "author-edit": canView && !pageReadonly && item.readonly !== true && editActions.length > 0,
      "author-artifact": canView && access !== "restricted",
      resolution: canView,
      "world-setting": canView && Boolean(item.actions?.read_world_setting),
    },
  };
}

function openWorldDetail(model, item, handlers, opener) {
  if (!handlers.dialogs) return;
  const detailAccess = worldDetailAccess(model, item);
  openCapabilityDialog(handlers.dialogs, {
    objectKey: item.key,
    opener,
    title: copy("pages.worlds.detail.title"),
    kicker: copy("pages.worlds.detail.kicker"),
    footerLabel: copy("pages.worlds.detail.close"),
    panels: [
      { id: "world-package", label: copy("pages.worlds.detail.title") },
      { id: "world-setting", label: copy("pages.worlds.rc8.11cd242fd5") },
      { id: "author-edit", label: copy("pages.worlds.detail.author_edit") },
      { id: "author-artifact", label: copy("pages.worlds.detail.author_artifact") },
      { id: "resolution", label: copy("pages.worlds.detail.resolution") },
    ],
    activePanel: "world-setting",
    permissions: detailAccess.permissions,
    lazyPanelLoader: async (tab) => worldDetailPanel(tab, item, handlers, detailAccess),
  });
}

function worldCard(model, item, handlers) {
  const optional = descriptorFor(item, "world.module.toggle");
  const archive = descriptorFor(item, "world.archive");
  const state = statusState(item.state);
  const draft = state === "warning";
  const secondary = draft ? archive : optional || archive;
  const actions = draft
    ? [navigationButton("designer", copy("pages.worlds.detail.open_author_workspace"), handlers)]
    : [markWorldAction(renderButton({ variant: "secondary", label: copy("pages.worlds.detail.open"), onActivate: (_intent, event) => openWorldDetail(model, item, handlers, event.currentTarget) }), "open-detail")];
  if (secondary) actions.push(renderButton({
    variant: secondary.intent === "world.archive" ? "danger" : "secondary",
    label: secondary.label,
    intent: bindDescriptor(secondary, item),
    onActivate: (_intent, event) => executeDescriptor(secondary, item, handlers, event.currentTarget),
  }));
  const density = publicProfile(item)?.density_label || densityLabels[publicProfile(item)?.density];
  const subtitle = [item.author || copy("pages.worlds.card.source_builtin"), item.content_version_label].filter(Boolean).join(" · ");
  return el("article", { class: `tavern-world-card${draft ? " tavern-world-card-attention" : ""}`, "data-world-state": state, "data-object-key": item.key }, [
    el("header", { class: "tavern-world-card-header" }, [
      el("div", {}, [el("h3", { text: `《${label(item)}》` }), el("p", { text: subtitle })]),
      renderStatusBadge({ state: statusState(item.state), label: item.state }),
    ]),
    el("p", { class: "tavern-world-card-summary", text: summary(item) }),
    cardTags(item),
    definitionGrid([
      [copy("pages.worlds.detail.modules"), moduleValue(item)],
      [copy("pages.worlds.card.content_kinds"), copy("pages.worlds.detail.category_count", { p0: contentEntries(item).length })],
      [copy("pages.worlds.detail.players"), item.player_summary],
      [copy("pages.worlds.rc8.44979ba1f1"), density],
      draft && item.updated_at ? [copy("pages.author_jobs.statusState.message.0a5f9a8929"), formatUtc8Minute(item.updated_at)] : null,
    ]),
    draft ? null : cardDisclosure(item),
    !draft && item.updated_at ? el("time", { class: "tavern-world-card-time", datetime: item.updated_at, text: formatUtc8Minute(item.updated_at) }) : null,
    el("div", { class: "tavern-world-action-row" }, actions),
  ]);
}

function importTarget(imports, intent) {
  for (const item of imports) {
    const descriptor = descriptorFor(item, intent);
    if (descriptor) return { item, descriptor };
  }
  return null;
}

function importAction(target, labelText, variant, handlers) {
  return renderButton({
    variant,
    label: labelText,
    disabledReason: target ? "" : variant === "primary" ? copy("pages.worlds.import.commit_waiting") : copy("pages.worlds.import.preview_unavailable"),
    intent: target ? bindDescriptor(target.descriptor, target.item) : null,
    onActivate: (_intent, event) => target && executeDescriptor(target.descriptor, target.item, handlers, event.currentTarget),
  });
}

function twpPayload(payload, predicate) {
  return [payload, payload?.data, payload?.body, payload?.data?.data, payload?.data?.body]
    .find((candidate) => candidate && typeof candidate === "object" && predicate(candidate)) || null;
}

function twpIssueText(issues) {
  return rows(issues).map((issue) => [issue?.message, issue?.hint].filter(Boolean).join("；")).filter(Boolean);
}

function twpPreflightPreview(payload, file) {
  const report = twpPayload(payload, (candidate) => typeof candidate.compatible === "boolean");
  if (!report) {
    const problem = new Error(copy("pages.worlds.rc8.c3344fa717"));
    problem.recovery = copy("pages.worlds.rc8.35a912e779");
    problem.retryable = true;
    throw problem;
  }
  const issues = twpIssueText(report.issues);
  if (!report.compatible) {
    const problem = new Error(issues[0] || copy("pages.worlds.rc8.eb812ce10c"));
    problem.recovery = issues.slice(1).join("；") || copy("pages.worlds.rc8.08068fe033");
    throw problem;
  }
  const packageSummary = report.summary && typeof report.summary === "object" ? report.summary : {};
  const name = packageSummary.name || file.name;
  const version = packageSummary.version ? ` · 版本 ${packageSummary.version}` : "";
  const facts = [
    Number.isFinite(Number(packageSummary.declared_modules)) ? `模块 ${packageSummary.enabled_modules ?? 0}/${packageSummary.declared_modules}` : "",
    Number.isFinite(Number(packageSummary.entities)) ? `内容实体 ${packageSummary.entities}` : "",
    Number.isFinite(Number(packageSummary.files)) ? `文件 ${packageSummary.files}` : "",
  ].filter(Boolean);
  return {
    summary: `《${name}》${version} 已通过世界包预检。再次确认后才会导入。`,
    diff_summary: facts.length ? facts : [copy("pages.worlds.rc8.e6b58032b6")],
  };
}

function openLocalTwpImport(model, handlers, opener, file, { preflightOnly = false } = {}) {
  if (!handlers.dialogs?.openDialog || !handlers.client?.upload || !file) return;
  let verifiedPreflight = null;
  openEditor(handlers.dialogs, {
    fields: [{
      name: "selected_file",
      type: "text",
      label: copy("pages.worlds.rc8.2c4e2f8b1c"),
      hint: copy("pages.worlds.rc8.7c9cec4c45"),
      value: file.name,
      disabled: true,
      uiOnly: true,
      wide: true,
    }],
    opener,
    title: copy("pages.worlds.rc8.69f0b8dfb0"),
    kicker: "TWP ZIP",
    specialization: "world-local-zip-import",
    shellFooter: true,
    contextFacts: [
      { label: copy("components.capability_hub.state_label"), value: file.name },
      { label: copy("pages.worlds.rc8.2dfed5d0b8"), value: copy("pages.worlds.rc8.f76285120f") },
      { label: copy("pages.worlds.rc8.248c17d5af"), value: preflightOnly ? copy("pages.worlds.rc8.e6b58032b6") : copy("pages.worlds.rc8.46097da723") },
    ],
    intro: {
      kicker: copy("pages.worlds.rc8.104f31854a"),
      title: copy("pages.worlds.rc8.f678f8c2d5"),
      summary: copy("pages.worlds.rc8.9316e25c6e"),
    },
    labels: { cancel: copy("pages.settings.editor.cancel"), preview: copy("pages.worlds.rc8.9a6b29e3d6"), submit: preflightOnly ? copy("pages.worlds.rc8.a54d938ff5") : copy("pages.worlds.rc8.4a8d6841b4") },
    validate: () => {
      if (!file) return { selected_file: copy("pages.worlds.rc8.3fcfaffca9") };
      return String(file.name || "").toLowerCase().endsWith(".zip")
        ? {}
        : { selected_file: copy("pages.worlds.rc8.901c33fc66") };
    },
    preview: async () => {
      verifiedPreflight = twpPreflightPreview(await handlers.client.upload(
        "worlds/twp/preflight",
        file,
        { operation: copy("pages.worlds.rc8.a54d938ff5") },
      ), file);
      return verifiedPreflight;
    },
    submit: async ({ idempotencyKey }) => {
      if (preflightOnly) {
        return verifiedPreflight;
      }
      const payload = await handlers.client.upload(
        "worlds/twp/import",
        file,
        { operation: copy("pages.worlds.rc8.69f0b8dfb0"), idempotencyKey },
      );
      const imported = twpPayload(payload, (candidate) => candidate.item && candidate.preflight);
      if (!imported) {
        const problem = new Error(copy("pages.worlds.rc8.802306d5c7"));
        problem.recovery = copy("pages.worlds.rc8.4885bcc86e");
        throw problem;
      }
      await handlers.refresh?.();
      return {};
    },
  });
}

function localTwpPicker(model, handlers, { preflightOnly = false, available = true } = {}) {
  const input = el("input", {
    class: "tavern-world-file-input",
    type: "file",
    accept: ".zip,application/zip",
    tabindex: "-1",
    "aria-hidden": "true",
  });
  let opener = null;
  input.dataset.worldImportMode = preflightOnly ? "preflight" : "import";
  input.addEventListener("change", () => {
    const file = input.files?.[0];
    input.value = "";
    if (file) openLocalTwpImport(model, handlers, opener, file, { preflightOnly });
  });
  const button = renderButton({
    variant: preflightOnly ? "secondary" : "primary",
    label: preflightOnly ? copy("pages.worlds.rc8.a54d938ff5") : copy("pages.worlds.toolbar.import"),
    disabledReason: available ? "" : copy("pages.worlds.rc8.8a3ae53d54"),
    onActivate: (_intent, event) => {
      if (!available) return;
      opener = event.currentTarget;
      input.click();
    },
  });
  button.dataset.worldAction = preflightOnly ? "local-zip-preflight" : "local-zip-import";
  return [button, input];
}

function toolbar(model, imports, handlers) {
  const fields = rows(model.filters);
  const values = Object.fromEntries(fields.map((field) => [field.name, field.value]));
  const search = el("input", {
    class: "tavern-control tavern-world-search",
    type: "search",
    value: handlers.navigation?.filters?.q ?? values.q ?? "",
    placeholder: copy("pages.worlds.statusState.message.c725f6b1a8"),
    "aria-label": copy("pages.worlds.statusState.message.c725f6b1a8"),
  });
  const applySearch = () => {
    handlers.updateLocation?.({ filters: { ...(handlers.navigation?.filters || {}), q: search.value, cursor: "" } });
    return handlers.refresh?.();
  };
  search.addEventListener("change", applySearch);
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    applySearch();
  });
  const localImportAvailable = model?.readonly !== true
    && model?.permissions?.can_manage === true
    && Boolean(handlers.dialogs?.openDialog && handlers.client?.upload);
  const [localPreflight, localPreflightInput] = localTwpPicker(model, handlers, { preflightOnly: true, available: localImportAvailable });
  const [localImport, localImportInput] = localTwpPicker(model, handlers, { available: localImportAvailable });
  return el("section", { class: "tavern-world-toolbar" }, [
    el("div", { class: "tavern-world-toolbar-copy" }, [el("h2", { text: copy("pages.worlds.toolbar.title") }), el("p", { text: copy("pages.worlds.toolbar.summary") })]),
    el("div", { class: "tavern-world-filter-row" }, [
      search,
      localPreflight,
      localImport,
      localPreflightInput,
      localImportInput,
    ]),
  ]);
}

function densityStrip(model, items, imports) {
  const summaryValue = value(model, "summary") || {};
  const ready = items.filter((item) => item.state === "可以开团").length;
  const repair = items.filter((item) => item.state === "需要修复").length;
  const defaultWorld = summaryValue.default_world_label || items.find((item) => item.is_default)?.label || copy("pages.worlds.stats.default_empty");
  const stats = [
    [copy("pages.worlds.stats.available"), ready, copy("pages.worlds.stats.available_detail")],
    [copy("pages.worlds.stats.repair"), repair, copy("pages.worlds.stats.repair_detail")],
    [copy("pages.worlds.stats.import"), imports.length, copy("pages.worlds.stats.import_detail")],
    [copy("pages.worlds.stats.default"), defaultWorld, summaryValue.count ? copy("pages.worlds.stats.world_count", { p0: summaryValue.count }) : copy("pages.worlds.stats.default_empty")],
  ];
  return el("section", { class: "tavern-world-density" }, stats.map(([term, amount, detail]) => el("article", { class: "tavern-world-density-stat" }, [
    el("small", { text: term }), el("strong", { text: amount }), el("span", { text: detail }),
  ])));
}

function importFlow(commitReady) {
  const steps = [
    ["01", copy("pages.worlds.rc8.006347ca61"), copy("pages.worlds.rc8.5fb67c315f"), !commitReady],
    ["02", copy("pages.worlds.rc8.a2e8210e73"), copy("pages.worlds.rc8.9ba93277bd"), commitReady],
    ["03", copy("pages.worlds.rc8.a0f42f891d"), copy("pages.worlds.rc8.2bedf717a2"), false],
  ];
  return el("ol", { class: "tavern-world-import-flow" }, steps.map(([number, title, detail, current]) => el("li", { "data-current": String(current) }, [
    el("small", { text: number }), el("strong", { text: title }), el("span", { text: detail }),
  ])));
}

function importPanel(imports, handlers) {
  const preview = importTarget(imports, "github.world.preview");
  const commit = importTarget(imports, "github.world.commit");
  const current = commit?.item || preview?.item || imports[0] || {};
  return el("section", { class: "tavern-world-import-panel" }, [
    el("header", { class: "tavern-world-import-head" }, [
      el("div", {}, [el("h2", { text: copy("pages.worlds.import.panel_title") }), el("p", { text: copy("pages.worlds.import.panel_summary") })]),
      renderStatusBadge({ state: commit ? "ready" : "waiting", label: current.state || copy("pages.worlds.import.waiting") }),
    ]),
    importFlow(Boolean(commit)),
    el("div", { class: "tavern-world-import-actions" }, [
      markWorldAction(importAction(preview, copy("pages.worlds.import.preview"), "secondary", handlers), "import-preview"),
      markWorldAction(importAction(commit, copy("pages.worlds.import.commit"), "primary", handlers), "import-commit"),
    ]),
  ]);
}

export function renderWorlds(model, handlers = {}) {
  const root = pageRoot(model, "tavern-worlds");
  const items = rows(value(model, "world_cards"));
  const imports = rows(value(model, "github_previews"));
  root.append(stateNotice(model, copy("pages.worlds.renderWorlds.message.2d89cff62a")), toolbar(model, imports, handlers), densityStrip(model, items, imports));
  const grid = el("section", { class: "tavern-world-grid", "aria-label": copy("pages.worlds.renderWorlds.message.887e771413") }, items.map((item) => worldCard(model, item, handlers)));
  if (!items.length && model.phase === "ready") grid.append(renderStatePanel({ phase: "empty", emptyCopy: copy("pages.worlds.renderWorlds.emptyCopy.fb3e90fb63") }));
  root.append(grid, importPanel(imports, handlers));
  if (model.pagination) root.append(renderPagination({
    workspace: "worlds",
    ...model.pagination,
    onPage: (cursor) => {
      handlers.updateLocation?.({ filters: { ...(handlers.navigation?.filters || {}), cursor } });
      return handlers.refresh?.();
    },
  }));
  return root;
}
