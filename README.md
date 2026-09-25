# 建设交通干线节日保障接力台账基础服务

本项目提供节日公共服务场景的通用后台基础层，负责组织、服务站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。在此之上，`relay` 模块实现**交通干线节日保障接力台账**：

- 接收结构化风险上报、控制措施、资源回执与复查结果，同一现场（场所+路段）的处置归并为一条只追加、哈希串联、**不可覆盖的修订链**；
- 按事件时间判定乱序（`late`）与重复（`message_id` 去重）消息；
- 值守人员领取**会过期的责任租约**，转交必须附带尚未完成的条件；
- 封控只能由**独立复核者**在全部前置项满足后解除，迟到消息只留档，**不得重新打开** `resolved`/`closed` 受保护终态；
- 资源不足时形成**带理由的候选调配**（空闲直接预留 / 终结现场回收借用 / 缺口需外部增援），由负责人按**状态版本整体确认**，任一锁定失败整笔回滚，不留部分占用；
- 查询接口解释当前道路状态、资源去向与每个决定使用的证据；
- 未完接力保存在 SQLite 中，服务重启后时间线、租约与审计链继续可用。

## 目录

- `src/festival_foundation/`：领域模型、SQLite 存储、权限服务、审计链、接力台账（`relay.py`）、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由、接力台账规则和端到端验收测试。

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
```

验收命令会在临时 SQLite 数据库中登记组织、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m festival_foundation.api --database festival_foundation.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

## 接力台账接口

所有写接口沿用 `request_id` 幂等（同号重放返回 `replayed: true`），并以请求头 `X-Actor-Id` 标识操作者（`operator` 处置、`reviewer` 复核、`admin` 两者皆可）。

| 方法与路径 | 说明 |
| --- | --- |
| `POST /relay/risks` | 结构化风险上报，同一场所+路段的未结事件自动归并，按风险类型开启封控/清障/疏导/资源/复查前置条件 |
| `POST /relay/actions` | 处置上报：`road_closed`、`clearance_done`、`crowd_guided`、`resource_receipt`、`review_recorded` 等；乱序消息标记 `ordering=late`，终态后消息 `applied=false` 仅留档 |
| `POST /relay/leases/claim` | 领取会过期的责任租约（`ttl_seconds`，默认 90 分钟） |
| `POST /relay/leases/transfer` | 转交租约，必须存在尚未完成的条件，回执携带条件清单，旧租约失效 |
| `POST /relay/allocations/plan` | 生成带理由的候选调配，不产生占用；含缺口时状态为 `infeasible` |
| `POST /relay/allocations/confirm` | 按 `expected_version` 整体确认，版本不符或任一资源锁定失败则全部不占用 |
| `POST /relay/allocations/acknowledge` | 现场资源到位回执，预留转为占用并满足资源条件 |
| `POST /relay/lift-control` | 独立复核者确认全部前置项后解除封控（事件进入受保护终态 `resolved`） |
| `POST /relay/reviews/reopen` | 复查失败的显式回流，重开指定条件；迟到消息不能把回流条件重新满足 |
| `POST /relay/close` | 关闭事件（`closed`），生效租约随之结束 |
| `GET /relay/incidents/{id}/timeline` | 道路状态、条件及证据、租约、占用、修订链、决定日志与自然语言解释 |
| `GET /relay/sites/{site_id}/resources` | 场所内每项资源的当前去向与完整历史 |

时间线响应中的每个条件、复查、占用与决定都附带 `evidence`（修订号、消息号、事件时间、操作者、修订哈希），修订链与审计链均可离线校验。
