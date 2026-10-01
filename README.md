# 酒店经营数据自动化系统

一套面向酒店运营的自动化系统,把「取数据 → 发提醒 → 比价格」这三件每天都要做的事交给程序完成。

系统对接携程 eBooking 商家后台与企业微信,自动采集酒店经营数据、按时推送到群里,
并把周边竞品酒店的房价抓回来做横向对比。

---

## 功能

### 一、经营数据自动采集

从携程 eBooking 商家后台采集酒店的经营数据,包括流量、订单、点评、竞争圈等,
按日期归档入库(PostgreSQL),并支持定时截图留档。

### 二、企业微信自动推送

通过企业微信机器人把数据推到群里,支持:

- **日报**——每天固定时间推送当日经营数据
- **告警预警**——流量异常、关房、差评等按规则触发提醒
- **点评管理**——自动生成回复草稿,支持审核后发送
- **群内命令**——在群里直接查数据、发报告、跑任务

### 三、酒店比价

把周边竞品酒店的房价抓回来做对比:

- 同时支持 **携程** 与 **美团** 两个平台的前台
- 按真实距离排序(自动计算与目标酒店的直线距离)
- **一家店一行**展示两个平台的报价,直接标出哪个平台更便宜、差多少钱
- 生成 Markdown / HTML 报告存档,并可推送到群里

---

## 技术栈

| 类别 | 选型 |
|---|---|
| 语言 | Python 3.14 |
| Web 框架 | FastAPI |
| 数据库 | PostgreSQL 16 + SQLAlchemy 2(异步)+ Alembic |
| 浏览器自动化 | Playwright(异步) |
| 定时调度 | APScheduler |
| 命令行 | Typer |
| 日志 | Loguru |
| 部署 | Docker Compose |

---

## 项目结构

```
hotel-data-system/
├── src/hoteldata/
│   ├── domains/            # 业务域
│   │   ├── collect/        # 经营数据采集
│   │   ├── alert/          # 告警预警
│   │   ├── review/         # 点评管理
│   │   ├── report/         # 报表生成
│   │   ├── bot/            # 企业微信机器人
│   │   └── compare/        # 酒店比价
│   ├── infra/              # 基础设施(数据库 / 浏览器 / 任务注册表 / 迁移)
│   ├── push/               # 消息推送
│   ├── web/                # Web 管理后台
│   ├── cli.py              # 命令行入口
│   └── runtime.py          # 统一装配入口
├── config/                 # 业务配置(比价目标、预警规则、采集配置等)
├── docs/                   # 文档与验收记录
├── scripts/                # 验收与诊断脚本
├── var/                    # 运行产物(报告、截图、登录态)
└── docker-compose.yml      # 数据库
```

---

## 快速开始

### 1. 环境准备

```bash
# Python 3.14 + Poetry
poetry install

# 复制配置模板并填写
cp .env.example .env
```

### 2. 启动数据库

```bash
docker compose up -d
poetry run alembic upgrade head
```

### 3. 登录一次

系统需要人工登录一次平台账号(登录态会加密保存,之后自动复用):

```bash
# 商家后台(数据采集用)
poetry run hoteldata login

# 前台(比价用,与商家后台是两套登录态)
poetry run hoteldata price login --platform ctrip
poetry run hoteldata price login --platform meituan
```

### 4. 开始使用

```bash
# 采集当日经营数据
poetry run hoteldata collect

# 手动推一次日报
poetry run hoteldata push now

# 单店比价
poetry run hoteldata compare --name "某某酒店" --city 某某市

# 启动服务(机器人 + 推送 + 定时调度)
poetry run hoteldata serve
```

---

## Web 管理后台

系统自带一个轻量的 Web 管理界面,可以查看数据、管理任务与配置:

```bash
# 设置后台口令(哈希存储,不存明文)
poetry run hoteldata admin passwd --set 你的口令

# 启动
poetry run hoteldata serve
# 浏览器打开 http://127.0.0.1:8000/admin
```

---

## 技术要点索引

> 每条都给出**文件路径**,可直接在仓库中查看实现。

### 架构

| 要点 | 实现位置 | 说明 |
|---|---|---|
| 分层与域隔离 | `src/hoteldata/domains/`、`runtime.py` | 业务域 / 基础设施 / 装配三层;域间不互相依赖,注入点收敛到装配层 |
| 统一状态机 | `domains/collect/contract.py` | `ok / degraded / no_data / failed` —— **"无数据"与"失败"在类型层面分开** |
| 任务注册表为唯一事实源 | `src/hoteldata/jobs.py` | 22 个任务的时刻表集中定义,`.env` 只放开关 |

### 并发与资源管理

| 要点 | 实现位置 | 说明 |
|---|---|---|
| 会话资源池 + LRU 淘汰 | `infra/browser.py` | 上限 `max_contexts=4`;超出淘汰**空闲**上下文,全忙时排队等待 |
| 双层令牌桶限速 | `infra/rate_limit.py` | **平台级** + **账号级**两层;平台限频 0.6s/请求(风控闸门) |
| 异步长连接 | `domains/bot/client.py` | `websockets` + `asyncio.Task`,单进程多路连接;心跳保活、分片传输 |

### 可靠性

| 要点 | 实现位置 | 说明 |
|---|---|---|
| 三级异常分类 | `domains/compare/contract.py` | `可重试` / `不可重试` / `需人工验证` 分开建模,处置方式各不相同 |
| 全链路幂等 | 18 处 `on_conflict_do_*` | 会话、群绑定、告警状态、比价行等均以唯一约束保证重跑不产生重复 |
| 任务抢占 + 补跑 | `infra/tasks.py` | 「抢占即执行」防并发重跑;停机后按时间窗补跑,超 `max_delay` 记 `skipped` |
| 唯一约束陷阱修复 | `migrations/versions/0006_*.py` | PostgreSQL 中 `NULL` 在 UNIQUE 约束里互不相等 → 改用 `NULLS NOT DISTINCT` |

### 反爬与浏览器自动化

| 要点 | 实现位置 | 说明 |
|---|---|---|
| 拟人化行为集 | `domains/compare/human.py` | 逐字符变速打字、分步鼠标移动、随机滚动、抖动、弹窗自动关闭 |
| 验证码检测 | `domains/compare/human.py` | 识别到验证码即停止,交由人工处理 |
| 双通道取价 | `domains/compare/platforms/` | 接口取元数据(ID/坐标/评分)+ 页面取价格,按名称归一化合并 |
| 视觉读价兜底 | `domains/compare/vision.py` | 页面结构无法解析时,用视觉模型读截图;带预算控制 |

### 安全

| 要点 | 实现位置 | 说明 |
|---|---|---|
| 凭据加密 | `infra/crypto.py` | Fernet 加密,支持 `kid` 密钥轮换;明文不落盘 |
| 后台认证 | `web/auth.py` | PBKDF2-HMAC-SHA256 / **600,000** 次迭代(OWASP 2023 建议值) |
| 会话与审计 | `web/auth.py`、表 `ops_admin_audit` | 签名 cookie;审计表**只追加** |

### 数据层

| 要点 | 实现位置 | 说明 |
|---|---|---|
| 21 张表 / 7 个版本化迁移 | `infra/models/`、`infra/migrations/` | 数据库变更进代码评审 |
| 异步驱动 | SQLAlchemy 2 异步 + asyncpg | 全链路异步,无阻塞调用 |

### 工程化

| 要点 | 实现位置 | 说明 |
|---|---|---|
| 编号验收体系 | `scripts/verify_acceptance{,2,3}.py` | **84 项**(V1–V84),每项输出**可核验证据**而非仅通过/失败 |
| 自检断言 | `check_compare_{units,platforms}.py`、`check_admin_web.py` | **167 条**(88 + 48 + 31) |
| 平台改版诊断 | `hoteldata price probe`、`scripts/diag_*` | 逐条报选择器命中数,快速定位失效点 |

---

## 定时任务

所有定时任务的时刻表集中定义在代码里的任务注册表中(`src/hoteldata/jobs.py`),
`.env` 只负责开关(`*_ENABLED`),不写时刻。

| 任务 | 默认时间 | 说明 |
|---|---|---|
| 经营数据采集 | 每天多次 | 按配置采集各模块数据 |
| 日报推送 | 每天 09:00 | 汇总当日数据推送 |
| 截图留档 | 每天凌晨 | 关键页面截图存档 |
| 数据库备份 | 每天凌晨 | 自动备份 |
| 比价采集 | 每天多次 | 抓取周边酒店房价 |
| 比价推送 | 每天下午 | 比价结果推送 |

---

## 验收

系统分三个阶段开发,每个阶段都有独立的验收脚本:

| 阶段 | 内容 | 结果 | 记录 |
|---|---|---|---|
| 段1 | 经营数据采集 | 20 / 20 通过 | [验收记录](docs/段1-验收记录.md) |
| 段2 | 企业微信推送 | 39 通过 / 1 待真实凭据 | [验收记录](docs/段2-验收记录.md) |
| 段3 | 酒店比价 | 24 / 24 通过 | [验收记录](docs/段3-验收记录.md) |

```bash
.venv\Scripts\python.exe scripts\verify_acceptance.py     # 段1
.venv\Scripts\python.exe scripts\verify_acceptance2.py    # 段2
.venv\Scripts\python.exe scripts\verify_acceptance3.py    # 段3
```

---

## 配置说明

`.env` 里只放**敏感凭据**与**开关**,不放业务配置:

| 变量 | 说明 |
|---|---|
| `DB_URL` | 数据库连接串 |
| `MANAGE_CHATIDS` | 管理群白名单(留空则群内命令一律拒绝) |
| `OPS_CHATID` | 运维告警群 |
| `ZHIPU_API_KEY` | 视觉模型 API Key(截图识别用) |
| `ADMIN_PASSWORD_HASH` | Web 后台口令哈希 |
| `ADMIN_SESSION_SECRET` | 后台会话签名密钥 |

业务配置放在 `config/` 目录,例如比价目标、预警规则、报表模板等。

> `.env` 与 `config/secret.key` 已被 `.gitignore` 排除,不会进入版本库。
> 新环境请从 `.env.example` 复制后自行填写。
