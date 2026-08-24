import { copy } from "../../copy/catalog.js";
import { renderButton } from "../buttons.js";
import { collectionSurface, itemCards, surfaceNode, surfaceRecipe, surfaceRows } from "../surface-kit.js";

function hasIntent(payload, intent) {
  return surfaceRows(payload?.actions).some((action) => action?.intent === intent);
}

function actionDescriptor(payload, intent) {
  return surfaceRows(payload?.actions).find((action) => action?.intent === intent) || null;
}

async function recoverChallengeReceipt(client, sessionKey, key, operation) {
  if (!client || !sessionKey || !key) return null;
  try {
    const result = await client.get("sessions/gameplay", {
      query: { session_key: sessionKey, module_id: "challenge_engine", receipt_key: key },
      operation: `${operation}回执恢复`, dedupe: false,
    });
    return result?.data?.items?.[0] || result?.body?.data?.items?.[0] || null;
  } catch (_ignored) {
    return null;
  }
}

function challengeStartControl(payload, context) {
  const descriptor = actionDescriptor(payload, "challenge.start");
  if (!descriptor) return null;
  const client = context?.handlers?.client;
  const sessionKey = context?.handlers?.navigation?.objectKey || "";
  const options = surfaceRows(surfaceRows(descriptor.fields).find((field) => field.name === "template_key")?.options);
  const template = surfaceNode("select", { class: "tavern-control", "aria-label": copy("components.story.rc8.2b9246c337") }, options.map((item) => surfaceNode("option", { value: item.value, text: item.label })));
  const feedback = surfaceNode("div", { class: "tavern-challenge-feedback", role: "status", "aria-live": "polite" });
  let pendingKey = "";
  const start = async () => {
    if (!client || !sessionKey || !template.value) {
      feedback.textContent = copy("components.story.rc8.eceee136c3");
      return;
    }
    const key = pendingKey || crypto.randomUUID();
    pendingKey = key;
    try {
      await client.post("sessions/gameplay", {
        session_key: sessionKey, intent: "challenge.start", expected_revision: Number(payload?.contract?.state_revision || 0),
        template_key: template.value,
        world_revision: payload?.contract?.world_revision || "",
      }, { operation: copy("components.story.rc8.8dd00e43ce"), idempotencyKey: key });
      pendingKey = "";
      feedback.textContent = copy("components.story.rc8.33562a7085");
      await context?.handlers?.refresh?.();
    } catch (problem) {
      const recovered = await recoverChallengeReceipt(client, sessionKey, key, copy("components.story.rc8.8dd00e43ce"));
      if (recovered) {
        pendingKey = "";
        feedback.textContent = copy("components.story.rc8.c02d6a1e8b");
        await context?.handlers?.refresh?.();
        return;
      }
      feedback.textContent = `开始挑战失败：${problem?.message || "请求未完成"}；${problem?.recovery || "请刷新后重试。"}`;
    }
  };
  return surfaceNode("section", { class: "tavern-challenge-host-controls" }, [
    surfaceNode("h4", { text: copy("components.story.rc8.8dd00e43ce") }),
    surfaceNode("p", { text: copy("components.story.rc8.953fac7cd8") }),
    template,
    renderButton({ variant: "primary", label: copy("components.story.rc8.e7314f4a07"), onActivate: start }), feedback,
  ]);
}

function challengeActionEditor(payload, context) {
  if (!hasIntent(payload, "challenge.action.commit")) return null;
  const client = context?.handlers?.client;
  const sessionKey = context?.handlers?.navigation?.objectKey || "";
  const revision = Number(payload?.contract?.state_revision || 0);
  const kind = surfaceNode("select", { class: "tavern-control", "aria-label": copy("components.story.rc8.24e1608a21") }, [
    ["act", copy("components.story.rc8.9b6b9ca061")], ["investigate", copy("components.story.rc8.b130b39e12")], ["persuade", copy("components.story.rc8.61a29447b1")],
    ["pursue", copy("components.story.rc8.0c07f0fd33")], ["rescue", copy("components.story.rc8.8db821bcf9")], ["mitigate", copy("components.story.rc8.00ce1f34e8")],
    ["infiltrate", copy("components.story.rc8.b4677dabc4")], ["perform", copy("components.story.rc8.e36f12a858")], ["choose", copy("components.story.rc8.2c86914497")],
    ["withdraw", copy("components.story.rc8.0b4060e429")], ["negotiate", copy("components.story.rc8.3bd848ab67")],
  ].map(([value, label]) => surfaceNode("option", { value, text: label })));
  const description = surfaceNode("textarea", { class: "tavern-control", rows: "3", maxlength: "500", placeholder: copy("components.story.rc8.7a82bb0be5"), "aria-label": copy("components.story.rc8.3a049146e1") });
  const feedback = surfaceNode("div", { class: "tavern-challenge-feedback", role: "status", "aria-live": "polite" });
  let pendingCommitKey = "";
  const submit = async (intent, label, commit = false) => {
    if (!client || !sessionKey || !description.value.trim()) {
      feedback.textContent = copy("components.story.rc8.a5f63b1a97");
      return;
    }
    const key = commit ? (pendingCommitKey || crypto.randomUUID()) : "";
    if (commit) pendingCommitKey = key;
    feedback.textContent = `${label}处理中……`;
    try {
      const response = await client.post("sessions/gameplay", {
        session_key: sessionKey,
        module_id: "challenge_engine",
        intent,
        action_kind: kind.value,
        text: description.value.trim(),
        expected_revision: revision,
      }, { operation: label, idempotencyKey: key });
      const item = response?.data?.items?.[0] || response?.body?.data?.items?.[0] || {};
      const receipt = item.challenge_receipt || {};
      feedback.textContent = commit
        ? `挑战行动已提交：${receipt.result_band === "success" ? "成功推进" : receipt.result_band === "partial" ? "带代价推进" : "失败后推进"}。`
        : `预览完成：${surfaceRows(item.known_effects).join("；") || "行动可提交"}。预览没有写入状态。`;
      if (commit) {
        pendingCommitKey = "";
        await context?.handlers?.refresh?.();
      }
    } catch (problem) {
      const recovered = commit ? await recoverChallengeReceipt(client, sessionKey, key, label) : null;
      if (recovered) {
        pendingCommitKey = "";
        feedback.textContent = `${label}的响应曾中断，已恢复原回执；没有重复提交。`;
        await context?.handlers?.refresh?.();
        return;
      }
      feedback.textContent = `${label}失败：${problem?.message || "请求未完成"}。系统保留了输入；${problem?.recovery || "请刷新挑战后重新确认。"}`;
    }
  };
  return surfaceNode("section", { class: "tavern-challenge-editor" }, [
    surfaceNode("h4", { text: copy("components.story.rc8.e49eb5d802") }),
    surfaceNode("p", { text: copy("components.story.rc8.acbb3409fa") }),
    kind,
    description,
    surfaceNode("div", { class: "tavern-declared-surface-actions" }, [
      renderButton({ variant: "secondary", label: copy("components.story.rc8.5c2e586661"), onActivate: () => submit("challenge.action.preview", copy("components.story.rc8.5c2e586661")) }),
      renderButton({ variant: "primary", label: copy("components.story.rc8.38fd540593"), onActivate: () => submit("challenge.action.commit", copy("components.story.rc8.38fd540593"), true) }),
    ]),
    feedback,
  ]);
}

function challengeHostControls(payload, context) {
  if (!hasIntent(payload, "challenge.phase.advance") && !hasIntent(payload, "challenge.end")) return null;
  const client = context?.handlers?.client;
  const sessionKey = context?.handlers?.navigation?.objectKey || "";
  const revision = Number(payload?.contract?.state_revision || 0);
  const reason = surfaceNode("textarea", { class: "tavern-control", rows: "2", maxlength: "500", placeholder: copy("components.story.rc8.a74f2a0ca9"), "aria-label": copy("components.story.rc8.4b054f1a05") });
  const outcome = surfaceNode("select", { class: "tavern-control", "aria-label": copy("components.story.rc8.1b874d1409") }, [
    ["success", copy("components.story.rc8.053461ce86")], ["partial", copy("components.story.rc8.c1e9048a1e")], ["failure_forward", copy("components.outcomes.rc8.10270320c7")],
    ["retreat", copy("components.story.rc8.df47710f95")], ["negotiated", copy("components.story.rc8.9951c25f54")], ["aborted", copy("components.outcomes.rc8.1838a189cc")],
  ].map(([value, label]) => surfaceNode("option", { value, text: label })));
  const feedback = surfaceNode("div", { class: "tavern-challenge-feedback", role: "status", "aria-live": "polite" });
  const pendingKeys = new Map();
  const submit = async (intent, label) => {
    if (!client || !sessionKey) {
      feedback.textContent = copy("components.story.rc8.d2fa8acc7f");
      return;
    }
    if (!reason.value.trim()) {
      feedback.textContent = copy("components.story.rc8.e0d06c72d1");
      return;
    }
    const key = pendingKeys.get(intent) || crypto.randomUUID();
    pendingKeys.set(intent, key);
    try {
      await client.post("sessions/gameplay", {
        session_key: sessionKey, module_id: "challenge_engine", intent,
        expected_revision: revision, reason: reason.value.trim(), outcome: outcome.value,
      }, { operation: label, idempotencyKey: key });
      pendingKeys.delete(intent);
      feedback.textContent = `${label}已保存，并生成不可变回执。`;
      await context?.handlers?.refresh?.();
    } catch (problem) {
      const recovered = await recoverChallengeReceipt(client, sessionKey, key, label);
      if (recovered) {
        pendingKeys.delete(intent);
        feedback.textContent = `${label}的响应曾中断，已恢复原回执；没有重复执行。`;
        await context?.handlers?.refresh?.();
        return;
      }
      feedback.textContent = `${label}失败：${problem?.message || "请求未完成"}；${problem?.recovery || "请刷新后重试。"}`;
    }
  };
  return surfaceNode("section", { class: "tavern-challenge-host-controls" }, [
    surfaceNode("h4", { text: copy("components.story.rc8.3c45a15b0f") }), reason, outcome,
    surfaceNode("div", { class: "tavern-declared-surface-actions" }, [
      hasIntent(payload, "challenge.phase.advance") ? renderButton({ variant: "secondary", label: copy("components.story.rc8.34c22037b0"), onActivate: () => submit("challenge.phase.advance", copy("components.story.rc8.34c22037b0")) }) : null,
      hasIntent(payload, "challenge.end") ? renderButton({ variant: "danger", label: copy("components.story.rc8.e5bc2d0a3f"), onActivate: () => submit("challenge.end", copy("components.story.rc8.e5bc2d0a3f")) }) : null,
    ]), feedback,
  ]);
}

function challengeBoard(payload, context) {
  const data = payload?.data || {};
  if (!data.mode?.key) return surfaceRecipe(payload, surfaceNode("div", { class: "tavern-challenge-board" }, [
    itemCards(data.items, { fields: [["state", copy("components.story.rc8.59eb8d03ef")], ["risk", copy("components.story.rc8.f6bccd66af")], ["failure_forward", copy("components.outcomes.rc8.7c0e08e4a3")]] }),
    challengeStartControl(payload, context),
  ]), { actions: [] });
  const status = surfaceNode("div", { class: "tavern-challenge-status" }, [
    surfaceNode("strong", { text: data.mode.label || copy("components.story.rc8.fa52806566") }),
    surfaceNode("span", { text: data.phase?.label || copy("components.outcomes.rc8.03f07c12d5") }),
  ]);
  const section = (title, value, fields = []) => surfaceNode("section", { class: "tavern-challenge-section" }, [
    surfaceNode("h4", { text: title }), itemCards(value, { empty: copy("components.story.rc8.35fe8a0d86"), fields }),
  ]);
  return surfaceRecipe(payload, surfaceNode("div", { class: `tavern-challenge-board tavern-challenge-mode-${data.mode.key}` }, [
    data.objective_summary ? surfaceNode("p", { class: "tavern-challenge-objective", text: data.objective_summary }) : null,
    data.risk_summary ? surfaceNode("p", { class: "tavern-challenge-risk", text: `公开风险：${data.risk_summary}` }) : null,
    surfaceNode("p", { class: "tavern-challenge-progress", text: `进度 ${data.progress || 0} / ${data.target || 0}` }),
    section(copy("components.outcomes.rc8.32645e7919"), data.objectives, [["progress", copy("components.story.rc8.c827dc3403")], ["total", copy("components.story.rc8.b0763fd1e5")], ["failure_forward", copy("components.outcomes.rc8.7c0e08e4a3")]]),
    section(copy("components.story.rc8.8787a0444c"), data.options, [["risk", copy("components.definitions.rc8.af75e78cac")], ["limitation", copy("pages.characters.card.limits")]]),
    section(`${data.mode.label}要素`, data.mode_details, [["state", copy("dialogs.session_detail.openReceipt.label.6320b4a872")], ["risk", copy("components.definitions.rc8.af75e78cac")]]),
    data.telegraphs?.length ? surfaceNode("section", { class: "tavern-challenge-telegraphs" }, [surfaceNode("h4", { text: copy("components.story.rc8.afcd4dd975") }), surfaceNode("ul", {}, data.telegraphs.map((item) => surfaceNode("li", { text: item })))]) : null,
    section(copy("components.story.rc8.a847f067eb"), data.recent_receipts, [["progress", copy("components.story.rc8.85c36ce9cb")], ["state", copy("components.outcomes.rc8.bb7ef73495")]]),
    challengeStartControl(payload, context),
    challengeActionEditor(payload, context),
    challengeHostControls(payload, context),
  ]), { status, actions: [] });
}

function relationGraph(payload) {
  const data = payload.data || {};
  const nodes = surfaceRows(data.nodes || data.items);
  const edges = surfaceRows(data.edges);
  return surfaceRecipe(payload, surfaceNode("div", { class: "tavern-relation-semantic" }, [
    itemCards(nodes, { empty: payload.copy?.empty, fields: [["state_label", copy("pages.designer.rc8.253aaff19a")], ["recent_change", copy("components.story.rc8.51fc6e47f8")]] }),
    edges.length ? surfaceNode("ol", { class: "tavern-relation-links" }, edges.map((edge) => surfaceNode("li", { text: edge.label || copy("components.story.rc8.6f9ae78f23") }))) : null,
  ]));
}

function routeMap(payload) {
  const data = payload.data || {};
  return surfaceRecipe(payload, surfaceNode("ol", { class: "tavern-route-steps" }, surfaceRows(data.nodes || data.items).map((item) => surfaceNode("li", { dataset: { state: item.state || "" } }, [
    surfaceNode("strong", { text: item.label || copy("components.story.rc8.33c510eaa4") }),
    item.time_label ? surfaceNode("span", { text: item.time_label }) : null,
  ]))));
}

export const SURFACE_RENDERERS = Object.freeze({
  quest_board: (payload) => collectionSurface(payload, { fields: [["state_label", copy("visualizations.progress.renderProgress.message.f81ff55de1")], ["phase", copy("components.story.rc8.59eb8d03ef")], ["blocked_reason", copy("components.story.rc8.a0e84ab21e")]] }),
  challenge_board: challengeBoard,
  evidence_board: (payload) => collectionSurface(payload, { fields: [["state", copy("dialogs.session_detail.openReceipt.label.6320b4a872")], ["recent_change", copy("pages.dashboard.recentTimeline.text.cb29bae4c8")]] }),
  clock_board: (payload) => collectionSurface(payload, { fields: [["state_label", copy("dialogs.session_detail.openReceipt.label.6320b4a872")], ["progress_label", copy("visualizations.progress.renderProgress.message.f81ff55de1")], ["remaining_label", copy("components.story.rc8.d6822b0417")]] }),
  route_map: routeMap,
  relation_graph: relationGraph,
  npc_state_board: (payload) => collectionSurface(payload, { fields: [["state", copy("components.capability_hub.state_label")], ["risk", copy("components.definitions.rc8.af75e78cac")], ["deadline", copy("components.story.rc8.7e050f100d")]] }),
});
