# 建设交通干线节日保障接力台账基础服务

本项目提供节日公共服务场景的通用后台基础层，负责组织、服务站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。在此之上，`traffic` 领域模块实现交通干线节日保障的**接力台账**：接收结构化风险上报、控制措施、资源回执与复查结果，按事件时间线归并多班人员的连续处置，回答“道路现在什么状态、资源去了哪里、每个决定依据哪条消息”。

## 接力台账规则

- **不可覆盖的修订链**：同一现场（`site_id + scene_key`，最多一个进行中事件）的所有消息按到达顺序追加为哈希串联条目；重复 `message_id` 回放原结果，同号不同内容冲突；`event_time` 早于时间线水位的标记 `late=true`，但仍作为证据留存。
- **封控与前置条件**：`enforce` 可叠加多种措施（车道封控、设备占用等），每个前置条件只允许登记一次，修订链不允许覆盖。
- **责任租约**：领取任务获得带 `expires_at` 的租约，租约期间他人不能抢占；转交生成新租约并附带上一任 ID 与**截至转交时尚未完成的条件**；过期后可被他人重新领取。
- **独立复核解除**：每条前置条件都必须由 `reviewer` 角色、且非封控设置人的独立复核者确认；解除请求在仍有未确认项时返回结构化 `blockers`。
- **受保护终态**：事件关闭后到达的消息（即使时间戳很新）只落链为 `terminal_protected`，不会重开道路封控，关闭同时释放占用资源与未结束租约。
- **候选调配**：资源不足时生成带每条理由与受限候选（被占用/跨场所）的方案；负责人必须携带方案所基于的 `state_version` 整体 `execute`/`escalate`；执行时任一资源已不再空闲则整笔事务回滚，不留下任何部分占用。
- **可解释查询**：事件解释接口返回道路状态、未满足条件、租约链、资源去向（含每条 movement 与来源条目）、调配方案版本，以及每个决定引用的消息证据（message_id/哈希）。

## 目录

- `src/festival_foundation/`：基础模型、SQLite 存储、权限服务、审计链、交通接力台账（`traffic.py`）、HTTP 路由和离线验收；
- `tests/`：基础规则、接力时间线、租约、调配事务、接口路由、持久化与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m festival_foundation.acceptance
PYTHONPATH=src python3 -m festival_foundation.traffic_acceptance
```

基础验收核对登记、幂等回执与审计链；接力验收跑通“上报 → 封控 → 租约接力 → 资源不足升级 → 独立复核解除 → 迟到消息被终态保护”的完整链路，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m festival_foundation.api --database festival_foundation.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态、审计链与未完接力（进行中事件、未到期租约、资源占用、待确认条件）继续保留。

### 接力台账接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /traffic/incidents` | 结构化风险上报，建立事件与时间线首条 |
| `POST /traffic/messages` | 追加 `control_measure` / `resource_receipt` / `review_result` 消息 |
| `POST /traffic/leases/claim` | 领取会过期的责任租约（`ttl_seconds`，默认 1800） |
| `POST /traffic/leases/transfer` | 转交租约，回执携带尚未完成的条件 |
| `POST /traffic/resources` | 登记清障车、救护车等可占用资源 |
| `POST /traffic/allocation-plans` | 生成带理由的候选调配方案（可行/不可行及候选明细） |
| `POST /traffic/allocation-plans/decide` | 负责人按 `state_version` 整体确认（`execute`/`escalate`） |
| `GET /traffic/incidents/{id}` | 道路状态、租约链、资源去向、时间线与决策证据解释 |
| `GET /traffic/road-status?site_id=` | 场所下事件的道路状态总览 |
| `GET /traffic/resources/{id}/trace` | 单个资源的完整去向轨迹 |

所有写入都要求 `request_id` 幂等编号；重复请求返回同一回执并标注 `replayed`。
