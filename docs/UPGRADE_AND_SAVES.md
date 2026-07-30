# v0.5.1 Alpha 升级与存档说明

## 结论

v0.1—v0.5.0 Beta 的 SQLite 数据可以直接升级到 v0.5.1 Alpha，不需要清空旧存档。插件会先建立一致迁移备份，再升级到 Schema 5，并为每个群、每轮故事副本建立独立目录与数据库。

以下数据继续保留：

- 世界包、常驻 NPC 和角色卡模板
- 群会话与命名副本
- 玩家资料、角色卡版本和副本运行状态
- 剧情时间线、长期记忆和审计记录
- 命名存档、自动快照和单回合回滚点
- 0.4 的准备阵容、选项、投票、计时、代控、封禁与返场流程

0.5.1 新增的群备注、目录索引和副本文件同步状态会以安全默认值补齐；0.5.0 的副本规则状态、动态 NPC、账本、时钟、记忆治理、灵感及操作回执全部保留。

## Schema 5 存储布局

```text
astrbot_plugin_tavern/
├─ catalog.sqlite3
├─ groups/
│  └─ <平台>_g_<稳定群哈希>/
│     ├─ group.json
│     └─ stories/
│        └─ <故事标识>_<创建时间>_i-<副本ID>/
│           ├─ manifest.json
│           ├─ instance.sqlite3
│           ├─ saves/
│           └─ backups/
├─ exports/
└─ migration_backups/
```

- `catalog.sqlite3` 是全局目录和兼容事务协调库。
- 每轮故事副本都有自包含 `instance.sqlite3`，只保留该副本及其使用的世界与角色版本。
- `group.json` 保存群 ID、平台和群备注；目录名不使用备注。
- `manifest.json` 保存副本 ID、创建时间、第几次游玩、世界快照、来源分支、校验值和记录数。
- 副本目录中的创建时间永不修改；重新游玩会生成新的副本 ID 和目录。

## 两层存档

| 层级 | 位置 | 用途 |
|---|---|---|
| 回合级快速恢复点 | `instance.sqlite3` 内部 | 单回合回滚、自动检查点、操作前保护、快速读档 |
| 独立恢复文件 | `saves/`、`backups/` | 手动存档、最终存档、周期安全备份与灾难恢复 |

独立文件统一使用十四位数字时间，例如：

```text
save_border-tavern_20260729142300.zip
backup_border-tavern_20260729143000.zip
```

同一秒内再次生成时追加 `_02`、`_03`。每个 ZIP 都包含 `instance.sqlite3`、`manifest.json`、`group_snapshot.json` 和 `checksum.sha256`。

## 升级后的状态变化

- Schema 1/2 的旧 `running` 或 `maintenance` 副本仍按 0.4 规则安全迁移为 `paused`。
- Schema 3 的 0.4 运行副本保留现有选项、投票和计时流程。
- 0.4 中已经是 `finished` 的副本会迁移为永久只读归档；若没有存档，迁移会创建最终保护存档。
- 旧检定记录按“常规单骰、无优劣势来源”理解，不会重写历史结果。

如果 0.4 副本正在模型请求中，建议升级前先暂停，并等待当前请求结束。0.5 只能为升级后新建或重新进入的检定生成持久操作回执。

## 备份位置

全局目录库：

```text
AstrBot/data/plugin_data/astrbot_plugin_tavern/catalog.sqlite3
```

迁移前自动备份：

```text
AstrBot/data/plugin_data/astrbot_plugin_tavern/migration_backups/
backup_catalog_YYYYMMDDHHMMSS.sqlite3
backup_legacy_tavern_YYYYMMDDHHMMSS.sqlite3
```

管理台可以导出 `backup_tavern_YYYYMMDDHHMMSS.zip`，其中包含目录库、群／副本目录、独立存档、校验清单和兼容 `bundle.json`。升级前同时保留“停机数据目录备份＋完整 ZIP 导出”最稳妥。

## 推荐升级步骤

1. 暂停正在运行的团，并等待当前模型请求结束。
2. 停止 AstrBot。
3. 复制整个 `AstrBot/data/plugin_data/astrbot_plugin_tavern/`。
4. 保存旧版插件 ZIP 或代码目录。
5. 安装 v0.5.1 Alpha ZIP。
6. 启动 AstrBot，确认日志中的迁移备份路径。
7. 打开酒馆控制台，检查配置修订、模型选择器和模型链健康。
8. 逐一检查世界、角色卡模板、群会话卡片、阵容、NPC、记忆和存档。
9. 在测试群完成一次普通检定、优势检定、投票、暂停和存档恢复。
10. 确认无误后再恢复长期副本。

## `finished` 的新语义

0.5 的完结不是普通暂停：

- 自动建立最终保护存档。
- 取消选项、投票和全部计时器。
- 撤销代控与副本权限。
- 解除当前群活动绑定。
- 保存结束时间、操作者、类型和原因。
- 副本永久只读。

需要续作时，从最终或指定存档克隆新副本。不要直接修改已完结副本。

## 回退到旧版

旧版插件不能读取 Schema 5。要完整回退，必须同时恢复：

1. 旧版插件代码；以及
2. 升级前复制的整个数据目录，或 `backup_catalog_*.sqlite3` / `backup_legacy_tavern_*.sqlite3`。

不要只降级代码并继续使用 Schema 5 数据库。

推荐停机恢复。替换单个 `.sqlite3` 前，应一并移走同名 `-wal` 和 `-shm`，避免把另一份数据库的 WAL 错配到恢复文件。

## ZIP 与 JSON 备份兼容

- Schema 1—4 JSON 备份仍可导入，并自动补齐 Schema 5 的目录索引。
- Schema 5 完整 ZIP 内含 `bundle.json`，继续使用既有安全合并与覆盖恢复规则。
- Schema 5 导出额外包含群备注、目录清单和副本同步状态。
- 导入 ZIP 时会先校验全部 SHA-256、拒绝路径穿越、符号链接、重复成员和异常解压体积，再提交数据库恢复。
- 活动副本数据库与清单由 `bundle.json` 重新物化；`saves/` 和 `backups/` 中的独立恢复文件也会一并还原。
- 备份内记录的 `relative_path` 不会被直接采用；物理目录始终由受校验的群与副本身份重新计算。
- 来自更新 Schema 的备份会被拒绝。
- 安全合并是插入式合并；同一稳定身份指向不同对象时停止，不静默覆盖。
- 覆盖恢复会替换当前数据，操作前务必另行导出。

## 哪些情况会停止升级或导入

- SQLite 文件损坏
- 文件或目录不可写
- 磁盘空间不足，无法创建迁移备份
- 备份来自更新 Schema
- 手工改库造成唯一键或外键冲突
- JSON 备份的同一稳定 ID 指向不同对象
- 声称为 Schema 5 的备份缺少群目录或副本存储索引表

迁移备份失败后，插件不会继续修改旧数据库。

## QQ 官方私聊说明

群成员标识与私聊用户标识不能假定相同，且用户可能关闭机器人主动消息。一次性验证码仍是可靠绑定方式：

```text
/酒馆 建卡 <验证码>
```

建卡草稿和步骤均持久化，Bot 重载不会清空进行中的角色卡。
