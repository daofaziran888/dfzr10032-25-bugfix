# 多源数据采集与RPA自动化平台

面向网页抓取、API接入、增量采集、RPA流程编排、数据校验与采集监控的数据工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据源配置 | `datasource` | 数据源 | 源端编号、连接类型、接入地址 |
| 网页采集规则 | `crawler` | 采集规则 | 规则编号、目标网址、页面选择器 |
| API接入管理 | `apiclient` | API客户端 | 客户端编号、接口名称、请求方法 |
| 增量采集 | `incremental` | 增量游标 | 游标编号、来源标识、增量字段 |
| RPA任务编排 | `robot_task` | RPA任务 | 任务编号、任务名称、页面定位器 |
| 数据接入通道 | `ingestion` | 接入通道 | 通道编号、通道名称、输入格式 |
| 格式解析器 | `parser` | 解析配置 | 解析编号、输入格式、输出格式 |
| 采集结果校验 | `validator` | 校验规则 | 规则编号、校验字段、校验类型 |
| 采集去重 | `dedup` | 去重策略 | 策略编号、输入来源、去重键 |
| 采集调度 | `schedule` | 调度计划 | 计划编号、关联任务、调度周期 |
| 采集监控 | `monitor` | 监控面板 | 面板编号、采集源总数、今日采集量 |
| 采集日志 | `log_viewer` | 日志条目 | 日志编号、关联任务、日志级别 |
| 凭证管理 | `auth` | 认证凭证 | 凭证编号、凭证类型、关联数据源 |
| 采集转换管道 | `transform` | 转换节点 | 节点编号、输入来源、输出目标 |
| 数据流转编排 | `flow` | 流转管道 | 管道编号、管道名称、起始节点 |
| 数据血缘追踪 | `lineage` | 血缘关系 | 关系编号、上游节点、下游节点 |
| 采集报告 | `report` | 采集报告 | 报告编号、采集源、统计周期 |
| 采集载荷截断明细 | `truncation` | 截断明细/接入对账 | 声明条数、消息结束边界、断条页内位置、稳定运单键 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 采集载荷截断明细与接入对账

针对分页响应被截断（末行断在字段中间、页尾条目丢失）的采集-重试-对账链路，
代码在 `backend/app/truncation/`，接口在 `backend/app/routers/truncation.py`：

- **截断校验（`inspector.py`）**：用标准库 `json.JSONDecoder.raw_decode` 从被截断的
  响应原文恢复完整前缀，校验三件事——声明条数、消息结束边界、已收到事件；输出断条的
  页内位置与字符偏移、丢失位置、完整事件清单。
- **总数漂移只告警**：服务端声明的 `total` 会因采集期间新增记录而变化，`total` 对不上
  只记 `total_drift`，不会把边界完整的页判成错误页。
- **重采关联（`reconcile.py`）**：截断页的完整前缀与重采数据按原页序+位置关联；
  重发前缀（如重试时又送来的前 20 条）识别为重复、不二次交付；每个稳定键全局只占一条；
  同位置键不一致（内容错位）显式留痕，越界写入被拒绝。
- **两个业务视图**：装运侧随时可查“已可放行运单 / 等待页尾位置”（重试回来前也能查）；
  结算侧按原页序与键值核齐整页。

接口前缀 `/api/truncation`：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/channels/{code}/payloads` | 登记首次采集响应，回截断明细 |
| POST | `/channels/{code}/retries` | 登记重试响应，回去重/补齐关联结果 |
| GET | `/channels/{code}/releasable` | 装运放行查询（可放行运单 + 等待页尾位置） |
| GET | `/channels/{code}/pages/{p}/reconcile` | 结算整页对账（原页序 + 键值） |
| GET | `/channels/{code}/overview` | 通道各页接入进度汇总 |

仅用标准库的测试与演示（无需安装依赖）：

```bash
cd backend
python3 -I tests/test_truncation.py          # 截断恢复与重采对账单测
python3 -I tests/test_truncation_service.py  # 服务层端到端时序
python3 scripts/demo_truncation.py           # 题设场景演示（50 条/第 37 条断开/重试 52 条）
```
