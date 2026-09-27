# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口、接续质量、埋深复测和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询（含埋深复测轮次表）。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、埋深复测闸门和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情（含埋深闸门摘要：最浅点、缺口、最近一轮轮次/时间、阻断原因）。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/depth`：全部埋深复测轮次（含不合格轮次）的历史测点。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

## 状态与埋深复测闸门

状态流：`detected → approved → mobilized → surveyed → spliced → depth_check → depth_verified → tested → restored`；埋深不达标进入`depth_pending`，整改并复测达标后才能放行。

设计埋深固定为`1.5`米（创建记录时写入`design_burial_depth_m`）。

- `depth_check`（cable_engineer，spliced后）：提交`measured_at`和测点列表`points:[{distance_km, depth_m}]`，按测点记录距离与实际埋深；任一点低于1.5米即停在`depth_pending`，详情与列表的`payload.burial`中给出最浅点（`shallowest_distance_km`/`shallowest_depth_m`）、缺口`gap_m`和阻断原因。全部达标则进入`depth_verified`。
- `rectify`（vessel_master，depth_pending）：提交`rectified_at`记录现场整改完成；可选`new_vessel_name`，提供新船名即释放原船（记录`released_vessel_name`和`vessel_changes`，不提供则沿用原船）。整改后仍停在`depth_pending`等待全段复测。
- `resurvey`（cable_engineer，depth_pending）：重新提交全段复测。必须覆盖首轮全部测点距离，且`measured_at`不得早于最近一次整改完成时间；否则不达标、继续停在`depth_pending`。
- 每一轮复测（无论是否达标）都独立留档，可通过`/api/records/{id}/depth`查询历史测点；在`depth_pending`或`depth_verified`下`test`才被允许。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
