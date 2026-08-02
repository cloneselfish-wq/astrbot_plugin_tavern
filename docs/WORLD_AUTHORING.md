# v0.9.0 世界包编写说明

世界包是可复用模板。新建副本时会复制世界版本、规则与时间规则快照；之后修改模板不会静默改变正在运行的团。

## 顶层字段

| 字段 | 作用 |
|---|---|
| `slug` | 唯一标识；小写字母、数字、`_`、`-`，最多 64 字符 |
| `name` | 显示名称 |
| `description` | 题材、规模与玩法简介 |
| `system_prompt` | 稳定世界规律、能力边界与叙事语气 |
| `opening_scene` | 第一次开演时的开场 |
| `rules` | 人数、角色卡、检定、内容边界、NPC、事件与时间规则 |
| `initial_state` | 新副本的地点、时间、摘要、事实、物品与关系 |

## 规则骨架

```json
{
  "resolution": "d20",
  "default_difficulty": 12,
  "difficulty_min": 5,
  "difficulty_max": 25,
  "strict_choices": true,
  "check_density": "standard",
  "player_limits": {},
  "character_card": {},
  "dice_rules": {},
  "inspiration": {},
  "content_boundaries": {},
  "progress": {},
  "npc_policy": {},
  "context_budget": {},
  "time_rules": {},
  "opening_choices": [],
  "event_pool": [],
  "safe_exit_templates": [],
  "return_rules": {}
}
```

世界 JSON 是声明式数据，不能包含脚本或可执行表达式。

## 人数

```json
{
  "player_limits": {
    "recommended_min": 2,
    "recommended_max": 4,
    "minimum_start": 2,
    "maximum": 4
  }
}
```

`recommended_min/max` 只用于展示；`minimum_start` 是开演前置检查；`maximum` 是数据库强制席位上限，范围 `1..32`。

## 玩家角色卡

角色卡模板在管理台中拥有独立入口：

```text
酒馆控制台 → 世界与角色 → 对应世界卡片 → 角色卡模板
```

该入口支持导入 JSON、导出 JSON、结构校验、玩家建卡表单预览、恢复默认和确认保存。导入与编辑不会即时生效，只有通过校验并点击保存后才会更新世界模板。它不应与常驻 NPC 管理混为一体。

```json
{
  "character_card": {
    "version": 1,
    "auto_approve": false,
    "edit_requires_review": true,
    "fields": [
      {
        "key": "name",
        "label": "角色姓名",
        "required": true,
        "private": false,
        "max_chars": 40,
        "type": "text"
      },
      {
        "key": "code",
        "label": "副本代号",
        "required": true,
        "private": false,
        "max_chars": 20,
        "type": "text"
      },
      {
        "key": "belief",
        "label": "核心信念",
        "required": true,
        "private": false,
        "max_chars": 300,
        "type": "text"
      },
      {
        "key": "secret",
        "label": "私人秘密",
        "required": false,
        "private": true,
        "max_chars": 600,
        "type": "text"
      }
    ],
    "stats": {
      "budget": 10,
      "attributes": [
        {
          "key": "body",
          "label": "体魄",
          "minimum": 0,
          "maximum": 5,
          "default": 2
        },
        {
          "key": "agility",
          "label": "敏捷",
          "minimum": 0,
          "maximum": 5,
          "default": 2
        }
      ],
      "modifier_table": {
        "0": -3,
        "1": -2,
        "2": -1,
        "3": 0,
        "4": 1,
        "5": 2
      }
    }
  }
}
```

校验规则：

- `version` 必须是正整数。
- `fields` 的 `key` 不可为空或重复，并必须包含 `name` 与 `code`。
- 属性 `key` 不可重复。
- 属性默认值必须在最小值与最大值之间。
- 总预算必须介于全部属性最小值之和与最大值之和之间。
- 插件会为属性自动补充 `stat_<key>` 建卡步骤。

建议字段包含背景、目标、信念、羁绊、专长、缺陷、弱点、知识边界、私人秘密和内容边界。

## 检定规则

```json
{
  "dice_rules": {
    "system": "d20",
    "advantage": "2d20_keep_high",
    "disadvantage": "2d20_keep_low",
    "stacking": false,
    "opposites_cancel": true,
    "outcome_bands": true,
    "visibility": "public"
  },
  "inspiration": {
    "initial": 1,
    "maximum": 3,
    "uses": ["advantage_before_roll", "reroll_full_pool"]
  }
}
```

`visibility` 可为：

- `public`：显示骰池、取值、加值、DC、来源和结果。
- `immersive`：隐藏具体 DC。
- `hidden`：群内只显示叙事，完整数据留在后台。

建议内部测试固定使用 `public`。

## 风险与难度

DC 只表示成功难度：

| DC | 难度 |
|---:|---|
| 5 | 极易 |
| 8 | 容易 |
| 10 | 普通 |
| 12 | 标准 |
| 15 | 困难 |
| 18 | 非常困难 |
| 20 | 极限 |
| 25 | 传奇 |

风险只表示失败后果：

- `safe`
- `controlled`
- `dangerous`
- `desperate`
- `lethal`

`lethal` 必须提前填写玩家可见后果。不要让同一个原因同时提高 DC 和造成劣势。

## 正式进度

```json
{
  "progress": {
    "chapter": "第一章：暴雪来客",
    "current_objective": "调查酒馆地下的异响",
    "completed_milestones": 2,
    "total_milestones": 8
  }
}
```

只有 `total_milestones > 0` 时管理台才显示百分比。没有正式里程碑时只展示章节、目标和场景摘要。

## 内容边界

```json
{
  "content_boundaries": {
    "character_death": "ask",
    "player_conflict": "consent",
    "romance": "fade_to_black",
    "horror": "moderate",
    "sexual_content": "blocked",
    "safety_pause": true
  }
}
```

世界规则可在副本详情中覆盖。安全暂停会冻结全部计时，且玩家不必公开原因。

## 自动 NPC

```json
{
  "npc_policy": {
    "enabled": true,
    "max_new_per_turn": 3,
    "generated_requires_review": true,
    "archive_after_inactive_rounds": 12
  },
  "context_budget": {
    "recent_turns": 12,
    "memories": 10,
    "active_npcs": 12,
    "ledger_items": 16
  }
}
```

模型生成 NPC 必须有名字，并至少满足直接互动、掌握重要线索或写入长期记忆之一。自动 NPC 只能写公开资料、已知事实、误解和运行状态，不能创建系统级私密提示词。

## 开场四选一

```json
{
  "opening_choices": [
    {
      "key": "A",
      "text": "谨慎观察现场",
      "risk": "safe",
      "requires_check": false,
      "collective": false
    },
    {
      "key": "B",
      "text": "询问公开信息",
      "risk": "safe",
      "requires_check": false,
      "collective": false
    },
    {
      "key": "C",
      "text": "借助绳索翻越断桥",
      "risk": "desperate",
      "requires_check": true,
      "collective": false,
      "check_type": "standard",
      "check_stat": "敏捷",
      "difficulty": 15,
      "known_consequences": "失败可能坠落并与队伍分离",
      "advantage_sources": ["装备：绳索"],
      "disadvantage_sources": []
    },
    {
      "key": "D",
      "text": "带领全队离开当前区域",
      "risk": "controlled",
      "requires_check": false,
      "collective": true
    }
  ]
}
```

必须恰好包含 A、B、C、D，且至少一项为 `safe`。选项只能声明行动意图，不能提前保证结果。影响全队的行为必须设为 `collective: true`。

## 世界脉冲事件

```json
{
  "event_pool": [
    {
      "id": "late-courier",
      "title": "迟到的信使",
      "description": "负伤信使带来一条新线索和可响应威胁。",
      "weight": 2,
      "minimum_round": 2,
      "cooldown_rounds": 4,
      "once": true,
      "severity": "standard",
      "conditions": {
        "locations": ["边境无名酒馆·大厅"],
        "required_facts": ["酒馆保持中立"],
        "excluded_facts": ["信使事件已经解决"],
        "minimum_players": 2,
        "maximum_players": 4
      }
    }
  ]
}
```

每个完整多人轮次最多抽取一次。抽取结果先落库，模型失败重试不会换事件。

## 时间规则

JSON 内部仍以秒保存；`null` 或 `-1` 表示不限时，`0` 非法。管理台可直接选择秒、分钟、小时或天。

```json
{
  "time_rules": {
    "card_code_ttl_seconds": 1800,
    "card_draft_ttl_seconds": 604800,
    "card_completion_timeout_seconds": 86400,
    "preparation_timeout_seconds": 86400,
    "ready_timeout_seconds": 1800,
    "turn_timeout_seconds": 600,
    "turn_reminder_seconds": 180,
    "max_consecutive_timeouts": 2,
    "standby_timeout_seconds": 604800,
    "delegation_ttl_seconds": 86400,
    "check_timeout_seconds": 300,
    "vote_round_one_seconds": 600,
    "vote_round_two_seconds": 300,
    "vote_reminder_seconds": 120,
    "all_idle_pause_seconds": 600,
    "pause_stops_clock": true,
    "announce_timeouts": true,
    "turn_timeout_action": "skip",
    "card_timeout_action": "standby",
    "ready_timeout_action": "standby"
  }
}
```

`max_consecutive_timeouts: -1` 表示永不自动转候补。模型请求和数据库锁属于技术超时，不能设为不限时。

## 安全退场与返场

```json
{
  "safe_exit_templates": [
    "{character}去追查一条只能由其本人确认的线索，并留下未来重新联络的记号。"
  ],
  "return_rules": {
    "allow_return": true,
    "allow_resurrection": false,
    "requires_vote": true,
    "requires_story_condition": true
  }
}
```

模板必须中性，不羞辱、不擅自杀死角色，也不消耗团队公共物品。

## 初始状态

```json
{
  "location": "边境无名酒馆·大厅",
  "time": "雨夜",
  "scene_summary": "酒馆刚开门，尚无事件发生。",
  "facts": ["酒馆保持中立"],
  "inventory": {},
  "relationships": {},
  "check_modifiers": {
    "advantages": [],
    "disadvantages": []
  }
}
```

`check_modifiers` 可登记当前场景中明确、可验证的优劣势来源。模型不能直接改写权限、角色卡、会话阶段、骰点、投票或计时规则。

## 发布前检查

- 人数最小值与上限是否合理？
- 角色卡是否包含 `name`、`code`、背景、目标、信念、专长与知识边界？
- 属性预算、区间和加值表是否一致？
- A—D 是否只表达行动意图？
- 是否至少有一个安全选项？
- DC、风险和优劣势是否分别承担不同作用？
- 致命风险是否提前明示后果？
- 集体行为是否标记 `collective`？
- 自动 NPC 和上下文预算是否适合长期副本？
- 内容边界与安全暂停是否符合本团约定？
- 是否演练优势、劣势、灵感、集体检定、投票、超时、存档、克隆和模型失败？
