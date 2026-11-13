# 数仓分层建模与治理平台

面向原始数据落地、清洗转换、分层建模、维度事实设计、分区治理、质量校验、血缘追踪与历史补数的数据仓库工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
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
| 原始落地 | `landing` | 落地批次 | 批次编号、来源系统、落地路径 |
| 贴源层 | `ods` | 贴源表 | 表编号、表名称、来源系统 |
| 明细层 | `dwd` | 明细表 | 表编号、表名称、业务域 |
| 维度层 | `dim` | 维度表 | 维度编号、维度名称、自然键列 |
| 汇总层 | `dws` | 汇总表 | 表编号、表名称、统计粒度 |
| 应用层 | `ads` | 应用表 | 表编号、表名称、业务场景 |
| 分区治理 | `partition` | 分区方案 | 方案编号、数据表名、分区字段 |
| 质量校验 | `quality` | 质量规则 | 规则编号、校验表名、目标字段 |
| 数据血缘 | `lineage` | 血缘关系 | 关系编号、上游表名、下游表名 |
| 历史补数 | `backfill` | 补数任务 | 任务编号、目标表名、开始日期 |
| 清洗规则 | `cleanrule` | 清洗规则 | 规则编号、输入表名、空值策略 |
| 转换编排 | `transform` | 转换节点 | 节点编号、输入表名、输出表名 |
| 数据字典 | `dictionary` | 字典条目 | 条目编号、表名、字段名称 |
| 元数据管理 | `metamodel` | 元数据项 | 元数据编号、对象类型、对象名称 |
| 数据生命周期 | `lifecycle` | 生命周期策略 | 策略编号、数据表名、保留天数 |
| 存储成本 | `cost` | 成本账单 | 账单编号、数据表名、存储大小GB |
| 同步通道 | `sync` | 同步通道 | 通道编号、来源表名、目标表名 |
| 变更审计 | `auditlog` | 变更记录 | 记录编号、操作表名、变更类型 |

## 转换依赖准入与血缘投影

转换编排的「启用」不再只是改状态，而是一条准入链路：

1. `POST /api/transform/enable/preview`（或页面「预演准入」）只做校验，不写数据；
   提交下游节点即可，系统会自动扩展到依赖闭包。
2. 校验输入输出表（必填、不得同名）、依赖节点（存在、未停用、输出表必须进入下游输入）、
   执行顺序（整数且依赖早于下游）以及同表唯一生产者。
3. 出现环、缺失依赖、同表多个生产者时整组拒绝，返回可定位的错误代码、节点与表；
   冲突节点按依赖图顺序排列。拒绝时不会写入任何血缘、状态、版本或审计。
4. 校验通过后按拓扑顺序生成执行计划，并按字段映射把原始输入字段投影到最终输出字段，
   例如 `RAW_ORDERS.amount → ODS_ORDERS.amount → CLEAN_ORDERS.total_amount`。
   字段映射未覆盖时按同名字段推断，仍无法确定才整表兜底并在「字段识别」中标注。
5. 状态、血缘关系、版本快照与变更审计在同一次提交中落库；提交过程出现异常会整体回滚。
6. 同一依赖组重复提交或并发启用只生效一次（进程内串行 + 幂等键
   `Idempotency-Key`）；启用版本可通过 `GET /api/transform/versions` 查看。
7. 停用只归档血缘（`生效状态=历史归档`）并保留版本里的执行计划，历史仍可追溯；
   被已启用节点依赖时要求先停用下游。

示例数据内置 `TRAN_LAND → TRAN_DWD → TRAN_CLEAN` 三个节点：
启用 `TRAN_CLEAN` 会得到 3 步执行计划与 `LINE_LAND_TO_DWD`、`LINE_DWD_TO_CLEAN`、
`LINE_LAND_TO_CLEAN` 等字段级关系；把 `TRAN_DWD` 的依赖改成包含 `TRAN_CLEAN`
即可看到整组被环检查拒绝。

后端回归测试：

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -v
```

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
