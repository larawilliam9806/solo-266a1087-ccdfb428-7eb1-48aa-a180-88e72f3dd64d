# 公交 GTFS 时刻表发布系统

FastAPI + SQLite + 本地文件的公交时刻表发布与查询网页。数据工程师一次性上传同一版本的
5 个 CSV（stops / routes / trips / stop_times / calendar），系统整版校验通过后才发布；
公众按「线路 + 运营日」查询有序停站时刻。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `gtfs-data` 中，容器重启数据不丢。

### 本地运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

### 运行测试

```bash
pip install pytest httpx
pytest tests/ -q
```

## 网页功能

- **时刻查询**：选择运营日和线路，展示当日各班次的停站序列与到发时刻；
  跨午夜时刻显示为 `24:20:00` 并标注「+1天」。
- **运行图差异分析**：选择两个**不同的已发布版本**、线路与同一运营日，
  系统分别固定两版完整快照后整体比较，列出**新增、移除、调整、未变化**班次与
  **匹配冲突**；调整班次给出首个变化停站的旧/新到发时刻与变化分钟
  （同时返回秒数，分钟由秒数换算并保留两位小数）。
- **发布新版本**：一次上传 5 个 CSV + 版本名称。通过则立即生效；
  失败则列出全部错误行（文件 / 行号 / 字段 / 原因），当前正式版不受影响。
- **版本记录**：查看所有版本（含失败版本）及其错误明细。

也可以用 `sample_data/valid/`（可通过）和 `sample_data/invalid/`（含典型错误）
两组样例数据在网页上直接试。

### 差异分析的匹配与冲突规则

1. **先按班次号 `trip_id` 匹配**：版本内 `trip_id` 唯一，必然是一对一。
2. **班次号对不上的，按「线路 + 运营日 + 有序停站序列」匹配**：
   停站序列即按 `stop_sequence` 排好的 `stop_id` 元组，不看时刻。
   - 两侧候选都恰好 1 个 → 匹配（即使班次号已变化）；
   - 任一侧候选不唯一 → 记为**冲突**，响应中列出该序列两侧的全部对应班次，
     **不擅自合并**，且这些班次不计入新增 / 移除 / 调整；
   - 序列只在一侧出现 → 计入新增或移除。
3. 匹配上的班次逐站比较：停站序列与到发时刻完全一致为未变化，否则为调整。
   首个不同位置即「首个变化停站」：停站不同（或一侧缺站）报停站变化、不计算
   分钟差；停站相同但时刻不同，给出旧/新到发时刻与到、发各自的变化分钟。
4. **快照固定、无部分结果**：请求开始即校验两个版本（必须存在且已发布、互不
   相同）与日期格式，任一不合法直接返回 4xx，不生成任何结果；校验通过后分别
   加载两版完整快照再整体比较。版本数据不可变，整个请求固定在这两份快照上。
   线路仅在一版中存在时仍返回完整结果（另一版班次整体计新增/移除）；两版都不
   存在该线路时返回 404。

## 校验规则

任一规则不满足，整版拒绝发布：

1. **结构**：必备列齐全；字段类型合法（非负整数序号、YYYYMMDD 日期、0/1 星期标志、
   HH:MM:SS 时刻）。
2. **表内唯一**：`stop_id` / `route_id` / `service_id` / `trip_id` 各自唯一；
   `stop_times` 内 `(trip_id, stop_sequence)` 唯一。
3. **跨表引用**：`trips.route_id → routes`、`trips.service_id → calendar`、
   `stop_times.trip_id → trips`、`stop_times.stop_id → stops`；
   每个 trip 至少有一条 stop_times。
4. **时刻递增**：同一 trip 按 `stop_sequence` 排序后，每行 `departure ≥ arrival`，
   且下一站到站时刻严格大于上一站发车时刻。
5. **跨午夜**：时刻允许 `HH ≥ 24`（如 `25:10:00`），内部统一换算为
  「自运营日 00:00 起的秒数」比较与存储；`24:10` 之后出现 `23:59` 视为倒退报错。

## 发布与并发语义

- **整版原子发布**：所有校验在内存中完成；通过后在**单个 SQLite 事务**内写入
  整版数据并拨动 `meta.current_version_id` 指针。查询方要么看到旧完整版本、
  要么看到新完整版本，绝不出现半新半旧。
- **失败保旧**：校验失败的版本只记录 `versions(status='failed')` 和错误明细，
  不写入任何时刻数据、不触碰指针。
- **版本不可变**：已发布版本的数据永不修改或删除；请求开始时读一次指针，
  之后该请求内所有查询都固定读取同一版本，换版期间查询结果始终完整一致。
- SQLite 开启 **WAL**，发布写入不阻塞并发查询。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/versions` | multipart 上传 `label` + 5 个 CSV；成功 201，失败 422 并返回错误行 |
| GET | `/api/versions` | 版本列表（含当前正式版指针） |
| GET | `/api/versions/current` | 当前正式版 |
| GET | `/api/versions/{id}` | 版本详情与错误明细 |
| GET | `/api/routes` | 当前版本的线路列表 |
| GET | `/api/versions/{id}/routes` | 指定已发布版本的线路列表（差异分析用） |
| GET | `/api/routes/{route_id}/schedule?date=YYYY-MM-DD` | 该运营日的班次与有序停站时刻 |
| GET | `/api/diff?old_version_id=&new_version_id=&route_id=&date=YYYY-MM-DD` | 两版运行图差异：新增/移除/调整/未变化/冲突及首个变化停站的分钟差 |

### 命令行示例

```bash
# 发布
curl -F "label=2026冬季运行图" \
     -F "stops=@sample_data/valid/stops.csv" \
     -F "routes=@sample_data/valid/routes.csv" \
     -F "trips=@sample_data/valid/trips.csv" \
     -F "stop_times=@sample_data/valid/stop_times.csv" \
     -F "calendar=@sample_data/valid/calendar.csv" \
     http://localhost:8000/api/versions

# 查询夜班线路（含 24:00 之后的跨午夜班次）
curl "http://localhost:8000/api/routes/R2/schedule?date=2026-11-23"

# 比较版本 1 与版本 2 在 R1 线路、同一运营日上的运行图差异
curl "http://localhost:8000/api/diff?old_version_id=1&new_version_id=2&route_id=R1&date=2026-11-23"
```

## 目录结构

```
app/
  main.py        FastAPI 端点（发布 / 查询 / 版本 / 差异分析）
  gtfs.py        CSV 解析与整版校验（纯标准库）
  diff.py        两版快照加载与差异匹配引擎（纯函数，可单测）
  db.py          SQLite schema、原子发布、版本指针
  static/index.html  网页（查询 / 差异分析 / 发布 / 版本记录）
tests/           pytest 单元与集成测试（test_diff* 覆盖差异引擎与 API）
sample_data/     可通过与含错误的样例 CSV
Dockerfile / docker-compose.yml
```
