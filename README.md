# 跨海光缆故障与抢修协调

纯Python标准库实现的跨海光缆故障与抢修协调原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、海况、船机许可、备缆窗口和接续质量和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8330
```

默认端口为`8330`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数；`state=rectification_pending`可筛出仍卡在埋深整改的记录，列表项含`blocking`卡点摘要。
- `GET /api/records/{id}`：记录详情，含`blocking`卡点（最浅点、缺口、当前船舶、整改时间）。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/burial`：历次埋深测点，可带`round`参数只看某一轮；换船后历史测点仍可查，并保留当时测量船舶。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

## 抢修状态流与埋深复测

状态流：

```
detected → approved → mobilized → surveyed → spliced
  → burial_survey → buried（合格）或 rectification_pending（低于设计值）
rectification_pending →（rectify 整改完成 → resurvey 全段复测）→ buried 或继续待整改
buried → tested → restored
```

设计埋深为1.5米。接续（`splice`）完成后不能直接测试，必须先做埋深复测：

- `burial_survey`（cable_engineer）：按测点提交`burial_points`（每项含`distance_km`、`burial_depth_m`，可选`measured_at`）。测点必须覆盖全段（起止里程均有测点、里程不重复且不越界）。最浅点达到1.5米进入`buried`，否则停在`rectification_pending`，详情`blocking`列出最浅点位置、埋深和缺口（设计值减实际值）。
- `rectify`（cable_engineer）：现场整改完成，提交`rectified_at`。
- `resurvey`（cable_engineer）：整改后重新提交全段测点。未整改不能复测；复测时间（`measured_at`）早于整改完成时间（`rectified_at`）不放行；复测仍不达标继续停在待整改，可多轮整改复测。
- `change_vessel`（vessel_master）：整改期间换船，提交`new_vessel_name`；原船记入`released_vessels`释放，历史测点和轮次保留不受影响。
- `test`只允许在`buried`状态执行，埋深未达标无法放行到恢复。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
