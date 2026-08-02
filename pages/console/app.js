import { bridge } from "./core/bridge.js";
import {
  $,
  $$,
  escapeHTML,
  formatBytes,
  formatDate,
  prettyJSON,
  snapshotKindLabel,
  statusBadge,
  statusLabel,
} from "./core/dom.js";
import { app, viewMeta } from "./core/state.js";

const DEFAULT_CHARACTER_CARD_TEMPLATE = {
  version: 1,
  auto_approve: false,
  edit_requires_review: true,
  fields: [
    ["name", "角色姓名", true, false, 40],
    ["code", "副本代号", true, false, 20],
    ["appearance", "外貌特征", true, false, 300],
    ["background", "角色背景", true, false, 800],
    ["personality", "性格与行事方式", true, false, 400],
    ["goal", "当前目标", true, false, 300],
    ["belief", "核心信念", true, false, 300],
    ["bond", "重要羁绊", true, false, 300],
    ["specialties", "专长标签", true, false, 240],
    ["flaws", "缺陷标签", true, false, 200],
    ["weakness", "弱点或限制", true, false, 300],
    ["knowledge_boundary", "知识边界", true, false, 400],
    ["secret", "私人秘密", false, true, 600],
    ["content_boundaries", "个人内容边界", false, true, 600],
  ].map(([key, label, required, privateField, max_chars]) => ({
    key,
    label,
    required,
    private: privateField,
    max_chars,
  })),
  stats: {
    budget: 10,
    attributes: [
      ["body", "体魄"],
      ["agility", "敏捷"],
      ["will", "意志"],
      ["knowledge", "学识"],
    ].map(([key, label]) => ({
      key,
      label,
      minimum: 0,
      maximum: 5,
      default: 2,
    })),
    modifier_table: { 0: -3, 1: -2, 2: -1, 3: 0, 4: 1, 5: 2 },
  },
};

function validateCharacterCardTemplate(template) {
  if (!template || typeof template !== "object" || Array.isArray(template)) {
    throw new Error("玩家角色卡模板必须是 JSON 对象");
  }
  if (!Number.isInteger(Number(template.version)) || Number(template.version) < 1) {
    throw new Error("角色卡模板 version 必须是大于 0 的整数");
  }
  if (!Array.isArray(template.fields) || !template.fields.length) {
    throw new Error("角色卡模板必须包含 fields 数组");
  }
  const keys = template.fields.map((field) => String(field?.key || "").trim());
  if (keys.some((key) => !key)) throw new Error("角色卡字段 key 不能为空");
  if (new Set(keys).size !== keys.length) throw new Error("角色卡字段 key 不能重复");
  for (const required of ["name", "code"]) {
    if (!keys.includes(required)) throw new Error(`角色卡缺少必需字段 ${required}`);
  }
  const mode = String(template.stats?.mode || "manual").toLowerCase();
  if (!["none", "manual", "preset"].includes(mode)) {
    throw new Error("数值模式必须是 none、manual 或 preset");
  }
  const attributes = Array.isArray(template.stats?.attributes)
    ? template.stats.attributes
    : [];
  if (mode !== "none" && !attributes.length) {
    throw new Error("启用数值系统时必须包含 stats.attributes");
  }
  if (mode === "none") return template;
  const minimumBudget = attributes.reduce(
    (sum, item) => sum + Number(item.minimum || 0),
    0,
  );
  const maximumBudget = attributes.reduce(
    (sum, item) => sum + Number(item.maximum || 0),
    0,
  );
  const budget = Number(template.stats?.budget);
  if (!Number.isFinite(budget) || budget < minimumBudget || budget > maximumBudget) {
    throw new Error(`属性预算必须介于 ${minimumBudget} 与 ${maximumBudget} 之间`);
  }
  return template;
}

function parseJSONField(id, label) {
  const raw = $(id).value.trim();
  try {
    const parsed = JSON.parse(raw || "{}");
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      throw new Error("根节点不是对象");
    }
    return parsed;
  } catch (error) {
    throw new Error(`${label} 不是有效的 JSON 对象：${error.message}`);
  }
}

function turnPlayerStatus(player, turn) {
  const order = Array.isArray(turn?.order) ? turn.order : [];
  const position = order.findIndex((item) => item.user_id === player.user_id);
  if (position < 0) return player.enabled ? "未加入轮次" : "已停用";
  const current = turn.current_user_id === player.user_id ? " · 当前行动者" : "";
  return `第 ${position + 1} 位${current}`;
}

function turnPlayerControls(player, turn) {
  const order = Array.isArray(turn?.order) ? turn.order : [];
  const position = order.findIndex((item) => item.user_id === player.user_id);
  if (position < 0) {
    return player.enabled
      ? `<button class="action-button" data-session-detail-action="turn-add"
          data-user-id="${escapeHTML(player.user_id)}">加入顺序</button>`
      : "";
  }
  return `
    <button class="action-button" data-session-detail-action="turn-up"
      data-user-id="${escapeHTML(player.user_id)}" ${
        position === 0 ? "disabled" : ""
      }>上移</button>
    <button class="action-button" data-session-detail-action="turn-down"
      data-user-id="${escapeHTML(player.user_id)}" ${
        position === order.length - 1 ? "disabled" : ""
      }>下移</button>
    <button class="action-button is-danger" data-session-detail-action="turn-remove"
      data-user-id="${escapeHTML(player.user_id)}">移出</button>
  `;
}

const CHARACTER_CARD_STATUS_LABELS = {
  uncreated: "未建卡",
  draft: "建卡中",
  pending_review: "待审核",
  approved: "已通过",
  rejected: "未通过",
};

const PARTICIPANT_STATUS_LABELS = {
  reserved: "占位",
  active: "出场",
  standby: "候补",
  away: "暂离",
  retired: "已退场",
  archived: "已归档",
};

const CHARACTER_RUNTIME_LABELS = {
  inspiration: "当前灵感",
  inspiration_max: "灵感上限",
  statuses: "当前状态",
  equipment: "装备与携带物",
  known_clues: "已知线索",
  npc_relationships: "NPC 关系",
  temporary_traits: "临时特质",
  reputation: "声望",
  current_location: "角色位置",
};

function hasCharacterCardValue(value) {
  if (value === null || value === undefined || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

function characterCardValueHTML(value, emptyLabel = "未填写") {
  if (!hasCharacterCardValue(value)) {
    return `<span class="character-card-empty">${escapeHTML(emptyLabel)}</span>`;
  }
  if (typeof value === "boolean") {
    return `<span>${value ? "是" : "否"}</span>`;
  }
  if (typeof value === "object") {
    return `<pre>${escapeHTML(JSON.stringify(value, null, 2))}</pre>`;
  }
  return `<span>${escapeHTML(value)}</span>`;
}

function characterCardFieldClass(value) {
  const text =
    typeof value === "object" && value !== null
      ? JSON.stringify(value)
      : String(value ?? "");
  return text.length > 72 || text.includes("\n") ? " is-wide" : "";
}

function resolvedCharacterCardTemplate(detail) {
  const resolved = detail.instance_config?.character_card_template;
  if (
    resolved &&
    Array.isArray(resolved.fields) &&
    Array.isArray(resolved.stats?.attributes)
  ) {
    return resolved;
  }
  return DEFAULT_CHARACTER_CARD_TEMPLATE;
}

function characterCardProfile(item) {
  if (
    item.card_profile &&
    typeof item.card_profile === "object" &&
    Object.keys(item.card_profile).length
  ) {
    return item.card_profile;
  }
  if (item.draft_profile && typeof item.draft_profile === "object") {
    return item.draft_profile;
  }
  return {};
}

function renderCharacterCardFields(profile, template) {
  const definitions = Array.isArray(template.fields) ? template.fields : [];
  const visibleDefinitions = definitions.filter(
    (field) => !field.stat_key && !String(field.key || "").startsWith("stat_"),
  );
  const knownKeys = new Set(definitions.map((field) => String(field.key || "")));
  const extraFields = Object.entries(profile)
    .filter(
      ([key]) => !knownKeys.has(key) && !String(key).startsWith("stat_"),
    )
    .map(([key, value]) => ({
      key,
      label: key,
      required: false,
      private: false,
      value,
    }));
  const fields = [
    ...visibleDefinitions.map((field) => ({
      ...field,
      value: profile[field.key],
    })),
    ...extraFields,
  ];
  if (!fields.length) {
    return '<div class="empty-inline">当前模板没有资料字段。</div>';
  }
  return fields
    .map(
      (field) => `
        <article class="character-card-field${characterCardFieldClass(field.value)}">
          <div class="character-card-field-label">
            <span>${escapeHTML(field.label || field.key)}</span>
            ${field.required ? '<em class="is-required">必填</em>' : ""}
            ${field.private ? '<em class="is-private">私密</em>' : ""}
          </div>
          <div class="character-card-field-value">
            ${characterCardValueHTML(field.value)}
          </div>
        </article>
      `,
    )
    .join("");
}

function renderCharacterCardStats(profile, stats, template) {
  const definition = template.stats || {};
  const attributes = Array.isArray(definition.attributes)
    ? definition.attributes
    : [];
  const raw = stats?.raw && typeof stats.raw === "object" ? stats.raw : {};
  const labels =
    stats?.labels && typeof stats.labels === "object" ? stats.labels : {};
  const modifiers =
    stats?.modifiers && typeof stats.modifiers === "object"
      ? stats.modifiers
      : {};
  const table =
    definition.modifier_table && typeof definition.modifier_table === "object"
      ? definition.modifier_table
      : {};
  const values = attributes.map((attribute) => {
    const key = String(attribute.key || "");
    const profileValue = profile[`stat_${key}`];
    const value = hasCharacterCardValue(raw[key]) ? raw[key] : profileValue;
    const modifier = hasCharacterCardValue(modifiers[key])
      ? modifiers[key]
      : hasCharacterCardValue(value)
        ? table[String(value)]
        : null;
    return {
      key,
      label: labels[key] || attribute.label || key,
      value,
      modifier,
      minimum: attribute.minimum,
      maximum: attribute.maximum,
    };
  });
  const used = values.reduce(
    (sum, item) =>
      hasCharacterCardValue(item.value) && Number.isFinite(Number(item.value))
        ? sum + Number(item.value)
        : sum,
    0,
  );
  const budget = Number(stats?.budget ?? definition.budget ?? 0);
  if (!values.length) {
    return '<div class="empty-inline">当前模板没有数值属性。</div>';
  }
  return `
    <div class="character-card-budget">
      <span>属性预算</span>
      <strong>${escapeHTML(used)} / ${escapeHTML(budget)}</strong>
    </div>
    <div class="character-card-stats">
      ${values
        .map((item) => {
          const modifierNumber = Number(item.modifier);
          const modifierLabel = Number.isFinite(modifierNumber)
            ? `${modifierNumber >= 0 ? "+" : ""}${modifierNumber}`
            : "—";
          return `
            <div class="character-card-stat">
              <span>${escapeHTML(item.label)}</span>
              <strong>${
                hasCharacterCardValue(item.value)
                  ? escapeHTML(item.value)
                  : "—"
              }</strong>
              <small>修正 ${escapeHTML(modifierLabel)} · ${escapeHTML(
                item.minimum,
              )}—${escapeHTML(item.maximum)}</small>
            </div>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderCharacterRuntimeState(item) {
  const state =
    item.runtime_state && typeof item.runtime_state === "object"
      ? item.runtime_state
      : {};
  const keys = [
    ...Object.keys(CHARACTER_RUNTIME_LABELS),
    ...Object.keys(state).filter((key) => !(key in CHARACTER_RUNTIME_LABELS)),
  ];
  return keys
    .map(
      (key) => `
        <article class="character-card-field${characterCardFieldClass(state[key])}">
          <div class="character-card-field-label">
            <span>${escapeHTML(CHARACTER_RUNTIME_LABELS[key] || key)}</span>
          </div>
          <div class="character-card-field-value">
            ${characterCardValueHTML(state[key], "未记录")}
          </div>
        </article>
      `,
    )
    .join("");
}

function renderRosterCharacterCard(item, template, session, readonly, index) {
  const profile = characterCardProfile(item);
  const stats = item.card_stats || {};
  const attributes = Array.isArray(template.stats?.attributes)
    ? template.stats.attributes
    : [];
  const statPreview = attributes
    .map((attribute) => {
      const key = String(attribute.key || "");
      const value =
        stats.raw?.[key] ?? profile[`stat_${key}`] ?? attribute.default ?? "—";
      return `<span>${escapeHTML(attribute.label || key)} ${escapeHTML(value)}</span>`;
    })
    .join("");
  const cardVersion =
    item.card_version_no === null || item.card_version_no === undefined
      ? "尚未生成"
      : `v${item.card_version_no}`;
  const templateVersion =
    item.card_template_version ??
    item.draft_template_version ??
    template.version ??
    "—";
  const draftProgress =
    item.card_status === "draft" || item.card_status === "uncreated"
      ? `${Number(item.draft_step || 0)} / ${template.fields?.length || 0}`
      : "已提交";
  const disabled = readonly ? "disabled" : "";
  const actionButtons = `
    ${
      item.card_status === "approved"
        ? `<button class="action-button"
            data-session-detail-action="request-card-revision"
            data-ref="${escapeHTML(item.id)}" ${disabled}>新建修改版本</button>`
        : ""
    }
    ${
      item.card_status === "pending_review"
        ? `<button class="action-button"
            data-session-detail-action="card-approve"
            data-ref="${escapeHTML(item.id)}" ${disabled}>通过</button>
          <button class="action-button is-danger"
            data-session-detail-action="card-reject"
            data-ref="${escapeHTML(item.id)}" ${disabled}>拒绝</button>`
        : ""
    }
    ${
      item.participation_status === "active" && session.state === "running"
        ? `<button class="action-button"
            data-session-detail-action="designate"
            data-ref="${escapeHTML(item.id)}" ${disabled}>指定当前</button>`
        : ""
    }
    ${
      !["retired", "archived"].includes(item.participation_status)
        ? `<button class="action-button is-danger"
            data-session-detail-action="retire"
            data-ref="${escapeHTML(item.id)}" ${disabled}>安全退场</button>
          <button class="action-button is-danger"
            data-session-detail-action="ban"
            data-ref="${escapeHTML(item.id)}" ${disabled}>封禁</button>`
        : ""
    }
  `;
  const metaItems = [
    ["玩家昵称", item.display_name || "—"],
    ["群用户 ID", item.group_user_id || "—"],
    ["副本代号", item.character_code || "未设置"],
    ["角色卡版本", cardVersion],
    ["模板版本", `v${templateVersion}`],
    ["建卡进度", draftProgress],
    ["加入轮次", item.joined_round ?? "—"],
    ["连续超时", item.consecutive_timeouts ?? 0],
    ["运行状态修订", item.runtime_revision ? `r${item.runtime_revision}` : "—"],
    ["审核人", item.card_reviewed_by || "尚未审核"],
    ["角色卡生成", formatDate(item.card_version_created_at)],
    ["最后更新", formatDate(item.updated_at)],
  ];
  const opened = item.card_status === "pending_review" || index === 0;
  return `
    <details class="roster-character-card" ${opened ? "open" : ""}>
      <summary class="roster-character-summary">
        <div class="roster-character-identity">
          <div class="roster-character-title">
            <strong>${escapeHTML(item.character_name || item.display_name)}</strong>
            <span>${escapeHTML(item.character_code || "未设置代号")}</span>
          </div>
          <div class="roster-character-badges">
            <span class="character-card-badge status-${escapeHTML(
              item.card_status,
            )}">${escapeHTML(
              CHARACTER_CARD_STATUS_LABELS[item.card_status] || item.card_status,
            )}</span>
            <span class="character-card-badge">${escapeHTML(
              item.ready ? "已准备" : "未准备",
            )}</span>
            <span class="character-card-badge">${escapeHTML(
              PARTICIPANT_STATUS_LABELS[item.participation_status] ||
                item.participation_status,
            )}</span>
          </div>
        </div>
        <div class="roster-character-stat-preview">${statPreview}</div>
        <span class="roster-character-toggle">完整角色卡</span>
      </summary>
      <div class="roster-character-body">
        <section class="character-card-section">
          <div class="character-card-section-head">
            <div><span>CHARACTER PROFILE</span><h3>完整角色资料</h3></div>
            <small>按照本副本创建时锁定的角色卡模板展示</small>
          </div>
          <div class="character-card-fields">
            ${renderCharacterCardFields(profile, template)}
          </div>
        </section>
        <section class="character-card-section">
          <div class="character-card-section-head">
            <div><span>ATTRIBUTES</span><h3>属性与检定修正</h3></div>
            <small>审核和游戏裁定使用这里的持久化数值</small>
          </div>
          ${renderCharacterCardStats(profile, stats, template)}
        </section>
        <section class="character-card-section">
          <div class="character-card-section-head">
            <div><span>INSTANCE STATE</span><h3>当前副本动态状态</h3></div>
            <small>角色基础卡不变；这些参数会随剧情推进更新</small>
          </div>
          <div class="character-card-fields is-runtime">
            ${renderCharacterRuntimeState(item)}
          </div>
        </section>
        <section class="character-card-section">
          <div class="character-card-section-head">
            <div><span>CARD RECORD</span><h3>角色卡记录</h3></div>
            <small>审核、版本和席位信息</small>
          </div>
          <div class="character-card-records">
            ${metaItems
              .map(
                ([label, value]) => `
                  <div><span>${escapeHTML(label)}</span><strong>${escapeHTML(
                    value,
                  )}</strong></div>
                `,
              )
              .join("")}
          </div>
          ${
            item.aliases?.length
              ? `<div class="character-card-note"><strong>角色别名</strong><span>${escapeHTML(
                  item.aliases.join("、"),
                )}</span></div>`
              : ""
          }
          ${
            item.card_review_note
              ? `<div class="character-card-note"><strong>审核备注</strong><span>${escapeHTML(
                  item.card_review_note,
                )}</span></div>`
              : ""
          }
          ${
            item.exit_reason
              ? `<div class="character-card-note is-warning"><strong>退场原因</strong><span>${escapeHTML(
                  item.exit_reason,
                )}</span></div>`
              : ""
          }
        </section>
        ${
          actionButtons.trim()
            ? `<div class="roster-character-actions">${actionButtons}</div>`
            : ""
        }
      </div>
    </details>
  `;
}

function toast(message, type = "info") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.textContent = String(message);
  $("#toast-region").append(item);
  window.setTimeout(() => item.remove(), 3800);
}

async function withBusy(button, action) {
  const original = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "处理中…";
  }
  try {
    return await action();
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

function showError(error) {
  console.error(error);
  toast(error?.message || String(error), "error");
}

function splitLines(value) {
  return String(value || "")
    .split(/[\n,，]+/)
    .map((item) => item.trim())
    .filter((item, index, list) => item && list.indexOf(item) === index);
}

const DURATION_UNITS = {
  seconds: { label: "秒", multiplier: 1 },
  minutes: { label: "分钟", multiplier: 60 },
  hours: { label: "小时", multiplier: 3600 },
  days: { label: "天", multiplier: 86400 },
};

const GLOBAL_TIME_FIELDS = {
  "#gtime-card-code": "card_code_ttl_seconds",
  "#gtime-card-draft": "card_draft_ttl_seconds",
  "#gtime-card-completion": "card_completion_timeout_seconds",
  "#gtime-preparation": "preparation_timeout_seconds",
  "#gtime-ready": "ready_timeout_seconds",
  "#gtime-turn": "turn_timeout_seconds",
  "#gtime-turn-reminder": "turn_reminder_seconds",
  "#gtime-standby": "standby_timeout_seconds",
  "#gtime-delegation": "delegation_ttl_seconds",
  "#gtime-all-idle": "all_idle_pause_seconds",
  "#gtime-vote-one": "vote_round_one_seconds",
  "#gtime-vote-two": "vote_round_two_seconds",
  "#gtime-vote-reminder": "vote_reminder_seconds",
};

const SESSION_TIME_FIELDS = {
  "#t-card-code": "card_code_ttl_seconds",
  "#t-card-draft": "card_draft_ttl_seconds",
  "#t-card-completion": "card_completion_timeout_seconds",
  "#t-preparation": "preparation_timeout_seconds",
  "#t-ready": "ready_timeout_seconds",
  "#t-turn": "turn_timeout_seconds",
  "#t-turn-reminder": "turn_reminder_seconds",
  "#t-standby": "standby_timeout_seconds",
  "#t-delegation": "delegation_ttl_seconds",
  "#t-all-idle": "all_idle_pause_seconds",
  "#t-vote-one": "vote_round_one_seconds",
  "#t-vote-two": "vote_round_two_seconds",
  "#t-vote-reminder": "vote_reminder_seconds",
};

function bestDurationUnit(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return "minutes";
  if (value % 86400 === 0) return "days";
  if (value % 3600 === 0) return "hours";
  if (value % 60 === 0) return "minutes";
  return "seconds";
}

function ensureDurationControl(selector) {
  const input = $(selector);
  if (!input) return null;
  const existing = input.closest(".duration-control");
  if (existing) return existing;
  const raw = input.value.trim();
  const seconds = raw === "" || raw === "-1" ? null : Number(raw);
  const unitName = bestDurationUnit(seconds);
  const unit = DURATION_UNITS[unitName];
  const wrapper = document.createElement("div");
  wrapper.className = "duration-control";
  wrapper.innerHTML = `
    <select class="duration-unit" aria-label="时间单位">
      ${Object.entries(DURATION_UNITS)
        .map(
          ([key, item]) =>
            `<option value="${key}" ${key === unitName ? "selected" : ""}>${item.label}</option>`,
        )
        .join("")}
    </select>
    <label class="duration-unlimited">
      <input type="checkbox" ${seconds === null ? "checked" : ""} />
      <span>不限时</span>
    </label>
  `;
  input.replaceWith(wrapper);
  input.min = "1";
  input.step = "1";
  input.value = seconds === null ? "" : String(seconds / unit.multiplier);
  wrapper.prepend(input);
  const unlimited = wrapper.querySelector(".duration-unlimited input");
  const refresh = () => {
    input.disabled = unlimited.checked;
    wrapper.querySelector(".duration-unit").disabled = unlimited.checked;
  };
  unlimited.addEventListener("change", refresh);
  refresh();
  return wrapper;
}

function setTimeValue(selector, seconds) {
  const control = ensureDurationControl(selector);
  if (!control) return;
  const input = control.querySelector('input[type="number"]');
  const select = control.querySelector(".duration-unit");
  const unlimited = control.querySelector(".duration-unlimited input");
  const normalized =
    seconds === null || seconds === undefined || Number(seconds) === -1
      ? null
      : Number(seconds);
  const unitName = bestDurationUnit(normalized);
  select.value = unitName;
  unlimited.checked = normalized === null;
  input.value =
    normalized === null
      ? ""
      : String(normalized / DURATION_UNITS[unitName].multiplier);
  input.disabled = unlimited.checked;
  select.disabled = unlimited.checked;
}

function readTimeValue(selector) {
  const control = ensureDurationControl(selector);
  if (!control) return null;
  if (control.querySelector(".duration-unlimited input").checked) return null;
  const input = control.querySelector('input[type="number"]');
  const value = Number(input.value);
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error("时间数值必须大于 0，或勾选不限时");
  }
  const unitName = control.querySelector(".duration-unit").value;
  return Math.round(value * DURATION_UNITS[unitName].multiplier);
}

function applyTimePreset(name) {
  const presets = {
    instant: {
      card_code_ttl_seconds: 900,
      card_draft_ttl_seconds: 86400,
      card_completion_timeout_seconds: 3600,
      preparation_timeout_seconds: 7200,
      ready_timeout_seconds: 600,
      turn_timeout_seconds: 180,
      turn_reminder_seconds: 60,
      standby_timeout_seconds: 86400,
      delegation_ttl_seconds: 7200,
      all_idle_pause_seconds: 600,
      vote_round_one_seconds: 180,
      vote_round_two_seconds: 120,
      vote_reminder_seconds: 60,
      max_consecutive_timeouts: 2,
    },
    slow: {
      card_code_ttl_seconds: 1800,
      card_draft_ttl_seconds: 604800,
      card_completion_timeout_seconds: 86400,
      preparation_timeout_seconds: 86400,
      ready_timeout_seconds: 1800,
      turn_timeout_seconds: 600,
      turn_reminder_seconds: 180,
      standby_timeout_seconds: 604800,
      delegation_ttl_seconds: 86400,
      all_idle_pause_seconds: 600,
      vote_round_one_seconds: 600,
      vote_round_two_seconds: 300,
      vote_reminder_seconds: 120,
      max_consecutive_timeouts: 2,
    },
    manual: Object.fromEntries(
      Object.values(GLOBAL_TIME_FIELDS).map((key) => [key, null]),
    ),
  };
  presets.manual.max_consecutive_timeouts = -1;
  const preset = presets[name];
  if (!preset) return;
  Object.entries(GLOBAL_TIME_FIELDS).forEach(([selector, key]) =>
    setTimeValue(selector, preset[key]),
  );
  $("#gtime-timeout-count").value = preset.max_consecutive_timeouts;
  $("#settings-dirty-text").textContent = "存在尚未保存的更改";
  toast(
    {
      instant: "已应用即时团时间预设",
      slow: "已应用慢速群团时间预设",
      manual: "已应用全手动推进预设",
    }[name],
    "success",
  );
}

function providerOptionHTML(selectedId, emptyLabel) {
  const known = new Set(app.providers.map((item) => item.id));
  const options = [
    `<option value="">${escapeHTML(emptyLabel)}</option>`,
    ...app.providers.map((item) => {
      const detail = [item.name, item.model].filter(Boolean).join(" · ");
      return `<option value="${escapeHTML(item.id)}" ${
        item.id === selectedId ? "selected" : ""
      }>${escapeHTML(detail || item.id)}（${escapeHTML(item.id)}）</option>`;
    }),
  ];
  if (selectedId && !known.has(selectedId)) {
    options.push(
      `<option value="${escapeHTML(selectedId)}" selected>${escapeHTML(
        selectedId,
      )}（当前未加载）</option>`,
    );
  }
  return options.join("");
}

function renderFallbackProviders(values = null) {
  const configured = Array.isArray(values)
    ? values
    : [...$("#fallback-provider-list").querySelectorAll("select")].map(
        (item) => item.value,
      );
  const root = $("#fallback-provider-list");
  root.innerHTML = configured.length
    ? configured
        .map(
          (providerId, index) => `
            <div class="provider-fallback-row">
              <span class="provider-order">${index + 1}</span>
              <select data-fallback-provider>
                ${providerOptionHTML(providerId, "请选择备用模型")}
              </select>
              <div class="table-actions">
                <button type="button" class="action-button"
                  data-fallback-action="up" data-index="${index}" ${
                    index === 0 ? "disabled" : ""
                  }>上移</button>
                <button type="button" class="action-button"
                  data-fallback-action="down" data-index="${index}" ${
                    index === configured.length - 1 ? "disabled" : ""
                  }>下移</button>
                <button type="button" class="action-button is-danger"
                  data-fallback-action="remove" data-index="${index}">移除</button>
              </div>
            </div>
          `,
        )
        .join("")
    : '<div class="empty-inline">未设置备用模型；主模型失败时本轮会停止。</div>';
}

function applyBridgeContext(context) {
  const value = context || {};
  document.documentElement.dataset.theme = value.isDark ? "dark" : "light";
  if (value.locale) {
    document.documentElement.lang = value.locale;
  }
  document.title = bridge.t("pages.console.title", "酒馆控制台");
}

function switchView(name) {
  if (!viewMeta[name]) return;
  app.view = name;
  $$(".nav-item").forEach((item) => {
    item.classList.toggle("is-active", item.dataset.view === name);
  });
  $$(".view").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.viewPanel === name);
  });
  const [kicker, title] = viewMeta[name];
  $("#page-kicker").textContent = kicker;
  $("#page-title").textContent = title;
  $("#sidebar").classList.remove("is-open");
  if (name === "memories") loadMemories().catch(showError);
  if (name === "audit") loadAudit().catch(showError);
}

function setConnection(state, text) {
  const pill = $("#live-pill");
  pill.classList.toggle("is-live", state === "live");
  pill.classList.toggle("is-error", state === "error");
  $("#live-text").textContent = text;
  $("#side-health").textContent = text;
  const dot = $("#side-health-dot");
  dot.classList.toggle("is-ok", state === "live");
  dot.classList.toggle("is-error", state === "error");
}

function applySessionResponse(sessions) {
  app.sessions = sessions.items || [];
  app.sessionOptions = sessions.options || app.sessionOptions || [];
  app.sessionGroups = sessions.groups || [];
  app.sessionTotal = Number(sessions.total || 0);
  app.sessionPages = Math.max(1, Number(sessions.pages || 1));
  app.sessionPage = Math.min(
    app.sessionPages,
    Math.max(1, Number(sessions.page || app.sessionPage)),
  );
}

async function loadSessionPage() {
  const sessions = await bridge.apiGet("sessions", {
    q: app.sessionQuery,
    scope: app.sessionScope,
    page: app.sessionPage,
    page_size: app.sessionPageSize,
  });
  applySessionResponse(sessions);
  renderSessions();
  renderSessionSelects();
}

async function loadCore() {
  const [overview, worlds, sessions, settings, providers] = await Promise.all([
    bridge.apiGet("overview"),
    bridge.apiGet("worlds", {
      include_archived: $("#show-archived-worlds").checked,
    }),
    bridge.apiGet("sessions", {
      q: app.sessionQuery,
      scope: app.sessionScope,
      page: app.sessionPage,
      page_size: app.sessionPageSize,
    }),
    bridge.apiGet("settings"),
    bridge.apiGet("providers"),
  ]);
  app.overview = overview;
  app.worlds = worlds.items || [];
  applySessionResponse(sessions);
  app.settings = settings.settings;
  app.configState = settings.config_state || null;
  app.providerHealth = settings.provider_health || [];
  app.providers = providers.items || [];
  renderOverview();
  renderWorlds();
  renderSessions();
  renderSessionSelects();
  renderSettings();
  setConnection("live", "运行正常");
}

function renderOverview() {
  const data = app.overview;
  if (!data) return;
  const counts = data.counts || {};
  const cards = [
    ["运行会话", counts.running || 0, `${counts.sessions || 0} 个已建会话`, "◉"],
    ["世界包", counts.worlds || 0, "可加载世界", "◇"],
    ["长期记忆", counts.memories || 0, "跨回合事实", "≋"],
    ["安全存档", counts.snapshots || 0, formatBytes(data.database_size), "⌁"],
  ];
  $("#metrics").innerHTML = cards
    .map(
      ([label, value, note, icon]) => `
        <article class="metric-card">
          <div class="metric-icon">${icon}</div>
          <div class="metric-label">${escapeHTML(label)}</div>
          <div class="metric-value">${escapeHTML(value)}</div>
          <div class="metric-note">${escapeHTML(note)}</div>
        </article>
      `,
    )
    .join("");

  const sessions = data.sessions || [];
  const active = sessions.filter((item) => item.state !== "closed").slice(0, 5);
  $("#dashboard-sessions").innerHTML = active.length
    ? active
        .map(
          (session) => `
            <button class="session-row" data-open-session="${escapeHTML(session.id)}">
              <div>
                <div class="session-name">${escapeHTML(
                  session.instance_name || session.world_name,
                )}</div>
                <div class="session-meta">${escapeHTML(session.platform_id)} · 群 ${escapeHTML(
                  session.group_id,
                )} · ${escapeHTML(session.world_name)} · 第 ${escapeHTML(
                  session.turn_no,
                )} 回合</div>
              </div>
              <div class="session-location">${escapeHTML(
                session.world_state?.location || "地点未记录",
              )}</div>
              ${statusBadge(session.state)}
            </button>
          `,
        )
        .join("")
    : `
      <div class="empty-state compact">
        <div class="empty-symbol">◌</div>
        <span>当前没有运行中的群会话</span>
      </div>
    `;

  const security = data.security || {};
  const whitelistRequired = security.whitelist_required !== false;
  const groupBoundaryReady =
    !whitelistRequired || security.allowed_group_count > 0;
  const integrity = [
    [
      data.database_ok,
      "SQLite 完整性",
      data.database_ok ? "快速校验通过" : "需要检查数据库",
    ],
    [
      security.admin_count > 0,
      "管理员身份",
      `${security.admin_count || 0} 个授权 ID`,
    ],
    [
      groupBoundaryReady,
      "群聊边界",
      !whitelistRequired
        ? "白名单未启用"
        : `${security.allowed_group_count || 0} 个允许群`,
    ],
    [true, "Schema 版本", `v${data.schema_version} · 插件 ${data.plugin_version}`],
  ];
  $("#integrity-list").innerHTML = integrity
    .map(
      ([ok, title, note]) => `
        <div class="integrity-item">
          <span class="integrity-icon ${ok ? "ok" : "warn"}">${ok ? "✓" : "!"}</span>
          <div class="integrity-body">
            <div class="integrity-title">${escapeHTML(title)}</div>
            <div class="integrity-note">${escapeHTML(note)}</div>
          </div>
        </div>
      `,
    )
    .join("");

  const checklist = [
    [security.admin_count > 0, "设置真实管理员 ID", "群内权限不依赖昵称或文本声明"],
    [
      groupBoundaryReady,
      !whitelistRequired
        ? "群白名单已关闭"
        : security.allowed_group_count > 0
          ? "已绑定允许群"
          : "等待绑定首个群",
      !whitelistRequired
        ? "所有群都可进入会话，管理命令仍仅限授权 ID"
        : security.allowed_group_count > 0
          ? "仅允许列表中的群进入酒馆"
          : "授权管理员发送 /酒馆 开启 后自动绑定并选择副本",
    ],
    [counts.worlds > 0, "准备至少一个世界包", "默认已附带阿尔维恩：灰烬王冠"],
    [
      true,
      "设置叙事模型链",
      `${app.settings?.model?.fallback_provider_ids?.length || 0} 个备用模型 · ${
        app.settings?.model?.image_caption_provider_id
          ? "图片转述已配置"
          : "图片转述未配置"
      }`,
    ],
  ];
  $("#readiness-checklist").innerHTML = checklist
    .map(
      ([ok, title, note]) => `
        <div class="check-item">
          <span class="check-icon ${ok ? "ok" : "warn"}">${ok ? "✓" : "○"}</span>
          <div class="check-body">
            <div class="check-title">${escapeHTML(title)}</div>
            <div class="check-note">${escapeHTML(note)}</div>
          </div>
        </div>
      `,
    )
    .join("");

  $("#security-notice").classList.toggle("is-hidden", security.ready);
  if (!security.ready) {
    const missing = [];
    if (!security.admin_count) missing.push("管理员 ID");
    $("#security-notice-text").textContent =
      `仍缺少：${missing.join("、")}。填写后，由授权管理员在目标群发送 /酒馆 开启即可绑定并选择副本。`;
  }
}

function renderWorlds() {
  const root = $("#world-grid");
  if (!app.worlds.length) {
    root.innerHTML = `
      <div class="empty-state">
        <div class="empty-symbol">◇</div>
        <strong>还没有世界包</strong>
        <span>创建一个世界后才能建立群会话。</span>
      </div>
    `;
    return;
  }
  root.innerHTML = app.worlds
    .map(
      (world, index) => `
        <article class="world-card ${world.archived ? "is-archived" : ""}">
          <div class="world-card-top">
            <span class="world-number">WORLD ${String(index + 1).padStart(2, "0")}</span>
            ${world.archived ? '<span class="status-badge status-closed">已归档</span>' : ""}
          </div>
          <h3>${escapeHTML(world.name)}</h3>
          <div class="table-subtitle">${escapeHTML(world.slug)}</div>
          <p class="world-description">${escapeHTML(world.description || "尚未填写世界简介。")}</p>
          <div class="world-stats">
            <div class="world-stat">
              <strong>${escapeHTML(
                `${world.player_limits?.recommended_min || 1}—${
                  world.player_limits?.recommended_max || 4
                }`,
              )}</strong>
              <span>推荐人数</span>
            </div>
            <div class="world-stat">
              <strong>${escapeHTML(world.player_limits?.maximum || 4)}</strong>
              <span>强制上限</span>
            </div>
            <div class="world-stat">
              <strong>${world.choice_mode === "strict_abcd" ? "A—D" : "自由"}</strong>
              <span>行动模式</span>
            </div>
          </div>
          <div class="world-actions">
            <button class="action-button" data-world-action="edit" data-id="${escapeHTML(
              world.id,
            )}">编辑设定</button>
            <button class="action-button" data-world-action="characters" data-id="${escapeHTML(
              world.id,
            )}">角色管理</button>
            <button class="action-button" data-world-action="card-template" data-id="${escapeHTML(
              world.id,
            )}">角色卡模板</button>
            <button class="action-button" data-world-action="preflight" data-id="${escapeHTML(
              world.id,
            )}">兼容性体检</button>
            ${
              world.archived
                ? `<button class="action-button" data-world-action="restore" data-id="${escapeHTML(
                    world.id,
                  )}">恢复世界</button>`
                : `<button class="action-button is-danger" data-world-action="archive" data-id="${escapeHTML(
                    world.id,
                  )}">归档</button>`
            }
          </div>
        </article>
      `,
    )
    .join("");
}

function sessionActions(session) {
  if (session.state === "finished" || session.readonly) {
    return [
      ["clone", "克隆续作"],
      ["detail", "查看归档"],
    ];
  }
  const actions = [];
  if (session.state === "running") {
    actions.push(["pause", "暂停"]);
  } else if (session.state === "preparing") {
    actions.push(["perform", session.turn_no ? "继续故事" : "正式开演"]);
  } else if (session.state === "paused" || session.state === "maintenance") {
    actions.push(["resume", "恢复准备"]);
  } else {
    actions.push(["start", "进入准备"]);
  }
  if (session.state !== "closed") actions.push(["close", "关闭"]);
  if (session.state !== "closed") {
    actions.push(["finish", "完结"]);
    actions.push(["abort", "强制终止"]);
  }
  actions.push(["clone", "克隆分支"]);
  actions.push(["detail", "详情"]);
  return actions;
}

function sessionCardMarkup(session) {
  const progress = session.progress || {};
  const progressValue =
    session.progress_percent === null ||
    session.progress_percent === undefined
      ? null
      : Number(session.progress_percent);
  const waitingLabel =
    {
      vote: "等待集体投票",
      choice: "等待个人选择",
      preparation: "等待建卡与准备",
      admin: "等待管理员处理",
    }[session.waiting_for] || "暂无等待流程";
  const recovery = session.recovery || {};
  const termination =
    session.archive?.termination_type === "aborted"
      ? `强制终止：${session.archive?.reason || "未填写原因"}`
      : session.archive
        ? "故事正常完结"
        : "";
  const storageError =
    session.storage_sync_status === "error"
      ? `副本文件同步异常：${session.storage_last_error || "等待自动重试"}`
      : "";
  return `
    <article class="session-card ${session.readonly ? "is-readonly" : ""}">
      <header class="session-card-head">
        <div>
          <div class="eyebrow">PLAYTHROUGH ${escapeHTML(
            session.playthrough_no || 1,
          )} · ${escapeHTML(formatDate(session.created_at))}</div>
          <h2>${escapeHTML(session.instance_name || session.world_name)}</h2>
          <p>${escapeHTML(session.world_name)} · ${escapeHTML(
            session.instance_slug || session.world_slug,
          )}${session.selected ? " · 当前绑定" : ""}</p>
        </div>
        ${statusBadge(session.state)}
      </header>
      <div class="session-card-scene">
        <strong>${escapeHTML(session.world_state?.location || "地点未记录")}</strong>
        <span>${escapeHTML(
          session.world_state?.scene_summary || "尚无场景摘要",
        )}</span>
      </div>
      <div class="session-card-progress">
        <div>
          <span>${escapeHTML(progress.chapter || "章节未设置")}</span>
          <strong>${escapeHTML(
            progress.current_objective || "当前目标未设置",
          )}</strong>
        </div>
        ${
          progressValue === null
            ? '<span class="progress-note">未设置里程碑，不显示虚构百分比</span>'
            : `<div class="progress-meter" aria-label="剧情进度 ${progressValue}%">
                <i style="width:${Math.max(0, Math.min(100, progressValue))}%"></i>
              </div><span class="progress-note">${progressValue}% · ${escapeHTML(
                progress.completed_milestones || 0,
              )}/${escapeHTML(progress.total_milestones || 0)} 里程碑</span>`
        }
      </div>
      <div class="session-card-stats">
        <span>剧情 ${escapeHTML(session.turn_no)} 回合</span>
        <span>${escapeHTML(session.player_count || 0)} 人 · ${escapeHTML(
          session.ready_count || 0,
        )} 已准备</span>
        <span>${escapeHTML(session.npc_count || 0)} NPC</span>
        <span>${escapeHTML(session.memory_count || 0)} 记忆</span>
        <span>${escapeHTML(session.snapshot_count || 0)} 快速恢复点</span>
      </div>
      <div class="session-card-waiting">
        <span>${escapeHTML(waitingLabel)}</span>
        <span>${escapeHTML(
          session.active_deadline_at
            ? `截止 ${formatDate(session.active_deadline_at)}`
            : "不限时或未启动计时",
        )}</span>
      </div>
      ${
        termination || recovery.state === "error" || storageError
          ? `<div class="session-card-alert">${escapeHTML(
              termination ||
                storageError ||
                recovery.message ||
                "等待管理员恢复",
            )}</div>`
          : ""
      }
      <footer class="session-card-actions">
        <span>更新于 ${escapeHTML(formatDate(session.updated_at))}</span>
        <div class="table-actions">
          ${sessionActions(session)
            .map(
              ([action, label]) => `
                <button class="action-button ${
                  ["close", "abort"].includes(action) ? "is-danger" : ""
                }" data-session-action="${action}" data-id="${escapeHTML(
                  session.id,
                )}">${label}</button>
              `,
            )
            .join("")}
        </div>
      </footer>
    </article>
  `;
}

function renderSessions() {
  const body = $("#session-card-grid");
  $("#session-result-count").textContent = app.sessionQuery
    ? `检索到 ${app.sessionTotal} 个故事副本`
    : `共 ${app.sessionTotal} 个故事副本`;
  $("#session-page-label").textContent = `${app.sessionPage} / ${app.sessionPages}`;
  $("#session-page-prev").disabled = app.sessionPage <= 1;
  $("#session-page-next").disabled = app.sessionPage >= app.sessionPages;
  if (!app.sessions.length) {
    body.innerHTML = `
      <div class="empty-state compact">
        <div class="empty-symbol">◉</div>
        <span>${app.sessionQuery ? "没有匹配的群或故事副本" : "尚未建立群会话"}</span>
      </div>
    `;
    return;
  }
  const metaByGroup = new Map(
    app.sessionGroups.map((item) => [
      `${item.platform_id}\0${item.group_id}`,
      item,
    ]),
  );
  const grouped = new Map();
  for (const session of app.sessions) {
    const key = `${session.platform_id}\0${session.group_id}`;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(session);
  }
  body.innerHTML = [...grouped.entries()]
    .map(([key, sessions]) => {
      const meta = metaByGroup.get(key) || {};
      const sample = sessions[0];
      const remark = meta.remark || sample.group_remark || "";
      const displayName = remark || `群 ${sample.group_id}`;
      return `
        <section class="session-group-block">
          <header class="session-group-head">
            <div>
              <div class="eyebrow">${escapeHTML(sample.platform_id)} · GROUP</div>
              <h2>${escapeHTML(displayName)}</h2>
              <p>群 ID：${escapeHTML(sample.group_id)} · 共 ${escapeHTML(
                meta.story_count || sessions.length,
              )} 个故事副本${
                Number(meta.running_count || 0)
                  ? ` · ${escapeHTML(meta.running_count)} 个运行中`
                  : ""
              }</p>
            </div>
            <div class="table-actions">
              <button class="action-button" data-group-action="token-quota"
                data-platform-id="${escapeHTML(sample.platform_id)}"
                data-group-id="${escapeHTML(sample.group_id)}"
                data-group-name="${escapeHTML(displayName)}">群 Token 限额</button>
              <button class="action-button" data-group-action="remark"
                data-platform-id="${escapeHTML(sample.platform_id)}"
                data-group-id="${escapeHTML(sample.group_id)}"
                data-group-remark="${escapeHTML(remark)}"
                data-group-revision="${escapeHTML(
                  meta.revision || sample.group_revision || 1,
                )}">编辑群备注</button>
            </div>
          </header>
          <div class="session-card-grid">${sessions
            .map(sessionCardMarkup)
            .join("")}</div>
        </section>
      `;
    })
    .join("");
}

function renderSessionSelects() {
  const options = [
    '<option value="">选择会话</option>',
    ...app.sessionOptions.map(
      (session) =>
        `<option value="${escapeHTML(session.id)}">${escapeHTML(
          session.group_remark || `群 ${session.group_id}`,
        )} · ${escapeHTML(
          session.instance_name || session.world_name,
        )}</option>`,
    ),
  ].join("");
  const auditOptions = [
    '<option value="">全部会话</option>',
    ...app.sessionOptions.map(
      (session) =>
        `<option value="${escapeHTML(session.id)}">${escapeHTML(
          session.group_remark || `群 ${session.group_id}`,
        )} · ${escapeHTML(
          session.instance_name || session.world_name,
        )}</option>`,
    ),
  ].join("");
  const memoryValue = $("#memory-session-select").value;
  const auditValue = $("#audit-session-select").value;
  $("#memory-session-select").innerHTML = options;
  $("#audit-session-select").innerHTML = auditOptions;
  if (app.sessionOptions.some((item) => item.id === memoryValue)) {
    $("#memory-session-select").value = memoryValue;
  } else if (app.sessionOptions[0]) {
    $("#memory-session-select").value = app.sessionOptions[0].id;
  }
  $("#audit-session-select").value = app.sessionOptions.some(
    (item) => item.id === auditValue,
  )
    ? auditValue
    : "";
}

function openEditor({ title, kicker = "EDITOR", body, saveLabel = "保存", onSave }) {
  $("#editor-modal-title").textContent = title;
  $("#editor-modal-kicker").textContent = kicker;
  $("#editor-modal-body").innerHTML = body;
  $("#editor-save-button").textContent = saveLabel;
  $("#editor-modal-foot").classList.toggle("is-hidden", !onSave);
  app.editorSave = onSave || null;
  if (!$("#editor-modal").open) {
    $("#editor-modal").showModal();
  }
}

function openWorldEditor(world = null) {
  const item =
    world ||
    {
      slug: "",
      name: "",
      description: "",
      system_prompt: "",
      opening_scene: "",
      rules: {
        resolution: "d20",
        default_difficulty: 12,
        difficulty_min: 5,
        difficulty_max: 25,
        allow_player_result_claims: false,
        strict_choices: true,
        player_limits: {
          recommended_min: 2,
          recommended_max: 4,
          minimum_start: 2,
          maximum: 4,
        },
      },
      initial_state: {
        location: "",
        time: "",
        scene_summary: "",
        facts: [],
        inventory: {},
        relationships: {},
      },
    };
  const rulesForEditor = { ...(item.rules || {}) };
  const characterCardTemplate =
    item.card_template ||
    rulesForEditor.character_card ||
    structuredClone(DEFAULT_CHARACTER_CARD_TEMPLATE);
  delete rulesForEditor.character_card;
  openEditor({
    title: world ? `编辑「${world.name}」` : "新建世界包",
    kicker: "WORLD DEFINITION",
    body: `
      <div class="form-grid">
        <div class="field">
          <label for="world-name">世界名称</label>
          <input id="world-name" value="${escapeHTML(item.name)}" maxlength="400" />
        </div>
        <div class="field">
          <label for="world-slug">唯一标识</label>
          <input id="world-slug" value="${escapeHTML(item.slug)}" maxlength="64"
            placeholder="lowercase-slug" />
        </div>
        <div class="field field-span-2">
          <label for="world-description">简介</label>
          <textarea id="world-description" rows="3">${escapeHTML(
            item.description,
          )}</textarea>
        </div>
        <div class="field">
          <label for="world-recommended-min">推荐最少人数</label>
          <input id="world-recommended-min" type="number" min="1" max="32"
            value="${escapeHTML(
              item.player_limits?.recommended_min ||
                item.rules?.player_limits?.recommended_min ||
                2,
            )}" />
        </div>
        <div class="field">
          <label for="world-recommended-max">推荐最多人数</label>
          <input id="world-recommended-max" type="number" min="1" max="32"
            value="${escapeHTML(
              item.player_limits?.recommended_max ||
                item.rules?.player_limits?.recommended_max ||
                4,
            )}" />
        </div>
        <div class="field">
          <label for="world-minimum-start">最低开演人数</label>
          <input id="world-minimum-start" type="number" min="1" max="32"
            value="${escapeHTML(
              item.player_limits?.minimum_start ||
                item.rules?.player_limits?.minimum_start ||
                2,
            )}" />
        </div>
        <div class="field">
          <label for="world-maximum">强制人数上限</label>
          <input id="world-maximum" type="number" min="1" max="32"
            value="${escapeHTML(
              item.player_limits?.maximum || item.rules?.player_limits?.maximum || 4,
            )}" />
        </div>
        <label class="switch-field field-span-2">
          <input id="world-strict-choices" type="checkbox" ${
            item.rules?.strict_choices !== false ? "checked" : ""
          } />
          <span><strong>严格 A/B/C/D 回合</strong>
            <small>关键集体决定自动转为多数表决</small></span>
        </label>
        <div class="field">
          <label for="world-stats-mode">角色数值模式</label>
          <select id="world-stats-mode">
            ${[["none", "无数值"], ["manual", "手动分配"], ["preset", "职业预设"]].map(([value, label]) => `<option value="${value}" ${String(characterCardTemplate.stats?.mode || "manual") === value ? "selected" : ""}>${label}</option>`).join("")}
          </select>
        </div>
        <div class="field">
          <label for="world-resolution-mode">裁定模式</label>
          <select id="world-resolution-mode">
            ${[["none", "不裁定"], ["narrative", "叙事裁定"], ["dice_only", "纯骰检定"], ["attribute", "属性检定"]].map(([value, label]) => `<option value="${value}" ${String(item.rules?.resolution?.mode || (typeof item.rules?.resolution === "string" ? "attribute" : "none")) === value ? "selected" : ""}>${label}</option>`).join("")}
          </select>
        </div>
        <div class="field field-span-2">
          <label for="world-dice-system">骰制</label>
          <select id="world-dice-system"><option value="d20" ${String(item.rules?.resolution?.dice_system || "d20") === "d20" ? "selected" : ""}>D20</option><option value="none" ${String(item.rules?.resolution?.dice_system || "") === "none" ? "selected" : ""}>无骰制</option></select>
          <small>可视化字段会写回纯 JSON；复杂属性、职业和事件仍可在下方源码区精确调整。</small>
        </div>
        <div class="field field-span-2">
          <label for="world-system-prompt">核心世界设定</label>
          <textarea id="world-system-prompt" rows="9">${escapeHTML(
            item.system_prompt,
          )}</textarea>
          <small>这里只写稳定、不可随剧情轻易改变的世界规律。</small>
        </div>
        <div class="field field-span-2">
          <label for="world-opening">开场场景</label>
          <textarea id="world-opening" rows="5">${escapeHTML(
            item.opening_scene,
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <label for="world-rules">裁定规则 JSON</label>
          <textarea id="world-rules" class="code-field">${escapeHTML(
            prettyJSON(rulesForEditor),
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <div class="field-label-row">
            <label for="world-character-card">玩家角色卡模板 JSON</label>
            <div class="table-actions">
              <button type="button" class="action-button" id="preview-character-card">
                预览建卡流程
              </button>
              <button type="button" class="action-button" id="restore-character-card">
                恢复默认模板
              </button>
            </div>
          </div>
          <textarea id="world-character-card" class="code-field" rows="18">${escapeHTML(
            prettyJSON(characterCardTemplate),
          )}</textarea>
          <small>独立校验模板版本、重复字段、name/code 必需字段、属性预算与范围；常驻 NPC 不在这里管理。</small>
          <div class="template-preview" id="world-character-card-preview" hidden></div>
        </div>
        <div class="field field-span-2">
          <label for="world-state">初始世界状态 JSON</label>
          <textarea id="world-state" class="code-field">${escapeHTML(
            prettyJSON(item.initial_state),
          )}</textarea>
        </div>
      </div>
    `,
    onSave: async () => {
      const rules = parseJSONField("#world-rules", "裁定规则");
      const characterCard = parseJSONField("#world-character-card", "玩家角色卡模板");
      characterCard.stats = characterCard.stats || {};
      characterCard.stats.mode = $("#world-stats-mode").value;
      if (characterCard.stats.mode === "none") {
        characterCard.stats.attributes = [];
        characterCard.stats.modifier_table = {};
        characterCard.stats.budget = 0;
      }
      rules.character_card = validateCharacterCardTemplate(characterCard);
      rules.resolution = {
        ...(typeof rules.resolution === "object" ? rules.resolution : {}),
        mode: $("#world-resolution-mode").value,
        dice_system: $("#world-dice-system").value,
        unknown_attribute: "reject",
      };
      rules.strict_choices = $("#world-strict-choices").checked;
      rules.player_limits = {
        recommended_min: Number($("#world-recommended-min").value),
        recommended_max: Number($("#world-recommended-max").value),
        minimum_start: Number($("#world-minimum-start").value),
        maximum: Number($("#world-maximum").value),
      };
      const payload = {
        id: item.id,
        revision: item.revision,
        name: $("#world-name").value,
        slug: $("#world-slug").value,
        description: $("#world-description").value,
        system_prompt: $("#world-system-prompt").value,
        opening_scene: $("#world-opening").value,
        rules,
        initial_state: parseJSONField("#world-state", "初始世界状态"),
        archived: Boolean(item.archived),
        world_schema_version: 2,
        capabilities: {
          character_stats: characterCard.stats.mode !== "none",
          attribute_checks: rules.resolution.mode === "attribute",
          dice_resolution: ["dice_only", "attribute"].includes(rules.resolution.mode),
        },
      };
      await bridge.apiPost("worlds/save", payload);
      toast(world ? "世界包已更新" : "世界包已创建", "success");
      await loadCore();
    },
  });
  $("#restore-character-card").addEventListener("click", () => {
    $("#world-character-card").value = prettyJSON(DEFAULT_CHARACTER_CARD_TEMPLATE);
    toast("已恢复默认角色卡模板；保存世界后生效", "success");
  });
  $("#preview-character-card").addEventListener("click", () => {
    // 预览必须自带异常处理：模板缺少 fields / JSON 非法时会抛错，
    // 之前既没有 try/catch 也没有页面内反馈，表现为“点了没反应”。
    const panel = $("#world-character-card-preview");
    try {
      const template = validateCharacterCardTemplate(
        parseJSONField("#world-character-card", "玩家角色卡模板"),
      );
      // 不再使用 window.alert：控制台以 iframe 形式嵌在 AstrBot 面板中，
      // 浏览器会静默拦截跨源 iframe 里的模态框，导致点击毫无反馈。
      panel.innerHTML = renderCharacterCardTemplatePreview(template);
      panel.hidden = false;
      toast(
        `模板 v${template.version} 校验通过 · ${template.fields.length} 个字段`,
        "success",
      );
    } catch (error) {
      panel.innerHTML = `<p class="template-preview-error">${escapeHTML(
        error?.message || String(error),
      )}</p>`;
      panel.hidden = false;
      showError(error);
    }
  });
}

function renderCharacterCardTemplatePreview(template) {
  const fields = template.fields
    .map(
      (field, index) => `
        <div class="template-preview-field">
          <span>${index + 1}</span>
          <div>
            <strong>${escapeHTML(field.label || field.key)}</strong>
            <small>${escapeHTML(field.key)} · ${
              field.required ? "必填" : "选填"
            }${field.private ? " · 私密" : ""} · 最多 ${escapeHTML(
              field.max_chars || "—",
            )} 字</small>
          </div>
        </div>
      `,
    )
    .join("");
  const attributes = template.stats.attributes
    .map(
      (item) =>
        `<span>${escapeHTML(item.label || item.key)} ${escapeHTML(
          item.minimum,
        )}—${escapeHTML(item.maximum)}</span>`,
    )
    .join("");
  return `
    <div class="template-preview-head">
      <strong>模板 v${escapeHTML(template.version)}</strong>
      <span>${escapeHTML(template.fields.length)} 个字段 · 属性预算 ${escapeHTML(
        template.stats.budget,
      )}</span>
    </div>
    <div class="tag-row">${attributes}</div>
    <div class="template-preview-fields">${fields}</div>
  `;
}

function downloadJSON(filename, payload) {
  const blob = new Blob([`${prettyJSON(payload)}\n`], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function openCharacterCardTemplateManager(world) {
  const template =
    world.card_template ||
    world.rules?.character_card ||
    structuredClone(DEFAULT_CHARACTER_CARD_TEMPLATE);
  openEditor({
    title: `${world.name} · 角色卡模板`,
    kicker: "CHARACTER CARD TEMPLATE",
    body: `
      <div class="template-manager">
        <div class="section-toolbar template-toolbar">
          <p>导入只读取本地 JSON；通过结构校验并确认保存后才会影响以后新建的角色卡。</p>
          <div class="toolbar-actions">
            <input id="character-template-file" type="file"
              accept=".json,application/json" class="sr-only" />
            <button type="button" class="action-button"
              id="character-template-import">导入 JSON</button>
            <button type="button" class="action-button"
              id="character-template-export">导出 JSON</button>
            <button type="button" class="action-button"
              id="character-template-default">恢复默认</button>
            <button type="button" class="action-button"
              id="character-template-preview-button">预览表单</button>
          </div>
        </div>
        <div class="field">
          <label for="character-template-json">玩家角色卡模板 JSON</label>
          <textarea id="character-template-json" class="code-field"
            rows="20">${escapeHTML(prettyJSON(template))}</textarea>
          <small id="character-template-status">尚未保存；导入和编辑不会立即生效。</small>
        </div>
        <div class="template-preview" id="character-template-preview"></div>
      </div>
    `,
    saveLabel: "验证并保存模板",
    onSave: async () => {
      const nextTemplate = validateCharacterCardTemplate(
        parseJSONField("#character-template-json", "玩家角色卡模板"),
      );
      const rules = {
        ...(world.rules || {}),
        character_card: nextTemplate,
      };
      await bridge.apiPost("worlds/save", {
        id: world.id,
        revision: world.revision,
        slug: world.slug,
        name: world.name,
        description: world.description,
        system_prompt: world.system_prompt,
        opening_scene: world.opening_scene,
        rules,
        initial_state: world.initial_state,
      });
      toast("角色卡模板已校验并保存", "success");
      await loadCore();
    },
  });

  const updatePreview = () => {
    const nextTemplate = validateCharacterCardTemplate(
      parseJSONField("#character-template-json", "玩家角色卡模板"),
    );
    $("#character-template-preview").innerHTML =
      renderCharacterCardTemplatePreview(nextTemplate);
    $("#character-template-status").textContent =
      `模板 v${nextTemplate.version} 校验通过；确认保存后生效。`;
    return nextTemplate;
  };
  $("#character-template-import").addEventListener("click", () => {
    $("#character-template-file").click();
  });
  $("#character-template-file").addEventListener("change", async (event) => {
    const file = event.currentTarget.files?.[0];
    if (!file) return;
    try {
      if (file.size > 1024 * 1024) {
        throw new Error("角色卡模板文件不能超过 1 MiB");
      }
      const parsed = JSON.parse(await file.text());
      const imported =
        parsed?.character_card && typeof parsed.character_card === "object"
          ? parsed.character_card
          : parsed;
      validateCharacterCardTemplate(imported);
      $("#character-template-json").value = prettyJSON(imported);
      updatePreview();
      toast(`已导入 ${file.name}；尚未保存`, "success");
    } catch (error) {
      showError(
        new Error(`模板导入失败：${error?.message || String(error)}`),
      );
    } finally {
      event.currentTarget.value = "";
    }
  });
  $("#character-template-export").addEventListener("click", () => {
    const current = updatePreview();
    downloadJSON(
      `${world.slug}-character-card-template-v${current.version}.json`,
      current,
    );
    toast("角色卡模板已导出", "success");
  });
  $("#character-template-default").addEventListener("click", () => {
    $("#character-template-json").value = prettyJSON(
      DEFAULT_CHARACTER_CARD_TEMPLATE,
    );
    updatePreview();
    toast("已载入默认模板；尚未保存", "success");
  });
  $("#character-template-preview-button").addEventListener(
    "click",
    updatePreview,
  );
  updatePreview();
}

async function openCharacterManager(worldId) {
  const world = app.worlds.find((item) => item.id === worldId);
  const result = await bridge.apiGet("characters", { world_id: worldId });
  const characters = result.items || [];
  openEditor({
    title: `${world?.name || "世界"} · 角色管理`,
    kicker: "RESIDENT CHARACTERS",
    body: `
      <div class="section-toolbar">
        <p>角色拥有独立知识边界、资料与叙事方向。</p>
        <button type="button" class="button button-primary" data-character-action="new"
          data-world-id="${escapeHTML(worldId)}">＋ 新建角色</button>
      </div>
      <div class="session-stack">
        ${
          characters.length
            ? characters
                .map(
                  (character) => `
                    <div class="session-row">
                      <div>
                        <div class="session-name">${escapeHTML(character.name)}</div>
                        <div class="session-meta">${escapeHTML(character.slug)} · ${escapeHTML(
                          character.role,
                        )}</div>
                      </div>
                      <div class="session-location">${
                        character.enabled ? "已启用" : "已停用"
                      }</div>
                      <div class="table-actions">
                        <button type="button" class="action-button"
                          data-character-action="edit" data-id="${escapeHTML(
                            character.id,
                          )}" data-world-id="${escapeHTML(worldId)}">编辑</button>
                        <button type="button" class="action-button is-danger"
                          data-character-action="delete" data-id="${escapeHTML(
                            character.id,
                          )}" data-world-id="${escapeHTML(worldId)}">删除</button>
                      </div>
                    </div>
                  `,
                )
                .join("")
            : `
              <div class="empty-state compact">
                <div class="empty-symbol">♙</div>
                <span>这个世界还没有常驻角色</span>
              </div>
            `
        }
      </div>
    `,
  });
  app.currentCharacters = characters;
}

function openCharacterEditor(worldId, character = null) {
  const item =
    character ||
    {
      world_id: worldId,
      name: "",
      slug: "",
      role: "npc",
      profile: {
        identity: "",
        appearance: "",
        personality: "",
        knowledge_boundary: "",
      },
      prompt: "",
      enabled: true,
      sort_order: 0,
    };
  openEditor({
    title: character ? `编辑角色「${character.name}」` : "新建常驻角色",
    kicker: "CHARACTER CARD",
    body: `
      <div class="form-grid">
        <div class="field">
          <label for="character-name">角色名称</label>
          <input id="character-name" value="${escapeHTML(item.name)}" />
        </div>
        <div class="field">
          <label for="character-slug">唯一标识</label>
          <input id="character-slug" value="${escapeHTML(item.slug)}" />
        </div>
        <div class="field">
          <label for="character-role">角色类型</label>
          <select id="character-role">
            ${["npc", "narrator", "faction", "creature"]
              .map(
                (role) =>
                  `<option value="${role}" ${
                    item.role === role ? "selected" : ""
                  }>${role}</option>`,
              )
              .join("")}
          </select>
        </div>
        <div class="field">
          <label for="character-order">排序</label>
          <input id="character-order" type="number" value="${escapeHTML(
            item.sort_order,
          )}" />
        </div>
        <label class="switch-field field-span-2">
          <input id="character-enabled" type="checkbox" ${
            item.enabled ? "checked" : ""
          } />
          <span><strong>参与当前世界演算</strong><small>停用后仍保留角色卡</small></span>
        </label>
        <div class="field field-span-2">
          <label for="character-profile">角色资料 JSON</label>
          <textarea id="character-profile" class="code-field">${escapeHTML(
            prettyJSON(item.profile),
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <label for="character-prompt">私有叙事方向</label>
          <textarea id="character-prompt" rows="7">${escapeHTML(
            item.prompt,
          )}</textarea>
          <small>可写动机、秘密与行为边界；玩家输入不能覆盖这里。</small>
        </div>
      </div>
    `,
    onSave: async () => {
      await bridge.apiPost("characters/save", {
        id: item.id,
        revision: item.revision,
        world_id: worldId,
        name: $("#character-name").value,
        slug: $("#character-slug").value,
        role: $("#character-role").value,
        sort_order: Number($("#character-order").value),
        enabled: $("#character-enabled").checked,
        profile: parseJSONField("#character-profile", "角色资料"),
        prompt: $("#character-prompt").value,
      });
      toast(character ? "角色卡已更新" : "角色卡已创建", "success");
      await loadCore();
      window.setTimeout(
        () => openCharacterManager(worldId).catch(showError),
        0,
      );
    },
  });
}

function openSessionCreator() {
  const availableWorlds = app.worlds.filter((world) => !world.archived);
  if (!availableWorlds.length) {
    toast("请先创建或恢复至少一个可用世界包", "error");
    switchView("worlds");
    return;
  }
  const preferredWorld =
    availableWorlds.find(
      (world) =>
        world.slug === app.settings?.runtime?.default_world_slug,
    ) || availableWorlds[0];
  openEditor({
    title: "建立群会话",
    kicker: "SESSION BINDING",
    body: `
      <div class="form-grid">
        <div class="binding-guide field-span-2">
          <strong>推荐自动绑定</strong>
          <span>
            只需先在“安全与设置”填写管理员 ID，再由该 ID
            在目标群发送 <code>/酒馆 开启</code>。系统会自动取得下面两个 ID，
            但不会直接启动剧情，而会先显示可选副本。
          </span>
        </div>
        <div class="field">
          <label for="session-platform">平台实例 ID（手动）</label>
          <input
            id="session-platform"
            placeholder="例如当前适配器的唯一实例 ID"
            autocomplete="off"
            spellcheck="false"
            required
          />
          <small>
            AstrBot 当前平台适配器实例的唯一 ID，不是机器人 QQ 号，也不只是平台类型名。
          </small>
        </div>
        <div class="field">
          <label for="session-group">群 ID（手动）</label>
          <input
            id="session-group"
            placeholder="QQ 群号或平台下发的群 OpenID"
            autocomplete="off"
            spellcheck="false"
            required
          />
          <small>
            NapCat / OneBot 通常是 QQ 群号；QQ 官方接入可能是群 OpenID。
          </small>
        </div>
        <div class="field field-span-2">
          <label for="session-world">世界包</label>
          <select id="session-world">
            ${availableWorlds
              .map(
                (world) =>
                  `<option value="${escapeHTML(world.id)}" ${
                    world.id === preferredWorld.id ? "selected" : ""
                  }>${escapeHTML(
                    world.name,
                  )}（${escapeHTML(world.slug)}）</option>`,
              )
              .join("")}
          </select>
          <small>WebUI 建立后默认为关闭状态，由管理员确认后开启。</small>
        </div>
        <div class="field">
          <label for="session-instance-name">副本名称</label>
          <input
            id="session-instance-name"
            value="${escapeHTML(preferredWorld.name)}"
            maxlength="100"
            required
          />
          <small>同一世界可以建立多个互不串档的副本。</small>
        </div>
        <div class="field">
          <label for="session-instance-slug">副本标识</label>
          <input
            id="session-instance-slug"
            value="${escapeHTML(preferredWorld.slug)}"
            maxlength="64"
            pattern="[a-z0-9][a-z0-9_\\-]{0,63}"
            required
          />
          <small>群内使用 /酒馆 开启 &lt;副本标识&gt; 选择。</small>
        </div>
      </div>
    `,
    saveLabel: "建立会话",
    onSave: async () => {
      const platformId = $("#session-platform").value.trim();
      const groupId = $("#session-group").value.trim();
      const instanceName = $("#session-instance-name").value.trim();
      const instanceSlug = $("#session-instance-slug").value.trim();
      if (!platformId || !groupId || !instanceName || !instanceSlug) {
        throw new Error(
          "手动建立副本时，平台实例 ID、群 ID、副本名称和副本标识都不能为空",
        );
      }
      await bridge.apiPost("sessions/action", {
        action: "create",
        platform_id: platformId,
        group_id: groupId,
        world_ref: $("#session-world").value,
        instance_name: instanceName,
        instance_slug: instanceSlug,
      });
      toast("群副本已建立，并已加入允许群列表", "success");
      await loadCore();
    },
  });

  $("#session-world").addEventListener("change", (event) => {
    const world = availableWorlds.find(
      (item) => item.id === event.currentTarget.value,
    );
    if (!world) return;
    $("#session-instance-name").value = world.name;
    $("#session-instance-slug").value = world.slug;
  });
}

async function runSessionAction(id, action, button = null) {
  const source = app.sessions.find((item) => item.id === id);
  if (action === "clone") {
    const detail = await bridge.apiGet("sessions/detail", { id });
    const suffix = new Date().toISOString().slice(0, 10).replaceAll("-", "");
    openEditor({
      title: `克隆「${source?.instance_name || "副本"}」`,
      kicker: "BRANCH SESSION",
      body: `
        <div class="form-grid">
          <div class="field">
            <label for="clone-name">新副本名称</label>
            <input id="clone-name" maxlength="100"
              value="${escapeHTML(`${source?.instance_name || "副本"} · 分支`)}" />
          </div>
          <div class="field">
            <label for="clone-slug">新副本标识</label>
            <input id="clone-slug" maxlength="64"
              value="${escapeHTML(`${source?.instance_slug || "session"}-branch-${suffix}`)}" />
          </div>
          <div class="field field-span-2">
            <label for="clone-snapshot">分支起点</label>
            <select id="clone-snapshot">
              <option value="">当前状态；已归档副本自动使用最终存档</option>
              ${(detail.snapshots || [])
                .map(
                  (item) =>
                    `<option value="${escapeHTML(item.id)}">${escapeHTML(
                      item.name,
                    )} · 第 ${escapeHTML(item.turn_no)} 回合</option>`,
                )
                .join("")}
            </select>
            <small>新副本会复制世界状态、动态 NPC、剧情账本、场景时钟与长期记忆；原副本不受影响。</small>
          </div>
        </div>
      `,
      saveLabel: "创建分支",
      onSave: async () => {
        await bridge.apiPost("sessions/action", {
          action: "clone",
          session_id: id,
          instance_name: $("#clone-name").value.trim(),
          instance_slug: $("#clone-slug").value.trim(),
          snapshot_ref: $("#clone-snapshot").value,
        });
        toast("分支副本已建立，当前为关闭状态", "success");
        await loadCore();
      },
    });
    return;
  }
  let reason = "";
  if (action === "finish") {
    const ok = await confirmAction(
      "永久完结这个故事？",
      "系统会创建最终保护存档、取消全部计时与临时权限，并把副本设为只读。以后请从最终存档克隆续作。",
      "确认完结",
    );
    if (!ok) return;
  }
  if (action === "abort") {
    reason = window.prompt("请输入强制终止原因（必填）", "")?.trim() || "";
    if (!reason) return;
    const ok = await confirmAction(
      "强制终止并永久归档？",
      `原因：${reason}`,
      "确认终止",
    );
    if (!ok) return;
  }
  const execute = async () => {
    const session = app.sessions.find((item) => item.id === id);
    await bridge.apiPost("sessions/action", {
      session_id: id,
      action,
      reason,
      resume: action === "perform" && Number(session?.turn_no || 0) > 0,
    });
    toast(
      `会话已${
        {
          start: "进入准备大厅",
          perform: "正式推进",
          resume: "恢复至准备大厅",
          pause: "暂停",
          close: "关闭",
          finish: "永久完结并归档",
          abort: "强制终止并归档",
        }[action]
      }`,
      "success",
    );
    await loadCore();
  };
  if (action === "close") {
    const ok = await confirmAction(
      "关闭酒馆会话？",
      "关闭后不再处理群消息，也不会调用模型；现有状态和记忆仍会保留。",
      "关闭会话",
    );
    if (!ok) return;
  }
  await withBusy(button, execute);
}

async function openSessionDetail(sessionId) {
  const detail = await bridge.apiGet("sessions/detail", { id: sessionId });
  app.currentSession = detail;
  const session = detail.session;
  const roster = detail.roster || [];
  const timing = detail.instance_config?.time_rules || {};
  const ruleState = detail.rule_state || {};
  const progress = ruleState.progress || {};
  const sessionCharacters = detail.session_characters || [];
  const memories = detail.memories || [];
  const ledger = detail.story_ledger || [];
  const clocks = detail.scene_clocks || [];
  const storage = detail.storage || {};
  const operations = detail.operations || [];
  const cardRevisions = detail.card_revisions || [];
  const timerPolicy = detail.timer_policy || {
    global_enabled: false,
    switches: {},
    effective: {},
  };
  const tokenUsage = detail.token_usage || {
    session: { hour: 0, day: 0, all: 0 },
    group: { hour: 0, day: 0, all: 0 },
    quotas: [],
    by_type: [],
  };
  const sessionQuota =
    tokenUsage.quotas.find((item) => item.scope_type === "session") || {};
  const characterCardTemplate = resolvedCharacterCardTemplate(detail);
  const readonly = Boolean(detail.archive?.readonly);
  const turn = detail.turn || {
    round_no: 1,
    current_user_id: "",
    current_name: "",
    order: [],
  };
  const currentActor =
    turn.current_name || turn.current_user_id || "等待玩家加入";
  const nullable = (value) => (value === null || value === undefined ? "" : value);
  $("#session-modal-title").textContent =
    `${session.group_remark || `群 ${session.group_id}`} · ${
      session.instance_name || session.world_name
    }`;
  $("#session-modal-body").innerHTML = `
    <div class="detail-grid">
      <div class="detail-card"><span>运行状态</span><strong>${escapeHTML(
        statusLabel(session.state),
      )}</strong></div>
      <div class="detail-card"><span>副本标识</span><strong>${escapeHTML(
        session.instance_slug || session.world_slug,
      )}</strong></div>
      <div class="detail-card"><span>世界包</span><strong>${escapeHTML(
        session.world_name,
      )}</strong></div>
      <div class="detail-card"><span>剧情回合</span><strong>${escapeHTML(
        session.turn_no,
      )}</strong></div>
      <div class="detail-card"><span>多人轮次</span><strong>第 ${escapeHTML(
        turn.round_no,
      )} 轮</strong></div>
      <div class="detail-card"><span>当前行动者</span><strong>${escapeHTML(
        currentActor,
      )}</strong></div>
      <div class="detail-card"><span>当前位置</span><strong>${escapeHTML(
        session.world_state?.location || "未记录",
      )}</strong></div>
      <div class="detail-card"><span>状态修订</span><strong>r${escapeHTML(
        session.revision,
      )}</strong></div>
      <div class="detail-card"><span>当前章节</span><strong>${escapeHTML(
        progress.chapter || "未设置",
      )}</strong></div>
      <div class="detail-card"><span>剧情目标</span><strong>${escapeHTML(
        progress.current_objective || "未设置",
      )}</strong></div>
      <div class="detail-card"><span>长期记忆</span><strong>${escapeHTML(
        memories.length,
      )}</strong></div>
      <div class="detail-card"><span>副本 NPC</span><strong>${escapeHTML(
        sessionCharacters.length,
      )}</strong></div>
    </div>
    ${
      detail.archive
        ? `<div class="notice notice-warning archive-notice">
            <div><strong>永久只读归档</strong><span>${escapeHTML(
              detail.archive.termination_type === "aborted"
                ? `强制终止：${detail.archive.reason}`
                : "故事正常完结",
            )} · ${escapeHTML(formatDate(detail.archive.ended_at))}</span></div>
          </div>`
        : ""
    }
    <div class="tabbar">
      <button class="tab-button is-active" data-session-tab="state">总览与规则</button>
      <button class="tab-button" data-session-tab="roster">准备与角色 ${escapeHTML(
        roster.length,
      )}</button>
      <button class="tab-button" data-session-tab="npcs">NPC ${escapeHTML(
        sessionCharacters.length,
      )}</button>
      <button class="tab-button" data-session-tab="memory">长期记忆 ${escapeHTML(
        memories.length,
      )}</button>
      <button class="tab-button" data-session-tab="story">剧情账本与时钟</button>
      <button class="tab-button" data-session-tab="timing">时间与流程 ${escapeHTML(
        (detail.timers || []).filter((item) => ["active", "paused"].includes(item.status))
          .length,
      )}</button>
      <button class="tab-button" data-session-tab="workflow">选项与投票</button>
      <button class="tab-button" data-session-tab="rescue">急救与诊断 ${escapeHTML(
        operations.filter((item) => item.status === "pending").length,
      )}</button>
      <button class="tab-button" data-session-tab="access">权限与封禁 ${escapeHTML(
        (detail.bans || []).length,
      )}</button>
      <button class="tab-button" data-session-tab="saves">存档 ${escapeHTML(
        detail.snapshots.length,
      )}</button>
      <button class="tab-button" data-session-tab="events">时间线 ${escapeHTML(
        detail.events.length,
      )}</button>
    </div>
    <div class="tab-panel is-active" data-session-tab-panel="state">
      <div class="field">
        <label for="session-state-json">受控世界状态 JSON</label>
        <textarea id="session-state-json" class="code-field" rows="18">${escapeHTML(
          prettyJSON(session.world_state),
        )}</textarea>
        <small>手工保存前会自动生成安全快照；权限与会话状态不在此对象内。</small>
      </div>
      <div class="field" style="margin-top:18px">
        <label for="session-rules-json">副本规则、进度与内容边界 JSON</label>
        <textarea id="session-rules-json" class="code-field" rows="18">${escapeHTML(
          prettyJSON(ruleState),
        )}</textarea>
        <small>进度百分比只按正式里程碑计算；骰点可见性、NPC 策略和上下文预算也在这里按副本覆盖。</small>
      </div>
      <div class="modal-foot" style="margin: 18px -21px -20px">
        <button class="button button-primary" data-session-detail-action="save-state" ${
          readonly ? "disabled" : ""
        }>
          保存状态
        </button>
        <button class="button button-primary" data-session-detail-action="save-rules" ${
          readonly ? "disabled" : ""
        }>
          保存副本规则
        </button>
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="npcs">
      <div class="section-toolbar">
        <p>世界预设与剧情中途登记的 NPC 都保存在当前副本，不会污染其他副本。</p>
        <button class="button button-primary" data-session-detail-action="new-npc" ${
          readonly ? "disabled" : ""
        }>＋ 添加 NPC</button>
      </div>
      <div class="session-stack">
        ${
          sessionCharacters.length
            ? sessionCharacters
                .map(
                  (item) => `<div class="session-row">
                    <div><div class="session-name">${escapeHTML(item.name)}</div>
                    <div class="session-meta">${escapeHTML(item.role_type)} · ${escapeHTML(
                      item.source,
                    )} · ${escapeHTML(item.review_status)}</div></div>
                    <div class="session-location">${escapeHTML(
                      item.state?.location || item.lifecycle_status,
                    )}</div>
                    <button class="action-button" data-session-detail-action="edit-npc"
                      data-id="${escapeHTML(item.id)}" ${readonly ? "disabled" : ""}>编辑</button>
                  </div>`,
                )
                .join("")
            : '<div class="empty-state compact"><span>当前副本尚无登记 NPC</span></div>'
        }
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="memory">
      <div class="section-toolbar">
        <p>记忆可锁定、置顶、设为主持人或个人可见，并可标记失效或被新事实替代。</p>
        <button class="button button-primary" data-session-detail-action="new-memory" ${
          readonly ? "disabled" : ""
        }>＋ 添加记忆</button>
      </div>
      <div class="memory-list">
        ${
          memories.length
            ? memories
                .map(
                  (memory) => `<article class="memory-row ${
                    memory.invalidated ? "is-invalidated" : ""
                  }">
                    <div class="memory-importance"><span>${escapeHTML(
                      memory.importance,
                    )}</span></div>
                    <div class="memory-main">
                      <div class="memory-row-head"><span class="memory-kind">${escapeHTML(
                        memory.scope,
                      )} · ${escapeHTML(memory.kind)} · ${escapeHTML(
                        memory.visibility,
                      )}</span></div>
                      <div class="memory-content">${escapeHTML(memory.content)}</div>
                      <div class="tag-row">${memory.locked ? "<span>锁定</span>" : ""}${
                        memory.pinned ? "<span>置顶</span>" : ""
                      }${memory.invalidated ? "<span>已失效</span>" : ""}</div>
                    </div>
                    <button class="action-button" data-session-detail-action="edit-memory"
                      data-id="${escapeHTML(memory.id)}" ${
                        readonly ? "disabled" : ""
                      }>编辑</button>
                  </article>`,
                )
                .join("")
            : '<div class="empty-state compact"><span>当前副本尚无长期记忆</span></div>'
        }
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="story">
      <div class="detail-split">
        <div><h3>任务与剧情线账本</h3><div class="session-stack">${
          ledger.length
            ? ledger
                .map(
                  (item) => `<div class="session-row"><div>
                    <div class="session-name">${escapeHTML(item.title)}</div>
                    <div class="session-meta">${escapeHTML(item.kind)} · ${escapeHTML(
                      item.description,
                    )}</div></div><span>${escapeHTML(item.status)}</span></div>`,
                )
                .join("")
            : '<div class="empty-state compact"><span>尚无剧情账本条目</span></div>'
        }</div></div>
        <div><h3>场景时钟</h3><div class="session-stack">${
          clocks.length
            ? clocks
                .map(
                  (item) => `<div class="session-row"><div>
                    <div class="session-name">${escapeHTML(item.title)}</div>
                    <div class="session-meta">${escapeHTML(item.visibility)} · ${escapeHTML(
                      item.status,
                    )}</div></div><strong>${escapeHTML(item.current_value)}/${escapeHTML(
                      item.segments,
                    )}</strong></div>`,
                )
                .join("")
            : '<div class="empty-state compact"><span>尚无场景时钟</span></div>'
        }</div></div>
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="roster">
      <div class="section-toolbar">
        <p>每名玩家均显示完整角色资料、属性修正、当前副本状态与审核记录；点击卡片标题可收起或展开。</p>
        <div class="toolbar-actions">
          <span class="status-badge status-${escapeHTML(session.state)}">${escapeHTML(
            detail.preflight?.ok ? "可开演" : `${detail.preflight?.blockers?.length || 0} 项阻塞`,
          )}</span>
          <button class="button button-primary" data-session-detail-action="force-ready" ${
            readonly || session.state !== "preparing" ? "disabled" : ""
          }>强制所有合格角色准备</button>
        </div>
      </div>
      <div class="roster-character-list">
        ${
          roster.length
            ? roster
                .map(
                  (item, index) =>
                    renderRosterCharacterCard(
                      item,
                      characterCardTemplate,
                      session,
                      readonly,
                      index,
                    ),
                )
                .join("")
            : '<div class="empty-state compact"><span>尚未有玩家预留席位；请在群内发送 /酒馆 加入。</span></div>'
        }
      </div>
      ${
        detail.preflight?.blockers?.length
          ? `<div class="notice notice-warning" style="margin-top:16px">
              <div><strong>开演阻塞</strong><span>${detail.preflight.blockers
                .map((item) => escapeHTML(item))
                .join("；")}</span></div>
            </div>`
          : ""
      }
    </div>
    <div class="tab-panel" data-session-tab-panel="timing">
      <div class="panel" style="margin-bottom:18px">
        <div class="panel-head"><div><div class="eyebrow">COUNTDOWN POLICY</div>
          <h2>倒计时总开关与分类开关</h2></div>
          <button class="button button-primary" data-timer-policy="all"
            data-enabled="${timerPolicy.global_enabled ? "false" : "true"}" ${
              readonly ? "disabled" : ""
            }>${timerPolicy.global_enabled ? "关闭全部倒计时" : "启用全部倒计时"}</button>
        </div>
        <div class="tag-row">
          ${[
            ["card_code", "建卡码"],
            ["card_completion", "角色卡完成"],
            ["preparation", "准备大厅"],
            ["ready", "准备确认"],
            ["turn", "行动回合"],
            ["vote", "集体投票"],
            ["standby", "候补等待"],
            ["all_idle", "全员无互动"],
          ]
            .map(
              ([key, label]) => `<button class="action-button ${
                timerPolicy.effective?.[key] ? "" : "is-danger"
              }" data-timer-policy="${key}"
                data-enabled="${timerPolicy.switches?.[key] === false ? "true" : "false"}"
                ${readonly ? "disabled" : ""}>${escapeHTML(label)}：${
                  timerPolicy.effective?.[key] ? "开" : "关"
                }</button>`,
            )
            .join("")}
        </div>
        <p>关闭会冻结对应计时器并保存真实剩余时间；重新开启后继续，不执行停用期间的超时处罚。</p>
      </div>
      <div class="panel" style="margin-bottom:18px">
        <div class="panel-head"><div><div class="eyebrow">TOKEN BUDGET</div>
          <h2>当前副本 Token 用量与滚动限额</h2></div>
          <button class="button button-primary" data-session-detail-action="save-session-token-quota" ${
            readonly ? "disabled" : ""
          }>保存副本限额</button>
        </div>
        <div class="detail-grid">
          <div class="detail-card"><span>副本 1 小时</span><strong>${escapeHTML(
            tokenUsage.session.hour,
          )}</strong></div>
          <div class="detail-card"><span>副本 24 小时</span><strong>${escapeHTML(
            tokenUsage.session.day,
          )}</strong></div>
          <div class="detail-card"><span>副本累计</span><strong>${escapeHTML(
            tokenUsage.session.all,
          )}</strong></div>
        </div>
        <div class="form-grid" style="margin-top:16px">
          <label class="switch-field"><input id="quota-session-enabled" type="checkbox" ${
            sessionQuota.enabled ? "checked" : ""
          } /><span><strong>启用副本限额</strong><small>只限制当前故事副本</small></span></label>
          <div class="field"><label for="quota-session-window">副本滚动窗口（秒）</label>
            <input id="quota-session-window" type="number" min="60"
              value="${escapeHTML(sessionQuota.window_seconds || 3600)}" /></div>
          <div class="field"><label for="quota-session-limit">副本 Token 上限</label>
            <input id="quota-session-limit" type="number" min="1"
              value="${escapeHTML(sessionQuota.token_limit || 100000)}" /></div>
        </div>
        <p>群级 Token 限额已移到群会话标题旁的“群 Token 限额”，不会再跟随某个副本详情修改。</p>
      </div>
      <div class="section-toolbar">
        <p>留空表示不限时。副本值是创建时快照，修改世界模板不会突变正在运行的团。</p>
        <button class="button button-primary" data-session-detail-action="save-timing" ${
          readonly ? "disabled" : ""
        }>
          保存副本时间规则
        </button>
      </div>
      <div class="form-grid">
        <div class="field"><label for="t-card-code">建卡码有效期</label>
          <input id="t-card-code" type="number" min="1" value="${escapeHTML(
            nullable(timing.card_code_ttl_seconds),
          )}" /></div>
        <div class="field"><label for="t-card-draft">草稿保留</label>
          <input id="t-card-draft" type="number" min="1" value="${escapeHTML(
            nullable(timing.card_draft_ttl_seconds),
          )}" /></div>
        <div class="field"><label for="t-card-completion">等待完成建卡</label>
          <input id="t-card-completion" type="number" min="1" value="${escapeHTML(
            nullable(timing.card_completion_timeout_seconds),
          )}" /></div>
        <div class="field"><label for="t-preparation">准备大厅上限</label>
          <input id="t-preparation" type="number" min="1" value="${escapeHTML(
            nullable(timing.preparation_timeout_seconds),
          )}" /></div>
        <div class="field"><label for="t-ready">准备确认</label>
          <input id="t-ready" type="number" min="1" value="${escapeHTML(
            nullable(timing.ready_timeout_seconds),
          )}" /></div>
        <div class="field"><label for="t-turn">个人回合</label>
          <input id="t-turn" type="number" min="1" value="${escapeHTML(
            nullable(timing.turn_timeout_seconds),
          )}" /></div>
        <div class="field"><label for="t-turn-reminder">回合提前提醒</label>
          <input id="t-turn-reminder" type="number" min="1" value="${escapeHTML(
            nullable(timing.turn_reminder_seconds),
          )}" /></div>
        <div class="field"><label for="t-timeout-count">连续超时转候补次数</label>
          <input id="t-timeout-count" type="number" min="-1" max="20"
            value="${escapeHTML(timing.max_consecutive_timeouts ?? 2)}" /></div>
        <div class="field"><label for="t-standby">候补自动退场</label>
          <input id="t-standby" type="number" min="1" value="${escapeHTML(
            nullable(timing.standby_timeout_seconds),
          )}" /></div>
        <div class="field"><label for="t-delegation">代控授权有效期</label>
          <input id="t-delegation" type="number" min="1" value="${escapeHTML(
            nullable(timing.delegation_ttl_seconds),
          )}" /></div>
        <div class="field"><label for="t-all-idle">全员无互动暂停</label>
          <input id="t-all-idle" type="number" min="1" value="${escapeHTML(
            nullable(timing.all_idle_pause_seconds),
          )}" /></div>
        <div class="field"><label for="t-vote-one">第一轮投票</label>
          <input id="t-vote-one" type="number" min="1" value="${escapeHTML(
            nullable(timing.vote_round_one_seconds),
          )}" /></div>
        <div class="field"><label for="t-vote-two">第二轮投票</label>
          <input id="t-vote-two" type="number" min="1" value="${escapeHTML(
            nullable(timing.vote_round_two_seconds),
          )}" /></div>
        <div class="field"><label for="t-vote-reminder">投票提前提醒</label>
          <input id="t-vote-reminder" type="number" min="1" value="${escapeHTML(
            nullable(timing.vote_reminder_seconds),
          )}" /></div>
        <div class="field"><label for="t-turn-action">回合超时处理</label>
          <select id="t-turn-action">
            <option value="skip" ${timing.turn_timeout_action !== "hold" ? "selected" : ""}>跳过并累计超时</option>
            <option value="hold" ${timing.turn_timeout_action === "hold" ? "selected" : ""}>保留行动权</option>
          </select></div>
        <div class="field"><label for="t-card-action">建卡超时处理</label>
          <select id="t-card-action">
            <option value="standby" ${timing.card_timeout_action === "standby" ? "selected" : ""}>转候补</option>
            <option value="release" ${timing.card_timeout_action === "release" ? "selected" : ""}>释放席位</option>
            <option value="remind" ${timing.card_timeout_action === "remind" ? "selected" : ""}>仅提醒</option>
          </select></div>
        <div class="field"><label for="t-ready-action">准备超时处理</label>
          <select id="t-ready-action">
            <option value="standby" ${timing.ready_timeout_action === "standby" ? "selected" : ""}>转候补</option>
            <option value="remind" ${timing.ready_timeout_action === "remind" ? "selected" : ""}>仅提醒</option>
          </select></div>
        <label class="switch-field"><input id="t-pause-clock" type="checkbox" ${
          timing.pause_stops_clock !== false ? "checked" : ""
        } /><span><strong>暂停期间停止计时</strong><small>恢复后接着剩余时间</small></span></label>
        <label class="switch-field"><input id="t-announce" type="checkbox" ${
          timing.announce_timeouts !== false ? "checked" : ""
        } /><span><strong>在群内公布超时结果</strong><small>提醒和处理结果会发往对应会话</small></span></label>
      </div>
      <div class="session-stack" style="margin-top:18px">
        ${
          (detail.timers || []).length
            ? detail.timers
                .slice(0, 30)
                .map(
                  (timer) => `
                    <div class="session-row">
                      <div><div class="session-name">${escapeHTML(
                        timer.character_name || timer.display_name || timer.timer_type,
                      )}</div><div class="session-meta">${escapeHTML(
                        timer.timer_type,
                      )} · ${escapeHTML(timer.status)}</div></div>
                      <div class="session-location">${escapeHTML(
                        timer.deadline_at || "无截止时间",
                      )}</div>
                      <div class="table-actions">
                        ${
                          timer.status === "active"
                            ? `<button class="action-button" data-timer-action="pause"
                                data-id="${escapeHTML(timer.id)}">暂停</button>`
                            : ""
                        }
                        ${
                          timer.status === "paused"
                            ? `<button class="action-button" data-timer-action="resume"
                                data-id="${escapeHTML(timer.id)}">恢复</button>`
                            : ""
                        }
                        ${
                          ["active", "paused"].includes(timer.status)
                            ? `<button class="action-button" data-timer-action="extend"
                                data-id="${escapeHTML(timer.id)}">延长</button>
                              <button class="action-button is-danger" data-timer-action="expire"
                                data-id="${escapeHTML(timer.id)}">立即到期</button>
                              <button class="action-button is-danger" data-timer-action="disable"
                                data-id="${escapeHTML(timer.id)}">关闭限制</button>`
                            : ""
                        }
                      </div>
                    </div>
                  `,
                )
                .join("")
            : '<div class="empty-state compact"><span>当前没有计时器</span></div>'
        }
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="workflow">
      ${
        detail.vote
          ? `<div class="panel"><div class="panel-head"><div><div class="eyebrow">
              GROUP DECISION</div><h2>${escapeHTML(detail.vote.question)}</h2></div></div>
              <div class="command-list">${(detail.vote.options || [])
                .map(
                  (item) =>
                    `<code>${escapeHTML(item.key)}. ${escapeHTML(item.text)}</code>`,
                )
                .join("")}</div>
              <p>阶段 ${escapeHTML(detail.vote.stage)} · 截止 ${escapeHTML(
                detail.vote.deadline_at || "不限时",
              )}</p></div>`
          : detail.choice
            ? `<div class="panel"><div class="panel-head"><div><div class="eyebrow">
                CONTROLLED TURN</div><h2>${escapeHTML(
                  detail.choice.participant?.character_name ||
                    detail.choice.participant?.display_name ||
                    "当前行动者",
                )}</h2></div></div>
                <div class="command-list">${(detail.choice.choices || [])
                  .map(
                    (item) =>
                      `<code>${escapeHTML(item.key)}. ${escapeHTML(
                        item.text,
                      )} · ${escapeHTML(item.risk)}${
                        item.requires_check ? " · 需检定" : ""
                      }</code>`,
                  )
                  .join("")}</div>
                <p>重整次数 ${escapeHTML(detail.choice.reroll_count)} / 1</p></div>`
            : '<div class="empty-state compact"><span>当前没有未完成的选项或投票。</span></div>'
      }
      <div class="session-stack" style="margin-top:18px">
        ${(detail.return_requests || [])
          .map(
            (item) => `<div class="session-row"><div><div class="session-name">
              ${escapeHTML(item.character_name || item.display_name)}</div>
              <div class="session-meta">${escapeHTML(item.objective)}</div></div>
              <div class="session-location">${escapeHTML(item.status)}</div></div>`,
          )
          .join("")}
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="access">
      <div class="section-toolbar">
        <p>主持人负责副本流程；秩序管理员只处理队列与副本级异常。</p>
        <div class="toolbar-actions">
          <input id="permission-user-id" placeholder="真实用户 ID" />
          <select id="permission-role"><option value="moderator">秩序管理员</option>
            <option value="host">副本主持人</option></select>
          <button class="button button-primary" data-session-detail-action="grant-role">
            授予权限
          </button>
        </div>
      </div>
      <div class="session-stack">
        ${(detail.permissions || [])
          .map(
            (item) => `<div class="session-row"><div><div class="session-name">
              ${escapeHTML(item.user_id)}</div><div class="session-meta">由
              ${escapeHTML(item.granted_by)} 授予</div></div>
              <div class="session-location">${escapeHTML(item.role)}</div></div>`,
          )
          .join("") || '<div class="empty-state compact"><span>尚未设置副本角色权限</span></div>'}
      </div>
      <h3 style="margin-top:22px">有效黑名单</h3>
      <div class="session-stack">
        ${(detail.bans || [])
          .map(
            (item) => `<div class="session-row"><div><div class="session-name">
              ${escapeHTML(item.user_id)}</div><div class="session-meta">
              ${escapeHTML(item.reason || "未注明原因")}</div></div>
              <div class="session-location">${escapeHTML(item.scope)} ·
              ${escapeHTML(item.expires_at || "永久")}</div></div>`,
          )
          .join("") || '<div class="empty-state compact"><span>当前没有有效封禁</span></div>'}
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="rescue">
      <div class="section-toolbar">
        <p>只修复当前损坏的组件；所有操作都会写入审计记录。运行中的世界契约不会被热替换。</p>
        <button class="button button-primary" data-session-detail-action="download-diagnostics">导出脱敏诊断包</button>
      </div>
      <div class="detail-grid">
        <div class="detail-card"><span>未完成事务</span><strong>${escapeHTML(operations.filter((item) => item.status === "pending").length)}</strong></div>
        <div class="detail-card"><span>失败事务</span><strong>${escapeHTML(operations.filter((item) => item.status === "failed").length)}</strong></div>
        <div class="detail-card"><span>角色卡修订</span><strong>${escapeHTML(cardRevisions.filter((item) => item.status === "pending").length)}</strong></div>
      </div>
      <h3 style="margin-top:22px">事务恢复</h3>
      <div class="session-stack">
        ${operations.slice(0, 12).map((item) => `<div class="session-row"><div><div class="session-name">${escapeHTML(item.operation_type)} · ${escapeHTML(item.status)}</div><div class="session-meta">${escapeHTML(item.operation_id)} · ${escapeHTML(item.result?.phase || "reserved")}</div></div>${item.status === "pending" ? `<button class="action-button is-danger" data-session-detail-action="cancel-operation" data-operation-id="${escapeHTML(item.operation_id)}">放弃任务</button>` : ""}</div>`).join("") || '<div class="empty-state compact"><span>暂无事务记录</span></div>'}
      </div>
      <div class="field" style="margin-top:22px">
        <label for="rescue-choices-json">当前 A—D 选项 JSON</label>
        <textarea id="rescue-choices-json" class="code-field" rows="14">${escapeHTML(prettyJSON(detail.choice?.choices || []))}</textarea>
        <small>危险度与检定仍会按照冻结的世界契约严格校验。</small>
      </div>
      <button class="button button-primary" data-session-detail-action="replace-choices" ${!detail.choice || readonly ? "disabled" : ""}>保存当前选项</button>
      <div class="field" style="margin-top:22px">
        <label for="rescue-narrative">故事正文／过渡剧情</label>
        <textarea id="rescue-narrative" rows="8" placeholder="仅填写需要修订或插入的故事正文"></textarea>
      </div>
      <div class="toolbar-actions">
        <button class="button" data-session-detail-action="edit-last-narrative" ${readonly ? "disabled" : ""}>修订上一段正文</button>
        <button class="button" data-session-detail-action="bridge-narrative" ${readonly ? "disabled" : ""}>插入过渡剧情</button>
        <button class="button button-danger" data-session-detail-action="rollback-before-turn" ${readonly ? "disabled" : ""}>回到本轮之前</button>
      </div>
      <h3 style="margin-top:22px">角色卡修改申请</h3>
      <div class="session-stack">
        ${cardRevisions.map((item) => `<div class="session-row"><div><div class="session-name">${escapeHTML(item.character_name || item.display_name)} · v${escapeHTML(item.base_version)} → v${escapeHTML(item.candidate_version)}</div><div class="session-meta">${escapeHTML(item.request_note || "未填写修改说明")} · ${escapeHTML(item.status)}</div></div>${item.status === "pending" ? `<div class="table-actions"><button class="action-button" data-session-detail-action="revision-approve" data-request-id="${escapeHTML(item.id)}">通过</button><button class="action-button is-danger" data-session-detail-action="revision-reject" data-request-id="${escapeHTML(item.id)}">拒绝</button></div>` : ""}</div>`).join("") || '<div class="empty-state compact"><span>暂无角色卡修改申请</span></div>'}
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="saves">
      <div class="storage-summary">
        <div>
          <span>副本独立目录</span>
          <code>${escapeHTML(storage.relative_path || "等待建立目录")}</code>
        </div>
        <div>
          <span>实时运行库</span>
          <strong>${storage.database_exists ? "instance.sqlite3 已就绪" : "等待同步"}</strong>
        </div>
        <div>
          <span>独立文件存档</span>
          <strong>${escapeHTML(storage.save_files?.length || 0)} 份手动／最终存档 ·
            ${escapeHTML(storage.backup_files?.length || 0)} 份安全备份</strong>
        </div>
        <div>
          <span>文件同步</span>
          <strong>${escapeHTML(storage.sync_status || "pending")}</strong>
        </div>
      </div>
      <h3 style="margin-top:22px">独立安全文件</h3>
      <div class="session-stack">
        ${
          [
            ...(storage.save_files || []).map((item) => ({
              ...item,
              label: "手动／最终存档",
            })),
            ...(storage.backup_files || []).map((item) => ({
              ...item,
              label: "周期安全备份",
            })),
          ]
            .map(
              (item) => `<div class="session-row"><div>
                <div class="session-name"><code>${escapeHTML(
                  item.filename,
                )}</code></div>
                <div class="session-meta">${escapeHTML(item.label)} ·
                  ${escapeHTML(formatDate(item.created_at))}</div>
              </div><div class="session-location">${escapeHTML(
                formatBytes(item.size || 0),
              )}</div>${
                item.kind === "save"
                  ? `<button class="action-button is-danger"
                      data-session-detail-action="delete-independent-save"
                      data-filename="${escapeHTML(item.filename)}">删除文件</button>`
                  : ""
              }</div>`,
            )
            .join("") ||
          '<div class="empty-state compact"><span>尚未生成独立安全文件</span></div>'
        }
      </div>
      <div class="section-toolbar">
        <p>上方独立 ZIP 与下方回合恢复点分别管理；最终保护 ZIP 会拒绝删除。</p>
        <div class="toolbar-actions">
          <button class="button button-primary" data-session-detail-action="new-save" ${
            readonly ? "disabled" : ""
          }>＋ 创建存档</button>
          <button class="button is-danger" data-session-detail-action="delete-session" ${
            ["closed", "finished"].includes(session.state) ? "" : "disabled"
          }>删除整个故事副本</button>
        </div>
      </div>
      <div class="session-stack">
        ${
          detail.snapshots.length
            ? detail.snapshots
                .map(
                  (snapshot) => `
                    <div class="session-row">
                      <div>
                        <div class="session-name">${escapeHTML(snapshot.name)}</div>
                        <div class="session-meta">第 ${escapeHTML(
                          snapshot.turn_no,
                        )} 回合 · ${escapeHTML(
                          snapshotKindLabel(snapshot.kind),
                        )} · ${escapeHTML(formatDate(snapshot.created_at))}</div>
                      </div>
                      <div class="session-location">${escapeHTML(
                        snapshot.created_by || "system",
                      )}</div>
                      <div class="table-actions">
                        <button class="action-button" data-session-detail-action="restore-save"
                          data-id="${escapeHTML(snapshot.id)}">恢复</button>
                        ${
                          ["safety", "undo"].includes(snapshot.kind)
                            ? ""
                            : `<button class="action-button is-danger"
                              data-session-detail-action="delete-save"
                              data-id="${escapeHTML(snapshot.id)}">删除</button>`
                        }
                      </div>
                    </div>
                  `,
                )
                .join("")
            : '<div class="empty-state compact"><span>当前没有存档</span></div>'
        }
      </div>
    </div>
    <div class="tab-panel" data-session-tab-panel="events">
      <div class="timeline">
        ${
          detail.events.length
            ? [...detail.events]
                .reverse()
                .map(
                  (event) => `
                    <div class="timeline-item role-${escapeHTML(event.role)}">
                      <div class="timeline-title">
                        <strong>${escapeHTML(event.actor_name || event.role)}</strong>
                        <span>回合 ${escapeHTML(event.turn_no)} · ${escapeHTML(
                          formatDate(event.created_at),
                        )}</span>
                      </div>
                      <div class="timeline-content">${escapeHTML(event.content)}</div>
                    </div>
                  `,
                )
                .join("")
            : '<div class="empty-state compact"><span>时间线尚无事件</span></div>'
        }
      </div>
    </div>
  `;
  Object.entries(SESSION_TIME_FIELDS).forEach(([selector, key]) =>
    setTimeValue(selector, timing[key]),
  );
  if (readonly) {
    $$(
      "#session-modal-body [data-session-detail-action], #session-modal-body [data-timer-action]",
    ).forEach((item) => {
      if (
        ["delete-session", "delete-independent-save"].includes(
          item.dataset.sessionDetailAction,
        )
      ) {
        return;
      }
      item.disabled = true;
      item.title = "永久归档副本为只读；请从存档克隆新副本后继续";
    });
    $$(
      "#session-modal-body textarea, #session-modal-body input, #session-modal-body select",
    ).forEach((item) => {
      item.disabled = true;
    });
  }
  if (!$("#session-modal").open) {
    $("#session-modal").showModal();
  }
}

function openPlayerEditor(sessionId, player = null) {
  const item =
    player ||
    {
      session_id: sessionId,
      user_id: "",
      display_name: "",
      character_name: "",
      profile: {},
      enabled: true,
    };
  openEditor({
    title: player ? `编辑玩家「${player.display_name}」` : "添加玩家",
    kicker: "PLAYER BINDING",
    body: `
      <div class="form-grid">
        <div class="field">
          <label for="player-user-id">真实平台用户 ID</label>
          <input id="player-user-id" value="${escapeHTML(item.user_id)}" ${
            player ? "readonly" : ""
          } />
        </div>
        <div class="field">
          <label for="player-display-name">显示名称</label>
          <input id="player-display-name" value="${escapeHTML(
            item.display_name,
          )}" />
        </div>
        <div class="field field-span-2">
          <label for="player-character-name">剧中角色名</label>
          <input id="player-character-name" value="${escapeHTML(
            item.character_name,
          )}" placeholder="留空则沿用群昵称" />
        </div>
        <label class="switch-field field-span-2">
          <input id="player-enabled" type="checkbox" ${
            item.enabled ? "checked" : ""
          } />
          <span><strong>允许参与剧情</strong><small>停用后其发言不会进入模型</small></span>
        </label>
        <div class="field field-span-2">
          <label for="player-profile">角色能力与背景 JSON</label>
          <textarea id="player-profile" class="code-field">${escapeHTML(
            prettyJSON(item.profile),
          )}</textarea>
        </div>
      </div>
    `,
    onSave: async () => {
      await bridge.apiPost("players/save", {
        session_id: sessionId,
        user_id: $("#player-user-id").value,
        display_name: $("#player-display-name").value,
        character_name: $("#player-character-name").value,
        enabled: $("#player-enabled").checked,
        profile: parseJSONField("#player-profile", "玩家资料"),
      });
      toast("玩家资料已保存", "success");
      await loadCore();
      await openSessionDetail(sessionId);
    },
  });
}

function openSnapshotEditor(sessionId) {
  openEditor({
    title: "创建命名存档",
    kicker: "TIMELINE SNAPSHOT",
    body: `
      <div class="field">
        <label for="snapshot-name">存档名称</label>
        <input id="snapshot-name" maxlength="100" placeholder="例如：进入旧塔之前" />
        <small>发现同名存档时会展示覆盖确认，不再返回通用错误。</small>
      </div>
    `,
    saveLabel: "创建存档",
    onSave: async () => {
      const name = $("#snapshot-name").value.trim();
      const existing = (app.currentSession?.snapshots || []).find(
        (item) => item.name === name,
      );
      let replace = false;
      if (existing) {
        replace = await confirmAction(
          `覆盖同名存档「${name}」？`,
          `原存档位于第 ${existing.turn_no} 回合，覆盖后无法恢复旧内容。`,
          "确认覆盖",
        );
        if (!replace) return;
      }
      await bridge.apiPost("snapshots/create", {
        session_id: sessionId,
        name,
        replace,
      });
      toast(replace ? "同名存档已覆盖" : "存档已创建", "success");
      await openSessionDetail(sessionId);
    },
  });
}

async function loadMemories() {
  const sessionId = $("#memory-session-select").value;
  if (!sessionId) {
    app.memories = [];
    renderMemories();
    return;
  }
  const result = await bridge.apiGet("memories", {
    session_id: sessionId,
    q: $("#memory-search").value,
    limit: 200,
  });
  app.memories = result.items || [];
  renderMemories();
}

function renderMemories() {
  const root = $("#memory-grid");
  if (!$("#memory-session-select").value) {
    root.innerHTML =
      '<div class="empty-state"><div class="empty-symbol">≋</div><span>请先选择一个群会话</span></div>';
    return;
  }
  if (!app.memories.length) {
    root.innerHTML =
      '<div class="empty-state"><div class="empty-symbol">≋</div><strong>没有匹配的记忆</strong><span>可手工添加，或由叙事引擎在剧情中自动提取。</span></div>';
    return;
  }
  root.innerHTML = app.memories
    .map(
      (memory) => `
        <article class="memory-row">
          <div class="memory-importance" title="重要度 ${escapeHTML(
            memory.importance,
          )}">
            <span>${escapeHTML(memory.importance)}</span>
            <small>/ 5</small>
          </div>
          <div class="memory-main">
            <div class="memory-row-head">
              <span class="memory-kind">${escapeHTML(
                memory.scope,
              )} · ${escapeHTML(memory.kind)}</span>
              <span class="memory-scope-id">${escapeHTML(
                memory.scope_id || "全局",
              )}</span>
            </div>
            <div class="memory-content">${escapeHTML(memory.content)}</div>
            <div class="tag-row">
              ${(memory.tags || [])
                .map((tag) => `<span class="tag">${escapeHTML(tag)}</span>`)
                .join("")}
            </div>
          </div>
          <div class="memory-row-side">
            <time>${escapeHTML(formatDate(memory.updated_at))}</time>
            <div class="table-actions">
              <button class="action-button" data-memory-action="edit"
                data-id="${escapeHTML(memory.id)}">编辑</button>
              <button class="action-button is-danger" data-memory-action="delete"
                data-id="${escapeHTML(memory.id)}">删除</button>
            </div>
          </div>
        </article>
      `,
    )
    .join("");
}

function openSessionNPCEditor(sessionId, npc = null) {
  const item =
    npc ||
    {
      session_id: sessionId,
      name: "",
      aliases: [],
      role_type: "npc",
      public_profile: {},
      known_facts: [],
      misconceptions: [],
      state: {},
      review_status: "approved",
      lifecycle_status: "active",
      persistent: true,
    };
  openEditor({
    title: npc ? `编辑 NPC「${npc.name}」` : "添加副本 NPC",
    kicker: "SESSION CHARACTER",
    body: `
      <div class="form-grid">
        <div class="field">
          <label for="npc-name">名称</label>
          <input id="npc-name" value="${escapeHTML(item.name)}" maxlength="80" />
        </div>
        <div class="field">
          <label for="npc-role-type">角色类型</label>
          <input id="npc-role-type" value="${escapeHTML(item.role_type)}" maxlength="40" />
        </div>
        <div class="field field-span-2">
          <label for="npc-aliases">别名</label>
          <input id="npc-aliases" value="${escapeHTML(
            (item.aliases || []).join(", "),
          )}" placeholder="以逗号分隔；会检查重名与别名冲突" />
        </div>
        <div class="field">
          <label for="npc-review-status">复核状态</label>
          <select id="npc-review-status">
            ${[
              ["pending", "待复核"],
              ["approved", "已通过"],
              ["rejected", "已拒绝"],
              ["duplicate", "疑似重复"],
            ]
              .map(
                ([value, label]) =>
                  `<option value="${value}" ${
                    item.review_status === value ? "selected" : ""
                  }>${label}</option>`,
              )
              .join("")}
          </select>
        </div>
        <div class="field">
          <label for="npc-lifecycle-status">剧情状态</label>
          <select id="npc-lifecycle-status">
            ${[
              ["active", "活跃"],
              ["departed", "离场"],
              ["dead", "死亡"],
              ["archived", "归档"],
            ]
              .map(
                ([value, label]) =>
                  `<option value="${value}" ${
                    item.lifecycle_status === value ? "selected" : ""
                  }>${label}</option>`,
              )
              .join("")}
          </select>
        </div>
        <label class="switch-field field-span-2">
          <input id="npc-persistent" type="checkbox" ${
            item.persistent ? "checked" : ""
          } />
          <span><strong>作为持久 NPC</strong><small>临时路人可以取消；只有相关 NPC 会进入模型上下文</small></span>
        </label>
        <div class="field field-span-2">
          <label for="npc-profile">公开资料 JSON</label>
          <textarea id="npc-profile" class="code-field">${escapeHTML(
            prettyJSON(item.public_profile),
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <label for="npc-state">当前地点、阵营、关系与运行状态 JSON</label>
          <textarea id="npc-state" class="code-field">${escapeHTML(
            prettyJSON(item.state),
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <label for="npc-known-facts">NPC 真正知道的事实</label>
          <textarea id="npc-known-facts" rows="4">${escapeHTML(
            (item.known_facts || []).join("\n"),
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <label for="npc-misconceptions">误解、谣言与错误认知</label>
          <textarea id="npc-misconceptions" rows="4">${escapeHTML(
            (item.misconceptions || []).join("\n"),
          )}</textarea>
        </div>
      </div>
    `,
    onSave: async () => {
      await bridge.apiPost("sessions/npc", {
        id: item.id,
        session_id: sessionId,
        revision: item.revision,
        name: $("#npc-name").value,
        aliases: splitLines($("#npc-aliases").value),
        role_type: $("#npc-role-type").value,
        public_profile: parseJSONField("#npc-profile", "NPC 公开资料"),
        state: parseJSONField("#npc-state", "NPC 运行状态"),
        known_facts: splitLines($("#npc-known-facts").value),
        misconceptions: splitLines($("#npc-misconceptions").value),
        review_status: $("#npc-review-status").value,
        lifecycle_status: $("#npc-lifecycle-status").value,
        persistent: $("#npc-persistent").checked,
      });
      toast("副本 NPC 已保存", "success");
      await loadCore();
    },
  });
}

function openMemoryEditor(memory = null, requestedSessionId = "") {
  const sessionId =
    requestedSessionId ||
    memory?.session_id ||
    $("#memory-session-select").value;
  if (!sessionId) {
    toast("请先选择会话", "error");
    return;
  }
  const item =
    memory ||
    {
      session_id: sessionId,
      scope: "world",
      scope_id: "",
      kind: "fact",
      content: "",
      importance: 3,
      tags: [],
      visibility: "public",
      locked: false,
      pinned: false,
      invalidated: false,
      supersedes_id: "",
      conflict_status: "clear",
      governance_note: "",
    };
  openEditor({
    title: memory ? "编辑长期记忆" : "添加长期记忆",
    kicker: "MEMORY RECORD",
    body: `
      <div class="form-grid">
        <div class="field">
          <label for="memory-scope">范围</label>
          <select id="memory-scope">
            ${["world", "player", "npc"]
              .map(
                (scope) =>
                  `<option value="${scope}" ${
                    item.scope === scope ? "selected" : ""
                  }>${scope}</option>`,
              )
              .join("")}
          </select>
        </div>
        <div class="field">
          <label for="memory-scope-id">范围对象 ID</label>
          <input id="memory-scope-id" value="${escapeHTML(item.scope_id)}" />
        </div>
        <div class="field">
          <label for="memory-kind">类型</label>
          <input id="memory-kind" value="${escapeHTML(item.kind)}" />
        </div>
        <div class="field">
          <label for="memory-importance">重要度（1-5）</label>
          <input id="memory-importance" type="number" min="1" max="5"
            value="${escapeHTML(item.importance)}" />
        </div>
        <div class="field">
          <label for="memory-visibility">可见范围</label>
          <select id="memory-visibility">
            ${[
              ["public", "全队公开"],
              ["host", "仅主持人"],
              ["private", "个人私密"],
            ]
              .map(
                ([value, label]) =>
                  `<option value="${value}" ${
                    item.visibility === value ? "selected" : ""
                  }>${label}</option>`,
              )
              .join("")}
          </select>
        </div>
        <div class="field">
          <label for="memory-conflict-status">事实状态</label>
          <select id="memory-conflict-status">
            ${[
              ["clear", "明确"],
              ["conflict", "冲突待确认"],
              ["resolved", "冲突已解决"],
            ]
              .map(
                ([value, label]) =>
                  `<option value="${value}" ${
                    item.conflict_status === value ? "selected" : ""
                  }>${label}</option>`,
              )
              .join("")}
          </select>
        </div>
        <div class="field field-span-2">
          <label for="memory-content-field">记忆事实</label>
          <textarea id="memory-content-field" rows="6">${escapeHTML(
            item.content,
          )}</textarea>
        </div>
        <div class="field field-span-2">
          <label for="memory-tags">标签</label>
          <input id="memory-tags" value="${escapeHTML(
            (item.tags || []).join(", "),
          )}" placeholder="以逗号分隔" />
        </div>
        <div class="field">
          <label for="memory-supersedes">替代的旧记忆 ID</label>
          <input id="memory-supersedes" value="${escapeHTML(
            item.supersedes_id || "",
          )}" placeholder="可留空" />
        </div>
        <div class="field">
          <label for="memory-governance-note">治理备注</label>
          <input id="memory-governance-note" value="${escapeHTML(
            item.governance_note || "",
          )}" placeholder="例如：经主持人确认" />
        </div>
        <label class="switch-field">
          <input id="memory-locked" type="checkbox" ${
            item.locked ? "checked" : ""
          } />
          <span><strong>锁定</strong><small>重要事实不会被上下文预算裁掉</small></span>
        </label>
        <label class="switch-field">
          <input id="memory-pinned" type="checkbox" ${
            item.pinned ? "checked" : ""
          } />
          <span><strong>置顶</strong><small>优先进入近期叙事上下文</small></span>
        </label>
        <label class="switch-field field-span-2">
          <input id="memory-invalidated" type="checkbox" ${
            item.invalidated ? "checked" : ""
          } />
          <span><strong>标记失效</strong><small>保留审计，但不再作为有效世界事实</small></span>
        </label>
      </div>
    `,
    onSave: async () => {
      await bridge.apiPost("memories/save", {
        id: item.id,
        session_id: sessionId,
        scope: $("#memory-scope").value,
        scope_id: $("#memory-scope-id").value,
        kind: $("#memory-kind").value,
        content: $("#memory-content-field").value,
        importance: Number($("#memory-importance").value),
        tags: splitLines($("#memory-tags").value),
        visibility: $("#memory-visibility").value,
        locked: $("#memory-locked").checked,
        pinned: $("#memory-pinned").checked,
        invalidated: $("#memory-invalidated").checked,
        supersedes_id: $("#memory-supersedes").value,
        conflict_status: $("#memory-conflict-status").value,
        governance_note: $("#memory-governance-note").value,
      });
      toast("长期记忆已保存", "success");
      if ($("#memory-session-select").value === sessionId) {
        await loadMemories();
      }
      await loadCore();
    },
  });
}

async function loadAudit() {
  const result = await bridge.apiGet("audit", {
    session_id: $("#audit-session-select").value,
    limit: 200,
    offset: 0,
  });
  app.audit = result.items || [];
  renderAudit();
}

function renderAudit() {
  const body = $("#audit-table-body");
  if (!app.audit.length) {
    body.innerHTML =
      '<tr><td colspan="5"><div class="empty-state compact"><span>暂无审计记录</span></div></td></tr>';
    return;
  }
  body.innerHTML = app.audit
    .map((item) => {
      const detail = prettyJSON(item.detail);
      const shortDetail = detail.length > 150 ? `${detail.slice(0, 150)}…` : detail;
      return `
        <tr>
          <td>${escapeHTML(formatDate(item.created_at))}</td>
          <td><span class="table-title">${escapeHTML(item.action)}</span></td>
          <td>${escapeHTML(item.actor_id || "system")}</td>
          <td>${escapeHTML(item.target || "—")}</td>
          <td><code title="${escapeHTML(detail)}">${escapeHTML(shortDetail)}</code></td>
        </tr>
      `;
    })
    .join("");
}

function renderSettings() {
  const s = app.settings;
  if (!s) return;
  const configState = app.configState || {};
  $("#settings-revision").textContent = configState.revision
    ? `配置修订 r${configState.revision} · ${formatDate(configState.saved_at)}`
    : "配置修订：尚未记录";
  $("#provider-health-list").innerHTML = app.providerHealth.length
    ? app.providerHealth
        .map(
          (item) => `
            <div class="provider-health-item status-${escapeHTML(item.status)}">
              <strong>${escapeHTML(item.provider_id)}</strong>
              <span>${escapeHTML(
                item.status === "open"
                  ? `熔断至 ${formatDate(item.circuit_until)}`
                  : item.status === "half_open"
                    ? "等待试探请求"
                    : "健康",
              )}</span>
              <small>${escapeHTML(
                item.last_failure_reason ||
                  `连续失败 ${item.consecutive_failures || 0} 次`,
              )}</small>
            </div>
          `,
        )
        .join("")
    : '<div class="empty-inline">模型链尚无失败记录。</div>';
  $("#setting-admin-ids").value = (s.security.admin_ids || []).join("\n");
  $("#setting-group-ids").value = (s.security.allowed_group_ids || []).join("\n");
  $("#setting-require-whitelist").checked = s.security.require_group_whitelist;
  $("#setting-public-status").checked = s.security.public_status;
  $("#setting-unauthorized").value = s.security.unauthorized_command_behavior;

  $("#setting-provider").innerHTML = providerOptionHTML(
    s.model.provider_id || "",
    "跟随当前群会话模型",
  );
  $("#setting-image-provider").innerHTML = providerOptionHTML(
    s.model.image_caption_provider_id || "",
    "未配置（拒绝带图行动）",
  );
  renderFallbackProviders(s.model.fallback_provider_ids || []);
  $("#setting-image-prompt").value = s.model.image_caption_prompt || "";
  $("#setting-max-images").value = s.model.max_images_per_turn || 4;
  $("#setting-temperature").value = s.model.temperature;
  $("#setting-max-tokens").value = s.model.max_tokens;
  $("#setting-timeout").value = s.model.request_timeout_seconds;
  $("#setting-repair").value = s.model.json_repair_attempts;
  $("#setting-two-phase").checked = s.runtime.two_phase_checks;

  $("#setting-trigger-prefix").value = s.runtime.trigger_prefix || "jg";
  $("#setting-default-world").value = s.runtime.default_world_slug;
  $("#setting-recent-turns").value = s.runtime.recent_turns;
  $("#setting-memory-limit").value = s.runtime.memory_limit;
  $("#setting-snapshot-interval").value = s.runtime.auto_snapshot_interval;
  $("#setting-cooldown").value = s.runtime.user_cooldown_seconds;
  $("#setting-input-limit").value = s.runtime.max_input_chars;
  $("#setting-output-limit").value = s.runtime.max_output_chars;
  $("#setting-ooc-prefixes").value = (s.runtime.ooc_prefixes || []).join(", ");
  const timeRules = s.runtime.time_rules || {};
  Object.entries(GLOBAL_TIME_FIELDS).forEach(([selector, key]) =>
    setTimeValue(selector, timeRules[key]),
  );
  $("#gtime-timeout-count").value =
    timeRules.max_consecutive_timeouts ?? 2;
  $("#gtime-pause-clock").checked = timeRules.pause_stops_clock !== false;
  $("#gtime-announce").checked = timeRules.announce_timeouts !== false;
  $("#gtime-turn-action").value = timeRules.turn_timeout_action || "skip";
  $("#gtime-card-action").value = timeRules.card_timeout_action || "standby";
  $("#gtime-ready-action").value = timeRules.ready_timeout_action || "standby";

  $("#setting-audit-days").value = s.advanced.audit_retention_days;
  $("#setting-model-payloads").checked = s.advanced.store_model_payloads;
  $("#setting-debug").checked = s.advanced.debug;
}

function collectSettings() {
  return {
    security: {
      admin_ids: splitLines($("#setting-admin-ids").value),
      allowed_group_ids: splitLines($("#setting-group-ids").value),
      require_group_whitelist: $("#setting-require-whitelist").checked,
      unauthorized_command_behavior: $("#setting-unauthorized").value,
      public_status: $("#setting-public-status").checked,
    },
    model: {
      provider_id: $("#setting-provider").value,
      fallback_provider_ids: [
        ...$("#fallback-provider-list").querySelectorAll(
          "[data-fallback-provider]",
        ),
      ]
        .map((item) => item.value)
        .filter(
          (item, index, list) =>
            item &&
            item !== $("#setting-provider").value &&
            list.indexOf(item) === index,
        ),
      image_caption_provider_id: $("#setting-image-provider").value,
      image_caption_prompt: $("#setting-image-prompt").value.trim(),
      max_images_per_turn: Number($("#setting-max-images").value),
      temperature: Number($("#setting-temperature").value),
      max_tokens: Number($("#setting-max-tokens").value),
      request_timeout_seconds: Number($("#setting-timeout").value),
      json_repair_attempts: Number($("#setting-repair").value),
    },
    runtime: {
      default_world_slug: $("#setting-default-world").value.trim(),
      trigger_prefix: $("#setting-trigger-prefix").value.trim(),
      two_phase_checks: $("#setting-two-phase").checked,
      max_input_chars: Number($("#setting-input-limit").value),
      max_output_chars: Number($("#setting-output-limit").value),
      recent_turns: Number($("#setting-recent-turns").value),
      memory_limit: Number($("#setting-memory-limit").value),
      user_cooldown_seconds: Number($("#setting-cooldown").value),
      auto_snapshot_interval: Number($("#setting-snapshot-interval").value),
      ooc_prefixes: splitLines($("#setting-ooc-prefixes").value),
      time_rules: {
        ...(app.settings?.runtime?.time_rules || {}),
        card_code_ttl_seconds: readTimeValue("#gtime-card-code"),
        card_draft_ttl_seconds: readTimeValue("#gtime-card-draft"),
        card_completion_timeout_seconds: readTimeValue("#gtime-card-completion"),
        preparation_timeout_seconds: readTimeValue("#gtime-preparation"),
        ready_timeout_seconds: readTimeValue("#gtime-ready"),
        turn_timeout_seconds: readTimeValue("#gtime-turn"),
        turn_reminder_seconds: readTimeValue("#gtime-turn-reminder"),
        max_consecutive_timeouts: Number($("#gtime-timeout-count").value),
        standby_timeout_seconds: readTimeValue("#gtime-standby"),
        delegation_ttl_seconds: readTimeValue("#gtime-delegation"),
        all_idle_pause_seconds: readTimeValue("#gtime-all-idle"),
        vote_round_one_seconds: readTimeValue("#gtime-vote-one"),
        vote_round_two_seconds: readTimeValue("#gtime-vote-two"),
        vote_reminder_seconds: readTimeValue("#gtime-vote-reminder"),
        pause_stops_clock: $("#gtime-pause-clock").checked,
        announce_timeouts: $("#gtime-announce").checked,
        turn_timeout_action: $("#gtime-turn-action").value,
        card_timeout_action: $("#gtime-card-action").value,
        ready_timeout_action: $("#gtime-ready-action").value,
      },
    },
    advanced: {
      audit_retention_days: Number($("#setting-audit-days").value),
      store_model_payloads: $("#setting-model-payloads").checked,
      debug: $("#setting-debug").checked,
    },
  };
}

function confirmAction(title, message, confirmLabel = "确认") {
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  $("#confirm-submit").textContent = confirmLabel;
  const modal = $("#confirm-modal");
  modal.showModal();
  return new Promise((resolve) => {
    const handler = () => {
      modal.removeEventListener("close", handler);
      resolve(modal.returnValue === "confirm");
    };
    modal.addEventListener("close", handler);
  });
}

async function handleWorldAction(button) {
  const world = app.worlds.find((item) => item.id === button.dataset.id);
  if (!world) return;
  const action = button.dataset.worldAction;
  if (action === "edit") openWorldEditor(world);
  if (action === "characters") await openCharacterManager(world.id);
  if (action === "card-template") openCharacterCardTemplateManager(world);
  if (action === "preflight") {
    const response = await bridge.apiPost("worlds/preflight", { world_ref: world.id });
    const report = response.report || {};
    const summary = report.summary || {};
    openEditor({
      title: `${world.name} · 世界包体检`,
      kicker: report.compatible ? "COMPATIBLE" : "BLOCKED",
      body: `
        <div class="detail-grid">
          <div class="detail-card"><span>协议</span><strong>v${escapeHTML(summary.schema_version || "?")}</strong></div>
          <div class="detail-card"><span>数值</span><strong>${escapeHTML(summary.stats_mode || "unknown")}</strong></div>
          <div class="detail-card"><span>裁定</span><strong>${escapeHTML(summary.resolution_mode || "unknown")}</strong></div>
          <div class="detail-card"><span>结果</span><strong>${report.compatible ? "可导入" : "需修复"}</strong></div>
        </div>
        <div class="session-stack" style="margin-top:18px">
          ${(report.issues || []).map((item) => `<div class="session-row"><div><div class="session-name">${escapeHTML(item.level.toUpperCase())} · ${escapeHTML(item.message)}</div><div class="session-meta">${escapeHTML(item.path)} · ${escapeHTML(item.code)}</div></div></div>`).join("") || '<div class="empty-state compact"><span>未发现兼容性问题</span></div>'}
        </div>
        <h3 style="margin-top:22px">试运行</h3>
        <div class="command-list">${(report.tests || []).map((item) => `<code>${item.status === "passed" ? "✓" : "×"} ${escapeHTML(item.name)}</code>`).join("")}</div>
      `,
    });
  }
  if (action === "archive") {
    const ok = await confirmAction(
      `归档「${world.name}」？`,
      "归档后不能用于新会话；正在运行的会话会阻止此操作。已有数据不会删除。",
      "归档世界",
    );
    if (!ok) return;
    await bridge.apiPost("worlds/archive", { id: world.id });
    toast("世界包已归档", "success");
    await loadCore();
  }
  if (action === "restore") {
    await bridge.apiPost("worlds/restore", { id: world.id });
    toast("世界包已恢复", "success");
    await loadCore();
  }
}

function openGroupRemarkEditor(button) {
  const platformId = button.dataset.platformId;
  const groupId = button.dataset.groupId;
  const currentRemark = button.dataset.groupRemark || "";
  const revision = Number(button.dataset.groupRevision || 1);
  openEditor({
    title: currentRemark ? `编辑群备注「${currentRemark}」` : "添加群备注",
    kicker: "GROUP NOTE",
    body: `
      <div class="field">
        <label for="group-remark-input">群备注</label>
        <input id="group-remark-input" maxlength="120"
          value="${escapeHTML(currentRemark)}"
          placeholder="例如：周六固定团、测试一群" />
        <small>群备注只用于管理台显示和检索，不会改变稳定目录名或群 ID。</small>
      </div>
      <div class="binding-guide">
        <strong>${escapeHTML(platformId)}</strong>
        <span>群 ID：${escapeHTML(groupId)}</span>
      </div>
    `,
    saveLabel: currentRemark ? "保存备注" : "添加备注",
    onSave: async () => {
      await bridge.apiPost("groups/remark", {
        platform_id: platformId,
        group_id: groupId,
        remark: $("#group-remark-input").value.trim(),
        revision,
      });
      toast("群备注已保存", "success");
      await loadSessionPage();
    },
  });
}

async function openGroupTokenQuotaEditor(button) {
  const platformId = button.dataset.platformId;
  const groupId = button.dataset.groupId;
  const groupName = button.dataset.groupName || `群 ${groupId}`;
  const response = await bridge.apiGet("groups/token-usage", {
    platform_id: platformId,
    group_id: groupId,
  });
  const usage = response.usage || {};
  const totals = usage.group || { hour: 0, day: 0, all: 0 };
  const quota = usage.quota || {};
  openEditor({
    title: `${groupName} · Token 限额`,
    kicker: "GROUP TOKEN BUDGET",
    body: `
      <div class="detail-grid">
        <div class="detail-card"><span>群 1 小时</span><strong>${escapeHTML(
          totals.hour || 0,
        )}</strong></div>
        <div class="detail-card"><span>群 24 小时</span><strong>${escapeHTML(
          totals.day || 0,
        )}</strong></div>
        <div class="detail-card"><span>群累计</span><strong>${escapeHTML(
          totals.all || 0,
        )}</strong></div>
      </div>
      <div class="form-grid" style="margin-top:18px">
        <label class="switch-field"><input id="group-quota-enabled" type="checkbox" ${
          quota.enabled ? "checked" : ""
        } /><span><strong>启用群 Token 限额</strong><small>该群内全部故事副本共享同一额度</small></span></label>
        <div class="field"><label for="group-quota-window">群滚动窗口（秒）</label>
          <input id="group-quota-window" type="number" min="60"
            value="${escapeHTML(quota.window_seconds || 86400)}" /></div>
        <div class="field"><label for="group-quota-limit">群 Token 上限</label>
          <input id="group-quota-limit" type="number" min="1"
            value="${escapeHTML(quota.token_limit || 500000)}" /></div>
      </div>
      <div class="binding-guide">
        <strong>${escapeHTML(platformId)}</strong>
        <span>群 ID：${escapeHTML(groupId)}</span>
      </div>
    `,
    saveLabel: "保存群限额",
    onSave: async () => {
      const windowSeconds = Number($("#group-quota-window").value);
      const tokenLimit = Number($("#group-quota-limit").value);
      if (
        !Number.isInteger(windowSeconds) ||
        windowSeconds < 60 ||
        !Number.isInteger(tokenLimit) ||
        tokenLimit < 1
      ) {
        throw new Error("Token 限额窗口至少 60 秒，Token 上限必须为正整数");
      }
      await bridge.apiPost("groups/token-quota", {
        platform_id: platformId,
        group_id: groupId,
        enabled: $("#group-quota-enabled").checked,
        window_seconds: windowSeconds,
        token_limit: tokenLimit,
      });
      toast("群 Token 限额已保存", "success");
      await loadSessionPage();
    },
  });
}

async function handleCharacterAction(button) {
  const action = button.dataset.characterAction;
  const worldId = button.dataset.worldId;
  if (action === "new") {
    openCharacterEditor(worldId);
    return;
  }
  const character = (app.currentCharacters || []).find(
    (item) => item.id === button.dataset.id,
  );
  if (!character) return;
  if (action === "edit") {
    openCharacterEditor(worldId, character);
  } else if (action === "delete") {
    const ok = await confirmAction(
      `删除角色「${character.name}」？`,
      "角色卡会从世界包中永久移除。群会话的历史记录不会被改写。",
      "删除角色",
    );
    if (!ok) return;
    await bridge.apiPost("characters/delete", { id: character.id });
    toast("角色已删除", "success");
    await loadCore();
    await openCharacterManager(worldId);
  }
}

async function handleSessionDetailAction(button) {
  const detail = app.currentSession;
  if (!detail) return;
  const sessionId = detail.session.id;
  const action = button.dataset.sessionDetailAction;
  if (action === "download-diagnostics") {
    await withBusy(button, async () => {
      await bridge.download(
        "sessions/diagnostics",
        { id: sessionId },
        `tavern_diagnostic_${sessionId}.zip`,
      );
    });
    toast("脱敏诊断包已生成", "success");
  } else if (action === "request-card-revision") {
    const participant = (detail.roster || []).find((item) => item.id === button.dataset.ref);
    if (!participant) throw new Error("没有找到对应角色");
    $("#session-modal").close();
    openEditor({
      title: `${participant.character_name || participant.display_name} · 新建角色卡版本`,
      kicker: "CARD REVISION",
      body: `
        <p class="field-hint">修改会生成新版本并进入审核；审核通过前，当前副本继续使用旧版本。</p>
        <div class="field"><label for="card-revision-profile">完整角色资料 JSON</label>
          <textarea id="card-revision-profile" class="code-field" rows="18">${escapeHTML(prettyJSON(participant.card_profile || {}))}</textarea></div>
        <div class="field"><label for="card-revision-note">修改说明</label>
          <input id="card-revision-note" maxlength="500" placeholder="例如：修正背景错字与专长描述" /></div>
      `,
      saveLabel: "提交修改申请",
      onSave: async () => {
        const profile = parseJSONField("#card-revision-profile", "角色资料");
        await bridge.apiPost("sessions/card-revisions", {
          action: "request",
          session_id: sessionId,
          participant_ref: participant.id,
          profile_patch: profile,
          stats_patch: {},
          note: $("#card-revision-note").value.trim(),
        });
        toast("角色卡新版本已提交审核", "success");
        await openSessionDetail(sessionId);
      },
    });
  } else if (action === "cancel-operation") {
    const ok = await confirmAction("放弃未完成任务？", "该操作不会回滚已经提交的剧情，只会解除卡死的事务锁。", "放弃任务");
    if (!ok) return;
    await bridge.apiPost("sessions/rescue", {
      session_id: sessionId,
      action: "cancel_operation",
      operation_id: button.dataset.operationId,
      reason: "WebUI 管理员主动终止",
    });
    toast("未完成任务已标记为失败，可重新提交", "success");
    await openSessionDetail(sessionId);
  } else if (action === "replace-choices") {
    let choices;
    try {
      choices = JSON.parse($("#rescue-choices-json").value || "[]");
    } catch (error) {
      throw new Error(`选项不是有效 JSON：${error.message}`);
    }
    if (!Array.isArray(choices)) throw new Error("选项必须是 A—D 数组");
    await bridge.apiPost("sessions/rescue", {
      session_id: sessionId,
      action: "replace_choices",
      choices,
    });
    toast("当前 A—D 选项已替换", "success");
    await openSessionDetail(sessionId);
  } else if (["edit-last-narrative", "bridge-narrative"].includes(action)) {
    const narrative = $("#rescue-narrative").value.trim();
    if (!narrative) throw new Error("请先填写故事正文");
    await bridge.apiPost("sessions/rescue", {
      session_id: sessionId,
      action: action === "edit-last-narrative" ? "edit_last_narrative" : "bridge_narrative",
      narrative,
    });
    toast(action === "edit-last-narrative" ? "上一段故事正文已修订" : "过渡剧情已插入", "success");
    await openSessionDetail(sessionId);
  } else if (action === "rollback-before-turn") {
    const ok = await confirmAction("回到本轮之前？", "将恢复最近一个自动、安全或单回合保护点，并暂停副本。", "确认回滚");
    if (!ok) return;
    await bridge.apiPost("sessions/rescue", {
      session_id: sessionId,
      action: "rollback_before_turn",
    });
    toast("已恢复到最近保护点", "success");
    await openSessionDetail(sessionId);
  } else if (["revision-approve", "revision-reject"].includes(action)) {
    await bridge.apiPost("sessions/card-revisions", {
      action: action === "revision-approve" ? "approve" : "reject",
      request_id: button.dataset.requestId,
      note: action === "revision-approve" ? "WebUI 审核通过" : "WebUI 审核拒绝",
    });
    toast(action === "revision-approve" ? "角色卡新版本已启用" : "角色卡修改已拒绝", "success");
    await openSessionDetail(sessionId);
  } else if (action === "force-ready") {
    const ok = await confirmAction(
      "强制所有合格角色准备？",
      "只处理角色卡已审核通过且当前出场的玩家；不会绕过建卡或审核，也不会自动开演。",
      "确认强制准备",
    );
    if (!ok) return;
    await withBusy(button, async () => {
      const response = await bridge.apiPost("sessions/action", {
        session_id: sessionId,
        action: "force_ready",
      });
      toast(
        `已强制准备 ${response.result?.ready_count || 0} 人`,
        "success",
      );
      await openSessionDetail(sessionId);
    });
  } else if (action === "save-session-token-quota") {
    const policy = {
      scope_type: "session",
      enabled: $("#quota-session-enabled").checked,
      window_seconds: Number($("#quota-session-window").value),
      token_limit: Number($("#quota-session-limit").value),
    };
    if (
      !Number.isInteger(policy.window_seconds) ||
      policy.window_seconds < 60 ||
      !Number.isInteger(policy.token_limit) ||
      policy.token_limit < 1
    ) {
      throw new Error("Token 限额窗口至少 60 秒，Token 上限必须为正整数");
    }
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/token-quota", {
        session_id: sessionId,
        ...policy,
      });
      toast("副本 Token 限额已保存", "success");
      await openSessionDetail(sessionId);
    });
  } else if (action === "delete-session") {
    const name = detail.session.instance_name;
    const entered = window.prompt(
      `此操作会把整个故事副本移入回收目录。\n请输入副本名“${name}”确认：`,
      "",
    );
    if (entered === null) return;
    if (entered.trim() !== name) {
      throw new Error("输入的副本名不一致，未执行删除");
    }
    const ok = await confirmAction(
      `删除整个故事副本「${name}」？`,
      "角色、剧情、存档、Token 流水和独立 SQLite 都会从运行数据库移除，并尝试移入服务器回收目录。",
      "确认删除副本",
    );
    if (!ok) return;
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/action", {
        session_id: sessionId,
        action: "delete",
        confirm_name: name,
      });
      $("#session-modal").close();
      toast("整个故事副本已删除并移入回收目录", "success");
      await loadCore();
    });
  } else if (action === "save-state") {
    const worldState = parseJSONField("#session-state-json", "世界状态");
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/state", {
        session_id: sessionId,
        revision: detail.session.revision,
        world_state: worldState,
      });
      toast("世界状态已保存，并生成安全快照", "success");
      await loadCore();
      await openSessionDetail(sessionId);
    });
  } else if (action === "save-rules") {
    const rules = parseJSONField("#session-rules-json", "副本规则");
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/rules", {
        session_id: sessionId,
        rules,
      });
      toast("副本规则、剧情进度与内容边界已保存", "success");
      await loadCore();
      await openSessionDetail(sessionId);
    });
  } else if (action === "new-npc" || action === "edit-npc") {
    const npc =
      action === "edit-npc"
        ? (detail.session_characters || []).find(
            (item) => item.id === button.dataset.id,
          )
        : null;
    $("#session-modal").close();
    openSessionNPCEditor(sessionId, npc);
  } else if (action === "new-memory" || action === "edit-memory") {
    const memory =
      action === "edit-memory"
        ? (detail.memories || []).find((item) => item.id === button.dataset.id)
        : null;
    $("#session-modal").close();
    openMemoryEditor(memory, sessionId);
  } else if (action === "save-timing") {
    const current = detail.instance_config?.time_rules || {};
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/time-rules", {
        session_id: sessionId,
        rules: {
          ...current,
          card_code_ttl_seconds: readTimeValue("#t-card-code"),
          card_draft_ttl_seconds: readTimeValue("#t-card-draft"),
          card_completion_timeout_seconds: readTimeValue("#t-card-completion"),
          preparation_timeout_seconds: readTimeValue("#t-preparation"),
          ready_timeout_seconds: readTimeValue("#t-ready"),
          turn_timeout_seconds: readTimeValue("#t-turn"),
          turn_reminder_seconds: readTimeValue("#t-turn-reminder"),
          max_consecutive_timeouts: Number($("#t-timeout-count").value),
          standby_timeout_seconds: readTimeValue("#t-standby"),
          delegation_ttl_seconds: readTimeValue("#t-delegation"),
          all_idle_pause_seconds: readTimeValue("#t-all-idle"),
          vote_round_one_seconds: readTimeValue("#t-vote-one"),
          vote_round_two_seconds: readTimeValue("#t-vote-two"),
          vote_reminder_seconds: readTimeValue("#t-vote-reminder"),
          pause_stops_clock: $("#t-pause-clock").checked,
          announce_timeouts: $("#t-announce").checked,
          turn_timeout_action: $("#t-turn-action").value,
          card_timeout_action: $("#t-card-action").value,
          ready_timeout_action: $("#t-ready-action").value,
        },
      });
      toast("当前副本的时间与流程规则已保存", "success");
      await openSessionDetail(sessionId);
    });
  } else if (action === "card-approve" || action === "card-reject") {
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/card-review", {
        session_id: sessionId,
        participant_ref: button.dataset.ref,
        approved: action === "card-approve",
        note: action === "card-approve" ? "WebUI 审核通过" : "WebUI 审核拒绝",
      });
      toast(action === "card-approve" ? "角色卡已通过" : "角色卡已拒绝", "success");
      await openSessionDetail(sessionId);
    });
  } else if (["designate", "retire", "ban"].includes(action)) {
    if (action !== "designate") {
      const ok = await confirmAction(
        action === "ban" ? "封禁并安全退场？" : "让该角色安全退场？",
        action === "ban"
          ? "将原子移出队列、撤销授权、取消未完成建卡并保留中性退场记录。"
          : "席位会释放，角色卡与剧情历史会归档保留，可按返场流程回来。",
        action === "ban" ? "封禁" : "安全退场",
      );
      if (!ok) return;
    }
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/participant", {
        session_id: sessionId,
        participant_ref: button.dataset.ref,
        action,
        scope: "instance",
        reason: action === "ban" ? "WebUI 副本级封禁" : "WebUI 安全退场",
      });
      toast(
        action === "designate"
          ? "当前行动者已指定"
          : action === "ban"
            ? "封禁与退场已完成"
            : "角色已安全退场",
        "success",
      );
      await loadCore();
      await openSessionDetail(sessionId);
    });
  } else if (action === "grant-role") {
    const userId = $("#permission-user-id").value.trim();
    if (!userId) throw new Error("请填写真实用户 ID");
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/permission", {
        session_id: sessionId,
        user_id: userId,
        role: $("#permission-role").value,
      });
      toast("副本权限已授予", "success");
      await openSessionDetail(sessionId);
    });
  } else if (action === "new-player") {
    $("#session-modal").close();
    openPlayerEditor(sessionId);
  } else if (action === "edit-player") {
    const player = detail.players.find((item) => item.id === button.dataset.id);
    $("#session-modal").close();
    openPlayerEditor(sessionId, player);
  } else if (action?.startsWith("turn-")) {
    const userId = button.dataset.userId;
    const order = (detail.turn?.order || []).map((item) => item.user_id);
    const position = order.indexOf(userId);
    if (action === "turn-add" && position < 0) {
      order.push(userId);
    } else if (action === "turn-remove" && position >= 0) {
      order.splice(position, 1);
    } else if (action === "turn-up" && position > 0) {
      [order[position - 1], order[position]] = [
        order[position],
        order[position - 1],
      ];
    } else if (action === "turn-down" && position >= 0 && position < order.length - 1) {
      [order[position], order[position + 1]] = [
        order[position + 1],
        order[position],
      ];
    } else {
      return;
    }
    await withBusy(button, async () => {
      await bridge.apiPost("sessions/turn-order", {
        session_id: sessionId,
        order,
      });
      toast("多人回合顺序已更新", "success");
      await loadCore();
      await openSessionDetail(sessionId);
    });
  } else if (action === "new-save") {
    $("#session-modal").close();
    openSnapshotEditor(sessionId);
  } else if (action === "restore-save") {
    const snapshot = detail.snapshots.find((item) => item.id === button.dataset.id);
    const ok = await confirmAction(
      `恢复存档「${snapshot?.name || ""}」？`,
      "当前状态会先生成安全快照；恢复后旧分支不会再进入模型上下文，且会话自动暂停。",
      "恢复存档",
    );
    if (!ok) return;
    await bridge.apiPost("snapshots/restore", {
      session_id: sessionId,
      snapshot_ref: button.dataset.id,
    });
    toast("存档已恢复，会话现已暂停", "success");
    await loadCore();
    await openSessionDetail(sessionId);
  } else if (action === "delete-save") {
    const ok = await confirmAction(
      "删除这个存档？",
      "此操作不会改变当前世界状态，但删除后不能再用它恢复。",
      "删除存档",
    );
    if (!ok) return;
    await bridge.apiPost("snapshots/delete", { id: button.dataset.id });
    toast("存档已删除", "success");
    await openSessionDetail(sessionId);
  } else if (action === "delete-independent-save") {
    const filename = button.dataset.filename;
    const ok = await confirmAction(
      `删除独立存档文件「${filename}」？`,
      "文件会移入当前副本的回收目录；最终保护存档不会被允许删除。",
      "删除独立存档",
    );
    if (!ok) return;
    await bridge.apiPost("archives/delete", {
      session_id: sessionId,
      kind: "save",
      filename,
    });
    toast("独立存档文件已移入回收目录", "success");
    await openSessionDetail(sessionId);
  }
}

async function handleMemoryAction(button) {
  const memory = app.memories.find((item) => item.id === button.dataset.id);
  if (!memory) return;
  if (button.dataset.memoryAction === "edit") {
    openMemoryEditor(memory);
  } else {
    const ok = await confirmAction(
      "删除这条长期记忆？",
      "删除后叙事引擎不会再主动检索到这条事实。",
      "删除记忆",
    );
    if (!ok) return;
    await bridge.apiPost("memories/delete", { id: memory.id });
    toast("记忆已删除", "success");
    await loadMemories();
  }
}

function debounceRefresh() {
  window.clearTimeout(app.refreshTimer);
  app.refreshTimer = window.setTimeout(() => {
    loadCore().catch(showError);
    if (app.view === "memories") loadMemories().catch(showError);
    if (app.view === "audit") loadAudit().catch(showError);
  }, 450);
}

async function startSSE() {
  try {
    app.sseId = await bridge.subscribeSSE("events", {
      onOpen() {
        setConnection("live", "实时连接");
      },
      onMessage(event) {
        if (event.parsed?.type !== "keepalive") debounceRefresh();
      },
      onError() {
        setConnection("error", "实时流断开");
      },
    });
  } catch (error) {
    console.warn("SSE unavailable", error);
    setConnection("live", "轮询模式");
  }
}

document.addEventListener("click", async (event) => {
  const nav = event.target.closest("[data-view]");
  if (nav) switchView(nav.dataset.view);
  const jump = event.target.closest("[data-jump]");
  if (jump) switchView(jump.dataset.jump);

  const timePreset = event.target.closest("[data-time-preset]");
  if (timePreset) {
    applyTimePreset(timePreset.dataset.timePreset);
  }

  const openSession = event.target.closest("[data-open-session]");
  if (openSession) {
    try {
      await openSessionDetail(openSession.dataset.openSession);
    } catch (error) {
      showError(error);
    }
  }

  const worldButton = event.target.closest("[data-world-action]");
  if (worldButton) {
    try {
      await handleWorldAction(worldButton);
    } catch (error) {
      showError(error);
    }
  }

  const groupButton = event.target.closest("[data-group-action]");
  if (groupButton) {
    try {
      if (groupButton.dataset.groupAction === "remark") {
        openGroupRemarkEditor(groupButton);
      } else if (groupButton.dataset.groupAction === "token-quota") {
        await openGroupTokenQuotaEditor(groupButton);
      }
    } catch (error) {
      showError(error);
    }
  }

  const characterButton = event.target.closest("[data-character-action]");
  if (characterButton) {
    try {
      await handleCharacterAction(characterButton);
    } catch (error) {
      showError(error);
    }
  }

  const sessionButton = event.target.closest("[data-session-action]");
  if (sessionButton) {
    try {
      const action = sessionButton.dataset.sessionAction;
      if (action === "detail") {
        await openSessionDetail(sessionButton.dataset.id);
      } else {
        await runSessionAction(sessionButton.dataset.id, action, sessionButton);
      }
    } catch (error) {
      showError(error);
    }
  }

  const tab = event.target.closest("[data-session-tab]");
  if (tab) {
    const name = tab.dataset.sessionTab;
    $$("[data-session-tab]").forEach((item) =>
      item.classList.toggle("is-active", item.dataset.sessionTab === name),
    );
    $$("[data-session-tab-panel]").forEach((panel) =>
      panel.classList.toggle(
        "is-active",
        panel.dataset.sessionTabPanel === name,
      ),
    );
  }

  const detailAction = event.target.closest("[data-session-detail-action]");
  if (detailAction) {
    try {
      await handleSessionDetailAction(detailAction);
    } catch (error) {
      showError(error);
    }
  }

  const timerPolicyAction = event.target.closest("[data-timer-policy]");
  if (timerPolicyAction) {
    try {
      const sessionId = app.currentSession?.session?.id;
      if (!sessionId) return;
      await withBusy(timerPolicyAction, async () => {
        await bridge.apiPost("sessions/timer-policy", {
          session_id: sessionId,
          timer_type: timerPolicyAction.dataset.timerPolicy,
          enabled: timerPolicyAction.dataset.enabled === "true",
        });
        toast("倒计时开关已更新", "success");
        await openSessionDetail(sessionId);
      });
    } catch (error) {
      showError(error);
    }
  }

  const timerAction = event.target.closest("[data-timer-action]");
  if (timerAction) {
    try {
      let seconds = 0;
      if (timerAction.dataset.timerAction === "extend") {
        const raw = window.prompt("延长多少秒？", "1800");
        if (raw === null) return;
        seconds = Number(raw);
        if (!Number.isInteger(seconds) || seconds <= 0) {
          throw new Error("延长秒数必须是正整数");
        }
      }
      if (timerAction.dataset.timerAction === "expire") {
        const ok = await confirmAction(
          "立即触发这个计时器？",
          "系统会立刻按当前超时规则处理对应玩家或投票。",
          "立即到期",
        );
        if (!ok) return;
      }
      await withBusy(timerAction, async () => {
        await bridge.apiPost("sessions/timer", {
          timer_id: timerAction.dataset.id,
          action: timerAction.dataset.timerAction,
          seconds,
        });
        toast("计时器已更新", "success");
        await openSessionDetail(app.currentSession.session.id);
      });
    } catch (error) {
      showError(error);
    }
  }

  const memoryButton = event.target.closest("[data-memory-action]");
  if (memoryButton) {
    try {
      await handleMemoryAction(memoryButton);
    } catch (error) {
      showError(error);
    }
  }

  const fallbackButton = event.target.closest("[data-fallback-action]");
  if (fallbackButton) {
    const values = [
      ...$("#fallback-provider-list").querySelectorAll(
        "[data-fallback-provider]",
      ),
    ].map((item) => item.value);
    const index = Number(fallbackButton.dataset.index);
    const action = fallbackButton.dataset.fallbackAction;
    if (action === "remove") {
      values.splice(index, 1);
    } else if (action === "up" && index > 0) {
      [values[index - 1], values[index]] = [
        values[index],
        values[index - 1],
      ];
    } else if (action === "down" && index < values.length - 1) {
      [values[index], values[index + 1]] = [
        values[index + 1],
        values[index],
      ];
    }
    renderFallbackProviders(values);
    $("#settings-dirty-text").textContent = "存在尚未保存的更改";
  }
});

$("#menu-toggle").addEventListener("click", () => {
  $("#sidebar").classList.toggle("is-open");
});

$("#refresh-button").addEventListener("click", async (event) => {
  try {
    await withBusy(event.currentTarget, async () => {
      await loadCore();
      if (app.view === "memories") await loadMemories();
      if (app.view === "audit") await loadAudit();
    });
    toast("数据已刷新", "success");
  } catch (error) {
    showError(error);
  }
});

$("#show-archived-worlds").addEventListener("change", async () => {
  try {
    const result = await bridge.apiGet("worlds", {
      include_archived: $("#show-archived-worlds").checked,
    });
    app.worlds = result.items || [];
    renderWorlds();
  } catch (error) {
    showError(error);
  }
});

$("#session-search").addEventListener("input", (event) => {
  window.clearTimeout(app.sessionSearchTimer);
  const value = event.currentTarget.value;
  app.sessionSearchTimer = window.setTimeout(async () => {
    app.sessionQuery = value.trim();
    app.sessionPage = 1;
    try {
      await loadSessionPage();
    } catch (error) {
      showError(error);
    }
  }, 280);
});
$("#session-search-scope").addEventListener("change", async (event) => {
  app.sessionScope = event.currentTarget.value;
  app.sessionPage = 1;
  try {
    await loadSessionPage();
  } catch (error) {
    showError(error);
  }
});
$("#session-search-clear").addEventListener("click", async () => {
  $("#session-search").value = "";
  $("#session-search-scope").value = "all";
  app.sessionQuery = "";
  app.sessionScope = "all";
  app.sessionPage = 1;
  try {
    await loadSessionPage();
  } catch (error) {
    showError(error);
  }
});
$("#session-page-prev").addEventListener("click", async () => {
  if (app.sessionPage <= 1) return;
  app.sessionPage -= 1;
  try {
    await loadSessionPage();
  } catch (error) {
    app.sessionPage += 1;
    showError(error);
  }
});
$("#session-page-next").addEventListener("click", async () => {
  if (app.sessionPage >= app.sessionPages) return;
  app.sessionPage += 1;
  try {
    await loadSessionPage();
  } catch (error) {
    app.sessionPage -= 1;
    showError(error);
  }
});

$("#new-world-button").addEventListener("click", () => openWorldEditor());
$("#import-world-package-button").addEventListener("click", openWorldPackageImportDialog);
$("#import-npc-button").addEventListener("click", openNpcImportDialog);
$("#new-session-button").addEventListener("click", openSessionCreator);
$("#new-memory-button").addEventListener("click", () => openMemoryEditor());
$("#add-fallback-provider").addEventListener("click", () => {
  const values = [
    ...$("#fallback-provider-list").querySelectorAll(
      "[data-fallback-provider]",
    ),
  ].map((item) => item.value);
  if (values.length >= 4) {
    toast("原生配置兼容模式最多提供 4 个备用模型", "error");
    return;
  }
  const primary = $("#setting-provider").value;
  const candidate = app.providers.find(
    (item) => item.id !== primary && !values.includes(item.id),
  );
  values.push(candidate?.id || "");
  renderFallbackProviders(values);
  $("#settings-dirty-text").textContent = "存在尚未保存的更改";
});
$("#memory-session-select").addEventListener("change", () =>
  loadMemories().catch(showError),
);
$("#memory-search").addEventListener("input", () => {
  window.clearTimeout(app.memoryTimer);
  app.memoryTimer = window.setTimeout(() => loadMemories().catch(showError), 280);
});
$("#audit-session-select").addEventListener("change", () =>
  loadAudit().catch(showError),
);
$("#audit-refresh-button").addEventListener("click", () =>
  loadAudit().catch(showError),
);

$("#editor-modal-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (event.submitter?.id !== "editor-save-button") return;
  if (!app.editorSave) {
    $("#editor-modal").close();
    return;
  }
  const button = $("#editor-save-button");
  try {
    await withBusy(button, app.editorSave);
    $("#editor-modal").close();
  } catch (error) {
    showError(error);
  }
});

function closeEditor() {
  app.editorSave = null;
  $("#editor-modal").close("cancel");
}

$("#editor-modal-close").addEventListener("click", closeEditor);
$("#editor-cancel-button").addEventListener("click", closeEditor);
$("#editor-modal").addEventListener("cancel", () => {
  app.editorSave = null;
});
$("#editor-modal").addEventListener("close", () => {
  app.editorSave = null;
});

$("#session-modal-close").addEventListener("click", () =>
  $("#session-modal").close(),
);

$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  try {
    await withBusy(button, async () => {
      const result = await bridge.apiPost("settings/save", collectSettings());
      app.settings = result.settings;
      toast("设置已保存并立即生效", "success");
      await loadCore();
    });
  } catch (error) {
    showError(error);
  }
});

$("#settings-form").addEventListener("input", () => {
  $("#settings-dirty-text").textContent = "存在尚未保存的更改";
});

$("#export-backup-button").addEventListener("click", async (event) => {
  try {
    await withBusy(event.currentTarget, async () => {
      await bridge.download(
        "backup/export",
        {},
        "backup_tavern_v0.9.0.zip",
      );
    });
    toast("备份已生成", "success");
  } catch (error) {
    showError(error);
  }
});

$("#import-backup-file").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  const mode = $("#import-mode").value;
  try {
    if (mode === "replace") {
      const ok = await confirmAction(
        "覆盖全部酒馆数据？",
        "现有世界、会话、记忆、存档与审计会被备份文件替换。请确保已经另行导出当前数据。",
        "确认覆盖",
      );
      if (!ok) return;
    }
    await bridge.upload(`backup/import/${mode}`, file);
    toast("备份导入完成", "success");
    await loadCore();
  } catch (error) {
    showError(error);
  } finally {
    event.target.value = "";
  }
});

window.addEventListener("beforeunload", () => {
  if (app.sseId) bridge.unsubscribeSSE(app.sseId);
  if (app.contextOff) app.contextOff();
});

function _readImportSource(fileInput, text) {
  return new Promise((resolve, reject) => {
    if (fileInput.files && fileInput.files[0]) {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("文件读取失败"));
      reader.readAsText(fileInput.files[0]);
    } else {
      resolve(text);
    }
  });
}

function openWorldPackageImportDialog() {
  const body = `
    <p class="field-hint">导入<b>世界包</b> JSON（必须包含 <code>slug</code> / <code>name</code> / <code>system_prompt</code>）。按 <code>slug</code> 新建或更新世界，导入后会<strong>直接打开该世界</strong>。</p>
    <div class="field">
      <label for="wp-import-file">世界包 JSON 文件</label>
      <input type="file" id="wp-import-file" accept=".json,application/json" />
    </div>
    <div class="field">
      <label for="wp-import-text">或粘贴 JSON 文本</label>
      <textarea id="wp-import-text" rows="12" placeholder='将世界包 JSON 粘贴到这里…'></textarea>
    </div>
    <div class="import-actions">
      <button type="button" class="button button-primary" id="wp-import-submit">导入并创建世界</button>
      <button type="button" class="action-button" id="wp-import-cancel">取消</button>
    </div>
    <div class="import-result" id="wp-import-result" hidden></div>
  `;
  openEditor({ title: "导入世界包", kicker: "WORLD IMPORT", body });

  $("#wp-import-cancel").addEventListener("click", () => $("#editor-modal").close());
  $("#wp-import-submit").addEventListener("click", async () => {
    const resultEl = $("#wp-import-result");
    resultEl.hidden = true;
    resultEl.className = "import-result";
    const raw = await _readImportSource($("#wp-import-file"), $("#wp-import-text").value.trim());
    try {
      if (!raw) throw new Error("请选择 JSON 文件或在文本框中粘贴内容");
      let parsed;
      try {
        parsed = JSON.parse(raw);
      } catch (parseError) {
        throw new Error(`JSON 解析失败：${parseError.message}`);
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("世界包必须是包含 slug / name / system_prompt 的 JSON 对象");
      }
      const submit = $("#wp-import-submit");
      submit.disabled = true;
      try {
        const resp = await bridge.apiPost("worlds/import", parsed);
        const world = resp?.item || (app.worlds || []).find((w) => w.slug === parsed.slug);
        toast(resp?.mode === "updated" ? "世界包已更新" : "世界包已导入并创建世界", "success");
        await loadCore();
        if (world) {
          openWorldEditor(world);
        } else {
          $("#editor-modal").close();
        }
      } finally {
        submit.disabled = false;
      }
    } catch (error) {
      resultEl.hidden = false;
      resultEl.classList.add("is-error");
      resultEl.textContent = error?.message || String(error);
      showError(error);
    }
  });
}

function openNpcImportDialog() {
  const body = `
    <p class="field-hint">导入<b>常驻 NPC</b>：JSON 为角色数组，或包含 <code>items</code> / <code>npcs</code> / <code>characters</code> 字段的对象。将批量写入下方选择的目标世界。</p>
    <div class="field">
      <label for="np-import-file">常驻 NPC JSON 文件</label>
      <input type="file" id="np-import-file" accept=".json,application/json" />
    </div>
    <div class="field">
      <label for="np-import-text">或粘贴 JSON 文本</label>
      <textarea id="np-import-text" rows="12" placeholder='将常驻 NPC 的 JSON 粘贴到这里…'></textarea>
    </div>
    <div class="field">
      <label for="np-import-world">目标世界</label>
      <select id="np-import-world"></select>
    </div>
    <div class="import-actions">
      <button type="button" class="button button-primary" id="np-import-submit">导入</button>
      <button type="button" class="action-button" id="np-import-cancel">取消</button>
    </div>
    <div class="import-result" id="np-import-result" hidden></div>
  `;
  openEditor({ title: "导入常驻 NPC", kicker: "NPC IMPORT", body });

  const worldSelect = $("#np-import-world");
  worldSelect.innerHTML = (app.worlds || [])
    .map(
      (w) =>
        `<option value="${escapeHTML(w.id)}">${escapeHTML(w.name)}（${escapeHTML(w.slug)}）</option>`,
    )
    .join("");
  if (!app.worlds.length) {
    worldSelect.innerHTML =
      '<option value="">（暂无世界，请先导入或新建世界）</option>';
  }

  $("#np-import-cancel").addEventListener("click", () => $("#editor-modal").close());
  $("#np-import-submit").addEventListener("click", async () => {
    const resultEl = $("#np-import-result");
    resultEl.hidden = true;
    resultEl.className = "import-result";
    const raw = await _readImportSource($("#np-import-file"), $("#np-import-text").value.trim());
    try {
      if (!raw) throw new Error("请选择 JSON 文件或在文本框中粘贴内容");
      let parsed;
      try {
        parsed = JSON.parse(raw);
      } catch (parseError) {
        throw new Error(`JSON 解析失败：${parseError.message}`);
      }
      const worldId = worldSelect.value;
      if (!worldId) throw new Error("请选择目标世界");
      const payload = Array.isArray(parsed)
        ? { world_id: worldId, items: parsed }
        : { world_id: worldId, ...parsed };
      if (
        !Array.isArray(payload.items) &&
        !Array.isArray(payload.npcs) &&
        !Array.isArray(payload.characters)
      ) {
        throw new Error(
          "常驻 NPC 必须是数组，或包含 items / npcs 字段的对象",
        );
      }
      const submit = $("#np-import-submit");
      submit.disabled = true;
      try {
        const res = await bridge.apiPost("characters/import", payload);
        const count = res?.created ?? (res?.items || []).length;
        toast(`已导入 ${count} 个常驻角色`, "success");
        await loadCore();
        await openCharacterManager(worldId);
        $("#editor-modal").close();
      } finally {
        submit.disabled = false;
      }
    } catch (error) {
      resultEl.hidden = false;
      resultEl.classList.add("is-error");
      resultEl.textContent = error?.message || String(error);
      showError(error);
    }
  });
}

async function boot() {
  try {
    app.context = await bridge.ready();
    applyBridgeContext(app.context);
    if (typeof bridge.onContext === "function") {
      app.contextOff = bridge.onContext((context) => {
        app.context = context;
        applyBridgeContext(context);
      });
    }
    await loadCore();
    await startSSE();
  } catch (error) {
    setConnection("error", "连接失败");
    showError(error);
  }
}

boot();
