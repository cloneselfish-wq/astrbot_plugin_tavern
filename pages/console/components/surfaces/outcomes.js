import { copy } from "../../copy/catalog.js";
import { renderButton } from "../buttons.js";
import { collectionSurface, itemCards, surfaceNode, surfaceRecipe, surfaceRows } from "../surface-kit.js";

function hasIntent(payload, intent) {
  return surfaceRows(payload?.actions).some((action) => action?.intent === intent);
}

function actionDescriptor(payload, intent) {
  return surfaceRows(payload?.actions).find((action) => action?.intent === intent) || null;
}

function fieldDescriptor(descriptor, name) {
  return surfaceRows(descriptor?.fields).find((field) => field?.name === name) || null;
}

function optionalSelect(descriptor, name, fallbackLabel) {
  const field = fieldDescriptor(descriptor, name);
  if (!field || !surfaceRows(field.options).length) return null;
  return surfaceNode("select", { class: "tavern-control", "aria-label": field.label || fallbackLabel }, [
    surfaceNode("option", { value: "", text: `不选择${field.label || fallbackLabel}` }),
    ...surfaceRows(field.options).map((item) => surfaceNode("option", { value: item.value, text: item.label })),
  ]);
}

async function recoverReceipt(client, sessionKey, moduleId, key, operation) {
  if (!client || !sessionKey || !key) return null;
  try {
    const result = await client.get("sessions/gameplay", {
      query: { session_key: sessionKey, module_id: moduleId, receipt_key: key },
      operation: `${operation}回执恢复`, dedupe: false,
    });
    return result?.data?.items?.[0] || result?.body?.data?.items?.[0] || null;
  } catch (_ignored) {
    return null;
  }
}

function tacticalActionEditor(payload, context) {
  if (!hasIntent(payload, "tactical.action.commit")) return null;
  const handlers = context?.handlers || {};
  const client = handlers.client;
  const sessionKey = handlers.navigation?.objectKey || "";
  const revision = Number(payload.contract?.state_revision || 0);
  const descriptor = actionDescriptor(payload, "tactical.action.commit");
  const actionField = fieldDescriptor(descriptor, "action_kind");
  const action = surfaceNode("select", { class: "tavern-control", "aria-label": copy("components.outcomes.rc8.534bf41c44") }, [
    ...surfaceRows(actionField?.options).map((item) => surfaceNode("option", { value: item.value, text: item.label })),
  ]);
  const description = surfaceNode("textarea", {
    class: "tavern-control",
    rows: "3",
    maxlength: "500",
    placeholder: copy("components.outcomes.rc8.01d3afa13f"),
    "aria-label": copy("components.outcomes.rc8.e01bf90d00"),
  });
  const target = optionalSelect(descriptor, "target_key", copy("components.outcomes.rc8.d4543e895c"));
  const zone = optionalSelect(descriptor, "zone_key", copy("components.outcomes.rc8.125685a685"));
  const objective = optionalSelect(descriptor, "objective_key", copy("components.outcomes.rc8.32645e7919"));
  const capability = optionalSelect(descriptor, "capability_or_item_key", copy("components.outcomes.rc8.190d415b68"));
  const feedback = surfaceNode("div", { class: "tavern-tactical-feedback", role: "status", "aria-live": "polite" });
  let pendingCommitKey = "";
  const submit = async (intent, label, { commit = false } = {}) => {
    if (!client || !sessionKey || !description.value.trim()) {
      feedback.textContent = copy("components.outcomes.rc8.becdca76e1");
      return;
    }
    const key = commit ? (pendingCommitKey || crypto.randomUUID()) : "";
    if (commit) pendingCommitKey = key;
    feedback.textContent = `${label}处理中……`;
    try {
      const draft = { action_kind: action.value, description: description.value.trim() };
      if (target?.value) draft.target_key = target.value;
      if (zone?.value) draft.zone_key = zone.value;
      if (objective?.value) draft.objective_key = objective.value;
      if (capability?.value) draft.capability_or_item_key = capability.value;
      const result = await client.post("sessions/gameplay", {
        session_key: sessionKey,
        module_id: "tactical_conflict",
        intent,
        expected_revision: revision,
        draft,
      }, {
        operation: label,
        idempotencyKey: key,
      });
      const item = result?.data?.items?.[0] || result?.body?.data?.items?.[0] || {};
      const receipt = item.tactical_receipt || {};
      feedback.textContent = commit
        ? `行动已提交到待锁定区${receipt.replaced ? "，并替换了自己的上一份声明" : ""}。尚未掷骰或扣除最终额度。`
        : `预览完成：${surfaceRows(item.known_effects).join("；") || "已通过行动额度和阶段检查"}。预览没有写入状态。`;
      if (commit) {
        pendingCommitKey = "";
        await handlers.refresh?.();
      }
    } catch (problem) {
      const recovered = commit ? await recoverReceipt(client, sessionKey, "tactical_conflict", key, label) : null;
      if (recovered) {
        pendingCommitKey = "";
        feedback.textContent = `${label}的响应曾中断，已按原防重复凭证恢复回执；没有重复提交。`;
        await handlers.refresh?.();
        return;
      }
      feedback.textContent = `${label}失败：${problem?.message || "请求未完成"}。系统保留了草稿；${problem?.recovery || "请刷新战况后重新确认。"}`;
    }
  };
  const preview = renderButton({ variant: "secondary", label: copy("components.outcomes.rc8.d07faf2690"), onActivate: () => submit("tactical.action.preview", copy("components.outcomes.rc8.3960edc1d7")) });
  const commit = renderButton({ variant: "primary", label: copy("components.outcomes.rc8.ce1c2c8e8f"), onActivate: () => submit("tactical.action.commit", copy("components.outcomes.rc8.7d3a2ac72d"), { commit: true }) });
  return surfaceNode("section", { class: "tavern-tactical-editor" }, [
    surfaceNode("h4", { text: copy("components.outcomes.rc8.202d9a03ea") }),
    surfaceNode("p", { text: copy("components.outcomes.rc8.95e2fda273") }),
    action,
    description,
    target,
    zone,
    objective,
    capability,
    surfaceNode("div", { class: "tavern-declared-surface-actions" }, [preview, commit]),
    feedback,
  ]);
}

function tacticalHostControls(payload, context) {
  const hostIntents = ["tactical.conflict.start", "tactical.phase.advance", "tactical.correction.apply", "tactical.conflict.end"];
  if (!hostIntents.some((intent) => hasIntent(payload, intent))) return null;
  const client = context?.handlers?.client;
  const sessionKey = context?.handlers?.navigation?.objectKey || "";
  const revision = Number(payload.contract?.state_revision || 0);
  const phase = payload.data?.phase?.key || "setup";
  const startDescriptor = actionDescriptor(payload, "tactical.conflict.start");
  const templateOptions = surfaceRows(surfaceRows(startDescriptor?.fields).find((field) => field.name === "template_key")?.options);
  const template = startDescriptor ? surfaceNode("select", { class: "tavern-control", "aria-label": copy("components.outcomes.rc8.551416f0f0") }, templateOptions.map((item) => surfaceNode("option", { value: item.value, text: item.label }))) : null;
  const intensityField = fieldDescriptor(startDescriptor, "intensity");
  const intensity = intensityField ? surfaceNode("select", { class: "tavern-control", "aria-label": intensityField.label || copy("components.outcomes.rc8.89199a40f9") }, surfaceRows(intensityField.options).map((item) => surfaceNode("option", { value: item.value, text: item.label, selected: item.value === intensityField.default }))) : null;
  const reason = surfaceNode("textarea", { class: "tavern-control", rows: "2", maxlength: "500", placeholder: copy("components.outcomes.rc8.56104b446f"), "aria-label": copy("components.outcomes.rc8.a5bab6397a") });
  const correction = surfaceNode("input", { class: "tavern-control", maxlength: "220", placeholder: copy("components.outcomes.rc8.4edc10cdff"), "aria-label": copy("components.outcomes.rc8.4edc10cdff") });
  const outcome = surfaceNode("select", { class: "tavern-control", "aria-label": copy("components.outcomes.rc8.950973ed8e") }, [
    ["victory", copy("components.outcomes.rc8.521a53f756")], ["partial_success", copy("components.outcomes.rc8.64b452485c")], ["retreat", copy("components.outcomes.rc8.f31c2fe5ec")],
    ["negotiated", copy("components.outcomes.rc8.2ad861fa38")], ["defeat_forward", copy("components.outcomes.rc8.10270320c7")], ["aborted_by_host", copy("components.outcomes.rc8.1838a189cc")],
  ].map(([value, label]) => surfaceNode("option", { value, text: label })));
  const feedback = surfaceNode("div", { class: "tavern-tactical-feedback", role: "status", "aria-live": "polite" });
  const pendingKeys = new Map();
  const submit = async (intent, label) => {
    if (!client || !sessionKey || (intent !== "tactical.conflict.start" && !reason.value.trim())) {
      feedback.textContent = copy("components.outcomes.rc8.0143ab6f5f");
      return;
    }
    if (intent === "tactical.conflict.start" && !template?.value) {
      feedback.textContent = copy("components.outcomes.rc8.f87cbf5d1b");
      return;
    }
    if (intent === "tactical.correction.apply" && !correction.value.trim()) {
      feedback.textContent = copy("components.outcomes.rc8.472b0d8ad3");
      return;
    }
    const key = pendingKeys.get(intent) || crypto.randomUUID();
    pendingKeys.set(intent, key);
    try {
      await client.post("sessions/gameplay", {
        session_key: sessionKey, module_id: "tactical_conflict", intent,
        expected_revision: revision,
        reason: reason.value.trim(), outcome: outcome.value,
        template_key: template?.value || "",
        intensity: intensity?.value || "",
        world_revision: payload?.contract?.world_revision || "",
        correction: { field: "objective", value: correction.value.trim() },
      }, { operation: label, idempotencyKey: key });
      pendingKeys.delete(intent);
      feedback.textContent = `${label}已保存，并生成不可变回执。`;
      await context?.handlers?.refresh?.();
    } catch (problem) {
      const recovered = await recoverReceipt(client, sessionKey, "tactical_conflict", key, label);
      if (recovered) {
        pendingKeys.delete(intent);
        feedback.textContent = `${label}的响应曾中断，已恢复原回执；没有重复执行。`;
        await context?.handlers?.refresh?.();
        return;
      }
      feedback.textContent = `${label}失败：${problem?.message || "请求未完成"}；${problem?.recovery || "请刷新战况后重试。"}`;
    }
  };
  return surfaceNode("section", { class: "tavern-tactical-host-controls" }, [
    surfaceNode("h4", { text: copy("components.outcomes.rc8.98beb809e9") }),
    surfaceNode("p", { text: copy("components.outcomes.rc8.897173b4ce") }),
    template,
    intensity,
    reason,
    hasIntent(payload, "tactical.correction.apply") ? correction : null,
    hasIntent(payload, "tactical.conflict.end") ? outcome : null,
    surfaceNode("div", { class: "tavern-declared-surface-actions" }, [
      hasIntent(payload, "tactical.conflict.start") ? renderButton({ variant: "primary", label: copy("components.outcomes.rc8.b8329a7082"), onActivate: () => submit("tactical.conflict.start", copy("components.outcomes.rc8.b8329a7082")) }) : null,
      hasIntent(payload, "tactical.phase.advance") ? renderButton({ variant: "secondary", label: phase === "declare" ? copy("components.outcomes.rc8.6bb83b5886") : copy("components.outcomes.rc8.e9870c9649"), onActivate: () => submit("tactical.phase.advance", phase === "declare" ? copy("components.outcomes.rc8.6bb83b5886") : copy("components.outcomes.rc8.e9870c9649")) }) : null,
      hasIntent(payload, "tactical.correction.apply") ? renderButton({ variant: "secondary", label: copy("components.outcomes.rc8.84c19278ff"), onActivate: () => submit("tactical.correction.apply", copy("components.outcomes.rc8.84c19278ff")) }) : null,
      hasIntent(payload, "tactical.conflict.end") ? renderButton({ variant: "danger", label: copy("components.outcomes.rc8.77f1bf6dee"), onActivate: () => submit("tactical.conflict.end", copy("components.outcomes.rc8.77f1bf6dee")) }) : null,
    ]), feedback,
  ]);
}

function tacticalBoard(payload, context) {
  const data = payload.data || {};
  const budget = surfaceRows(data.actors).find((actor) => actor.is_self)?.action_budget || {};
  const status = surfaceNode("div", { class: "tavern-tactical-round" }, [
    surfaceNode("strong", { text: `第 ${data.round || 1} 轮` }),
    surfaceNode("span", { text: data.phase?.label || copy("components.outcomes.rc8.03f07c12d5") }),
  ]);
  const section = (title, content) => surfaceNode("section", { class: "tavern-tactical-section" }, [surfaceNode("h4", { text: title }), content]);
  return surfaceRecipe(payload, surfaceNode("div", { class: "tavern-tactical-board" }, [
    data.objective_summary ? surfaceNode("p", { class: "tavern-tactical-objective", text: data.objective_summary }) : null,
    section(copy("components.outcomes.rc8.32645e7919"), itemCards(data.objectives, { empty: copy("components.outcomes.rc8.f770a78099"), fields: [["progress", copy("visualizations.progress.renderProgress.message.f81ff55de1")], ["failure_forward", copy("components.outcomes.rc8.7c0e08e4a3")]] })),
    section(copy("components.outcomes.rc8.0e56970de8"), itemCards([...(surfaceRows(data.zones)), ...(surfaceRows(data.escape_routes))], { empty: copy("components.outcomes.rc8.88433b1208"), fields: [["risk", copy("components.definitions.rc8.af75e78cac")]] })),
    section(copy("components.outcomes.rc8.5875c13b13"), itemCards(data.actors, { empty: copy("components.outcomes.rc8.b21cc78097"), fields: [["zone_label", copy("components.outcomes.rc8.3382870bc4")], ["fate_label", copy("components.outcomes.rc8.305624f08c")], ["guard_label", copy("components.outcomes.rc8.c6d76ab94b")]] })),
    section(copy("components.outcomes.rc8.c08f542bfd"), itemCards(data.known_threats, { empty: copy("components.outcomes.rc8.b90926b77e"), fields: [["state", copy("components.outcomes.rc8.bbe525a65d")], ["risk", copy("components.outcomes.rc8.40873ebd8e")]] })),
    surfaceNode("p", { class: "tavern-tactical-budget", text: `本轮可用：主要行动 ${budget.major ?? 0}，移动 ${budget.maneuver ?? 0}，反应 ${budget.reaction ?? 0}` }),
    section(copy("components.outcomes.rc8.4ae13a6199"), itemCards(data.recent_receipts, { empty: copy("components.outcomes.rc8.ad6d2891d6"), fields: [["actor_label", copy("components.outcomes.rc8.0e2bd07fae")], ["action_label", copy("components.outcomes.rc8.253fb75906")], ["result_label", copy("components.outcomes.rc8.bb7ef73495")], ["roll_summary", copy("components.outcomes.rc8.9e3d5f12d9")], ["outcome_label", copy("components.outcomes.rc8.2835487a98")]] })),
    tacticalActionEditor(payload, context),
    tacticalHostControls(payload, context),
  ]), { status, actions: [] });
}

function replayTimeline(payload) {
  const items = surfaceRows(payload.data?.items);
  const main = items.length ? surfaceNode("ol", { class: "tavern-declared-timeline" }, items.map((item) => surfaceNode("li", {}, [
    surfaceNode("strong", { text: item.label || copy("components.outcomes.rc8.6f8862bfa0") }),
    item.summary ? surfaceNode("p", { text: item.summary }) : null,
  ]))) : itemCards([], { empty: payload.copy?.empty });
  return surfaceRecipe(payload, main);
}

export const SURFACE_RENDERERS = Object.freeze({
  tactical_board: tacticalBoard,
  ending_outlook: (payload) => collectionSurface(payload, { fields: [["state", copy("components.outcomes.rc8.fc58618f79")], ["risk", copy("components.outcomes.rc8.17a71107c8")], ["limitation", copy("components.outcomes.rc8.676e0aa185")]] }),
  replay_timeline: replayTimeline,
});
