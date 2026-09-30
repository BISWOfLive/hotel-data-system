# 酒店经营数据自动化系统 · 段3「比价功能」

> 携程 eBooking 经营数据自动化系统 —— **第 3 段:把美团/携程前台的房价按点取回来、比出来、发出去**。
>
> 上游依据:《酒店经营数据自动化系统_全量重写计划书》(总纲) +
> 《…_第3段_比价功能_开发设计计划书》(施工图) +
> **《…段3-计划书优化修订清单》**(实测修订,**冲突时以修订清单为准**)。
> 段1(提取)、段2(推送)已完成并验收;段3 **只复用段1 的基础设施、只调用段2 的推送契约**。

**段3 的终点线(唯一验收)**

> `hoteldata compare --name "隐欲民宿"` 一条命令:定位锚点 → 取回附近 3 家酒店报价
> (**`distance_km` 真实可用且按距离排序**)→ 生成 md/html 报告 → 存档进 PG
> (**同日多次采集各留一份,重跑不产生重复行**);`hoteldata compare-batch` 按清单批量跑完,
> 09:00 的日报里**出现价格段**。

**当前状态:V61–V84 共 24 条 → 23 PASS / 0 FAIL / 1 BLOCKED**
(唯一 BLOCKED 是 V72「美团真机取价」:美团前台登录态失效且**无账号凭据**,需人工拖滑块
→ 如实标 BLOCKED,**代码路径已 PASS**)。
详见 [`docs/段3-验收记录.md`](docs/段3-验收记录.md)。

---

## 快速开始(段3)

```bash
# ① 人工登录一次**前台**(与段1 的商家后台登录是两回事,见下)
poetry run hoteldata price login --platform ctrip     # 携程前台
poetry run hoteldata price login --platform meituan   # 美团前台(可选)

# ② 比价目标(取代旧系统的两个 txt)
poetry run hoteldata targets add --name "隐欲民宿" --city 莱州
poetry run hoteldata targets import --from <旧系统根目录> --dry-run   # 一次性导入旧清单

# ③ 单店比价(段3 的唯一验收入口)
poetry run hoteldata compare --name "隐欲民宿"

# ④ 离线看一眼(不碰平台、不碰登录态)
poetry run hoteldata compare --name "隐欲民宿" --demo

# ⑤ 批量 / 定时采集 / 独立推送
poetry run hoteldata compare-batch [--force]
poetry run hoteldata task run compare.collect
poetry run hoteldata price push

# ⑥ 诊断与历史
poetry run hoteldata price probe --platform ctrip --name "隐欲民宿"   # 改版时看选择器命中
poetry run hoteldata price history --name "隐欲民宿" --days 7
```

### 验收

```bash
.venv\Scripts\python.exe scripts\verify_acceptance3.py        # V61–V84
.venv\Scripts\python.exe scripts\check_compare_units.py       # 取价语义/坐标/距离 单元自检
.venv\Scripts\python.exe scripts\check_compare_platforms.py   # 用**真接口响应**验解析
.venv\Scripts\python.exe scripts\verify_acceptance.py         # 段1 回归
.venv\Scripts\python.exe scripts\verify_acceptance2.py        # 段2 回归
```

---

## 段3 修掉的旧缺陷(这才是段3 的核心价值)

旧比价域的真实现状**比计划书诊断的更严重**。段3 的四份基线报告
(在 [`docs/参考/段3-分析/`](docs/参考/段3-分析/))给出了实测证据:

| # | 旧缺陷 | 实测证据 | 段3 的做法 | 验收 |
|---|---|---|---|---|
| **P1** | **取价语义崩坏**:18 条归档里 **12 条把优惠券当成了房价**(「折扣券 ¥34」「十亿豪补 ¥12」) | 旧库全量导出 + 旧 `ctrip.py:82-108` 要求"数值型 price 键"而真接口没有 | **券价过滤器 + `price_scope` + `price_rejected`(丢弃可见)**;兜底判据改为"候选够不够"而非"价格是否为空" | **V83** |
| **P2** | 「¥236起」被当成确定的房价 | 两平台列表页只有**起价** | `price_scope='from'` **显式标注**,报告/推送都写清口径 | V73 |
| **D13** | **`distance_km` 恒为 `None`** → `HOTEL_RANK_MODE=geo` **完全空转** | 旧库 **18/18 条 distance 为空**;而携程接口**本来就有** `position.lat/lng`、美团卡片**本来就有**「直线1.2公里」 | **逐条坐标提取 + haversine 真填**;实测落库 **0.100/0.940/1.000 km** | **V69** |
| **P3b** | **跨平台比价被自己合并掉** —— `_merge_quotes` 把两平台价压成一条、**只留先到的价** | 旧 `runner.py:85-115` | 合并**只归一化去重,绝不合并价格** | **V84** |
| **D8** | 两表无 UNIQUE,SELECE-then-INSERT → **6 组重复 / 冗余 10 行**;同日 **2~5 条 `done`** | 旧库实测 + `pusher.py:568` **硬编码 `force=True`** | **先去重 → UNIQUE(含 slot)→ UPSERT**;`cmp_batch_runs` 每日一行 + `attempt`;`force` 变成**只有人能按的开关** | V65/V78/V79/V80 |
| **D15** | 平台"插件"是**假的**(真契约靠 `hasattr` 探测,4 个模板钩子全是死代码) | 旧 `base.py:16` / `runner.py:264` | **真 Protocol + 类型化模型 + 三态异常 + 声明式 `@register`** | V61/V62/V63 |
| **D14** | 视觉读价**九环死链**,从未跑过一次 | 旧库 `vision_price` 非空 **0/18**;`.env` 无 Key | 接回主链但**门控默认关闭**(零 HTTP/SDK)+ **交叉校验**偏差 >20% 标人工 | V74/V75 |
| — | 骨架页上继续跑启发式(券价的来源) | 旧实拍截图 `ctrip_list_*.png` 是**灰色骨架**;35 份诊断里 **33 份为空数组** | **先等就绪**(接口响应或卡片 ≥2)再取价,否则抛**可重试**异常 | V71 |

### 施工中新发现并修掉的两个**计划书之外**的问题

| 问题 | 影响 | 修法 |
|---|---|---|
| **`cmp_price_key` 唯一约束形同虚设** | 约束含 `room_type`,而它恒为 `NULL` —— **PG 的 UNIQUE 里 NULL 互不相等** → 裸 INSERT **不被拒绝**(实测连插两次都成功)。这是 **D8 的同类病**:schema 写着 UNIQUE 却不挡重复,比"没有约束"更难发现 | 迁移 **`0006`** 改 `UNIQUE NULLS NOT DISTINCT`;修后实测第二次裸插抛 `IntegrityError` |
| **`upsert_target` 把「已停用」悄悄改回启用** | `enabled` 默认 `True` → 每次 `targets add`/`import` 都重新启用管理员手动停用的目标,而那通常正是因为改版/风控期间不想跑 | `enabled` 默认 `None` = **不改动已有行的启用状态** |

---

## 段3 的三条必须理解的"业务事实"(写错就全盘错)

| 事实 | 为什么 |
|---|---|
| **「起价」不是房价** | 两个平台的**列表页只给「¥236起」** —— 那是区间下界,不是这家店今晚的价。真房型价必须逐家开详情页(段3 明确延后)。所以库里每一行都带 `price_scope`(`from`/`exact`),报告与推送都写「起」字 |
| **券价 ≠ 无价** | 券价是"我们主动不要",无价是"页面确实没有"。旧系统把两者都变成 `None`,于是**无法判断过滤是否过狠**。段3 用 `price_rejected` 把丢掉的候选原文**留在库里**,`price_scope` 与 `price_rejected` 是两件不同的事 |
| **`distance_km=None` 的含义是"不许假装排过序"** | 段3 在**三路提取**(接口坐标 → 卡片距离文本 → 城市中心兜底)后仍拿不到时,`distance_km` 留 `None`、`coord_source='none'`、`degraded=True`,**报告显式写「距离不可用」** —— 静默退化比报错更糟 |

---

## 常用命令(段3)

| 命令 | 说明 |
|---|---|
| `hoteldata compare --name <酒店> [--city] [--platform] [--nights] [--rooms] [--nearby] [--demo] [--no-save]` | 单店比价 |
| `hoteldata compare-batch [--force] [--city] [--date]` | 批量(`cmp_price_targets mode=batch`) |
| `hoteldata targets add\|list\|set\|remove\|import` | 比价目标维护(**取代旧的两个 txt**) |
| `hoteldata price login --platform <ctrip\|meituan>` | **前台**登录(`role=ota` / `ota_meituan`) |
| `hoteldata price probe --platform --name` | 页面结构探测(**改版时逐条选择器报命中数**) |
| `hoteldata price history --name [--days]` | 历史价格(按天 + slot) |
| `hoteldata price push [--slot] [--force]` | 手动触发 14:00/18:00 的独立推送 |

### ★ 取不到价格时按这个顺序查

实测踩过的坑(全部已修,记录在这里以便日后平台改版时对照):

| 现象 | 真正原因 | 怎么确认 |
|---|---|---|
| 价格列全是「—」,但**距离正常** | ① **未登录**:携程价格列显示「登录看低价」;② 该酒店**不可订**(页面写「本酒店目前不接受预订」) | `hoteldata price login --platform ctrip --name "<可订的酒店>"`;或看 `var/reports/diag/` 下的详情页截图 |
| 登录时判"成功"但价格还是没有 | 旧版登录检测用**cookie 数量**(匿名访问也有 76 个)→ 误判 | 新判据看**页面有没有「登录看低价」**;`price login` 会打印判定依据 |
| 美团取回的是**别的城市**的酒店 | `cityId` 硬编码成都(59),城市没解析 | 日志里应有 `[meituan] 城市 莱州 -> cityId=529`;没有就是解析失败 |
| 美团页面 404 / `NoSuchKey` | 旧地址 `/awp/h5/hotel/list.html` **已被美团下线** | 正确地址是 `/awp/h5/hotel/list/**list**.html` |
| 报告里同一家酒店出现两次 | 接口条目(有坐标)与 DOM 卡片(有价)没合并上 | 已修:用 `norm_hotel_name` 归一化 + 只接受唯一匹配 |
| 某平台整个取不到 | 平台改版 | `hoteldata price probe --platform <ctrip\|meituan> --name "<酒店>"` 逐条报选择器命中 |

**诊断脚本**(都在 `scripts/`,离线可跑或只需登录态):

```bash
diag_ctrip_price.py            # 携程详情页到底有没有价(截图 + 页面 ¥ 数字)
diag_ctrip_price_by_hotel.py   # 多锚点对照:接口给不给价 vs 页面给不给价
diag_ctrip_dom_selectors.py    # 携程 DOM 选择器逐条命中 + 价格节点的 class 链
diag_meituan_url.py            # 美团入口 URL 对照(search.html vs list/list.html)
```

> ★ 一个**方法论**上的提醒:上面有 4 个坑都是"**看起来正常但数据是错的**"
> (配错城市、把券价当房价、把重复店当两家、以为登录了其实没有)。
> 这类错误比"取不到"危险得多 —— 所以段3 的每条改动都配了对照实验脚本,
> 并且**报告里会显式标注**降级状态(「距离不可用」「待人工确认」「起价」)。

### 段3 的任务注册表(时刻表**只在这里**;`.env` 一个时刻都没有)

| 任务 | cron | 补跑 | 说明 |
|---|---|---|---|
| `compare.batch` | `0 1 * * *` | ✅ 6h | 清单批量比价 |
| `compare.collect` | `30 8,13,17 * * *` | ✅ 6h | 定时采集存档(**08:30 那次供 09:00 日报合并**) |
| `compare.push` | `0 14,18 * * *` | ✅ 6h | 独立推送(**纯文字**) |

> ★ **09:00 的比价不单独推** —— 它通过段2 的 `price_section` 钩子**合并进日报**。
> 链路是:08:30 采集落库 → 09:00 `push.daily` 组装日报时按「当日最近一个已完成的 slot」
> 读回比价段注入。

---

## 段3 的三条硬约束(与段1/段2 一致)

1. 依赖方向**单向向下**,禁止反向 import;
2. **域之间不互相 import** —— 所以段3 的日报注入点在 **`runtime.start_push()`(装配处)**,
   而不是在 `domains/report/daily.py` 里 import `domains/compare`;
3. **域之间不直接 join 别人的表**;段3 需要酒店名时由 service 传入,repo 只碰 `cmp_*` 三张表。

### ★ 段3 的关键目录

```
src/hoteldata/domains/compare/
├── contract.py      # ★ 平台契约:Protocol + 类型化模型 + 三态异常 + price_scope
├── registry.py      # ★ 声明式注册表(@register);未知平台抛错不静默
├── platforms/
│   ├── selectors.py # ★ 实测常量集中处(改版只改这一个文件)
│   ├── ctrip.py     #   携程前台(真接口 ctGetNearbyHotelList 优先)
│   └── meituan.py   #   美团 H5 列表页(单次 evaluate 批量读)
├── geo.py           # ★ 距离与坐标(修 D13;逐条归属,不是扁平列表)
├── human.py         # ★ 真人节奏库(段3 自己移植 + 异步化)
├── price.py         # ★ 取价语义(券价过滤;parse_price 逐字继承)
├── vision.py        #   视觉兜底(门控默认关闭;走 HTTP 不引 SDK)
├── report.py        #   md + html + 推送文本段
├── repository.py    #   UPSERT 幂等;每日一行批量记录
├── runner.py        #   单店编排(锚点→候选→取价→报告→存档)
├── sections.py      #   ★ 日报钩子(单店 / 群级拼接)
├── service.py       #   域服务(runtime.compare())
└── importer.py      #   旧两个 txt → 表(GBK 编码兼容)
```

---

## 接口契约:段1 → 段2/段3

段1 对外交付三样东西,段2/段3 只能通过它们取数,**不许绕过**:

| 契约 | 形态 |
|---|---|
| ① 结构化数据 | PG 表 `collect_reports` / `collect_modules` / `alert_portal_columns` / `alert_room_states` / `review_reviews` / `review_materials` |
| ② 查询服务 | `domains/collect/service.py`:`fetch_module_record()` / `build_payload()` / `aggregate_daily()` / `today_module_shots()` / `pick_screenshot()` |
| ③ 基础设施 | `infra/browser.py` 浏览器池 · `infra/session_store.py` 登录态 · `infra/rate_limit.py` 限频 · `infra/db.py` |

> 🚫 **禁止**:段2/段3 直接 import `domains/collect/` 内部的提取器实现,或直接读它的表。
> 要走 service。

### 段3 对外交付

| 契约 | 形态 |
|---|---|
| ① 结构化数据 | PG 表 `cmp_price_targets` / `cmp_price_comparisons` / `cmp_batch_runs` |
| ② 域服务 | `runtime.compare()`:`compare()` / `run_batch()` / `run_collect()` / `push_price()` / `group_price_section()` |
| ③ 日报钩子 | `sections.build_group_price_section()` —— **段2 代码一行未改**(V82 用 SHA256 证明) |

---

## 登录态:商家后台 vs 前台是**两个不同的东西**

| 用途 | platform | role | 谁用 |
|---|---|---|---|
| 携程**商家后台**(经营数据) | `ctrip` | `ebooking` | 段1 提取 |
| 携程**前台**(比价) | `ctrip` | **`ota`** | 段3 比价 |
| 美团**前台**(比价) | `meituan` | **`ota_meituan`** | 段3 比价 |

> ★ `ota` / `ota_meituan` **不是段3 起的名字** —— 段1 建 `sessions` 表时就写明了
> (`infra/models/core.py:112-113`)。旧系统把前台登录态放在**根目录的
> `storage_state_ctrip.json`**(绕开账号库)正是"四类登录态三种存法"的病根之一。

```bash
hoteldata sessions          # 看所有角色的登录态
hoteldata price login --platform ctrip
```

---

## 视觉模型

`VISION_ENABLED=0`(**默认关闭**)。🔴 启用前必须取得**客户书面授权**:
视觉调用 = 把客户酒店数据截图**外发**到智谱服务器。

段3 的实现让这条纪律成为**结构性保证**而非约定:

* 模块**顶层不 import 任何视觉 SDK**(实测 `zhipuai` 装了但 import 就失败 —— 缺 `sniffio`);
* 走 HTTP(智谱 OpenAI 兼容端点),复用段1 已有的 `httpx`;
* `VISION_ENABLED=0` 时 `read_price()` **直接返回 None,零 HTTP**(V74 专测);
* 视觉只作**第三档兜底**(DOM/接口都失败时才调),且有**日调用上限**与
  **偏差 >20% 标待人工确认**的交叉校验。

---

## Web 后台(Phase 3;总纲 §7.8)

```bash
# ① 设一次统一口令(不存明文;PBKDF2-HMAC-SHA256 / 600k 迭代)
poetry run hoteldata admin passwd --set "你的口令"     # 写入 .env 的 ADMIN_PASSWORD_HASH

# ② 起服务,浏览器打开
poetry run hoteldata serve
#   → http://127.0.0.1:8000/admin
```

| 页面 | 能做什么 |
|---|---|
| **总览** | 今日提取(四态)/ 推送成功率 / 预警送达率 / 比价条数 + 子系统状态(DB/调度器/机器人/派发器/限频/浏览器池/视觉) |
| **酒店** | 增删改;<b>关键字段 `ebk_hotel_id`</b>(段3 锚点直达靠它) |
| **账号** | 增删改;凭据 Fernet 加密,**页面永不回显明文**(编辑留空 = 不改) |
| **群绑定** | 绑定 / 解绑 / 暂停单条;显示 `MANAGE_CHATIDS` 白名单状态 |
| **机器人** | 增删改 + 连接健康;凭据加密 |
| **比价目标** | 增删 / 启停 / **一键从旧系统两个 txt 导入** |
| **比价历史** | 按天 + slot 看报价;距离 / 坐标来源 / `price_scope` / 待人工确认 / 已过滤券价数 |
| **任务** | 22 个注册任务(cron / 补跑 / max_delay)+ 手动跑一次 + 运行历史 |
| **推送审计** | `push_logs` 按类型/状态筛;`skipped` 标为"去重命中"而非失败 |
| **预警** | 状态机 + 触发记录 + **送达率**(口径:成功行 ÷ 总行) |
| **登录态** | `(platform, role, alias)` 三元组;★ **前台 vs 商家后台分开显示** |
| **操作审计** | `ops_admin_audit`,**append-only**,失败尝试也留痕,明细是 JSONB |

### 后台的四条安全/工程默认

1. **未设口令 = 拒绝一切登录**(不是放行)—— 与"未配置 `MANAGE_CHATIDS` 时管理群命令一律拒绝"同一纪律;
2. **只允许本机**:`ADMIN_ALLOW_REMOTE=0`(默认)。"监听 127.0.0.1"只挡网络层,
   所以在**应用层**再挡一次(非本机 → 404 并记 warning);
3. **凭据只进不出**:账号密码 / 机器人 secret 加密落库,**页面不回显明文**;
   审计明细里也**只记别名与平台**,绝不记用户名/密码(总纲 R4 的直接对策);
4. **凭据改动需重启 `serve` 才生效** —— 长连接与调度器都在 `lifespan` 里建,后台只落库。

> ★ **不引入前端构建链**(总纲 §7.8):Jinja2 服务端渲染 + 一份手写
> `static/admin.css`。没有 npm、没有打包步骤、没有 node_modules。

### 后台自检

```bash
.venv\Scripts\python.exe scripts\check_admin_web.py --password "<你设的口令>"
```

它会**自己起一个临时服务**,然后:未登录守卫 → 错误口令被拒 → 正确口令登录 →
**遍历全部 14 个页面**(验证状态码 + 关键内容)→ 一次真写操作 → 校验审计落库 →
静态资源 → 登出。当前:**31 PASS / 0 FAIL**。

> 为什么要有这个脚本:后台是**服务端渲染**的,模板语法错/变量名错**lint 抓不到、
> 单元测试也抓不到**,只有真的 GET 一遍才暴露。

---

## 段2(企业微信推送)快速开始


段1 的 1–5 步(装依赖 / 配置 / 起库 / `db upgrade` / 登录)**不变**。段2 追加:

### 6. 加机器人 + 绑群

```bash
# 凭据 Fernet 加密后落库,明文不落盘
poetry run hoteldata bots add --name bot01 --bot-id <企微bot_id> --secret <企微secret>
poetry run hoteldata bots list

# 管理群白名单(11 条管理群命令只在这里生效;**留空 = 一律拒绝**,不是放行)
# 写进 .env:MANAGE_CHATIDS=<群chatid1>,<群chatid2>
#          OPS_CHATID=<运维告警群chatid>

poetry run hoteldata bind add --group <群chatid> --hotel 隐欲民宿
poetry run hoteldata bind list
```

### 7. 起服务(机器人网关 + 推送派发器 + 调度器 + 补跑)

```bash
poetry run hoteldata serve
# 群里发「帮助」应立刻有回应 → 长连接通了
```

### 8. 单推一条 / 查审计

```bash
poetry run hoteldata push now --group <群chatid> [--force]   # 手动推一次日报
poetry run hoteldata push log --today [--group <chatid>]      # 推送审计 + 成功率
poetry run hoteldata report ls                                # 22 项与当日桶/窗口
poetry run hoteldata report run svc_weekly                    # 单跑一项(默认 force)
poetry run hoteldata alert test --slot 09:00                  # 预警干跑(不发送不写状态)
poetry run hoteldata alert status
poetry run hoteldata review status
```

### 9. 验收

```bash
poetry run python scripts/verify_acceptance2.py    # V21–V60,约 2 分钟
poetry run python scripts/verify_acceptance.py     # V1–V20 回归
```

**当前状态:39 / 40 PASS,0 FAIL,1 BLOCKED**
(V21「真实企微服务器订阅」需真凭据 + 外网 → 如实标 BLOCKED,不伪装成 PASS)。
详见 [`docs/段2-验收记录.md`](docs/段2-验收记录.md);
**想自己上手摸一遍** → [`docs/段2-联调测试指南.md`](docs/段2-联调测试指南.md)
(离线自测 / 真实企微群联调 / 六个旧缺陷逐条复现 / 排错速查表)。

> ★ 协议层**不是 mock 测的**:验收器起一个**逐字节实现企微帧格式的真 WebSocket 服务端**
> (`FakeWeComServer`),于是帧编解码 / ack 匹配 / **迟到回执静默忽略** / 心跳判死重连 /
> 512KB 分片 / 限频时间戳 / 重试退避**全部走真实代码路径**。

---

## 段2 实现状态

| 批次 | 内容 | 状态 |
|---|---|---|
| **A** 机器人与推送地基 | 协议层(逐字节)· async 客户端 · 多机器人管理 · 素材上传 · 发送原语 · 派发器 · 审计 · 群绑定 | ✅ |
| **B** 命令与问答 | 18 条群命令 · FAQ(三形态配图)· 实时问答 · 消息处理链 | ✅ |
| **C** 日报推送 | 标题行 + 轮换图 · **图文绑定(缺图整项不发 + 告警)** · 去重 · 段3 比价钩子 | ✅ |
| **D** 报告节奏引擎 | 22 项 · 四项桶 · 条件 DSL(补 `in`)· 渲染契约 · 聚合与**周报环比** · 合并拆分 | ✅ |
| **E** 预警 | 6+1 规则 · 状态机 · 两层时刻 · 去重/清零/忽略 · 附图 · 送达率 · 每日汇总 | ✅ |
| **F** 点评交互 | 建议草稿 · 自动回复(三重门控)· 分析日报 · **append-only 审计** | ✅ |
| **G** 运维推送与收尾 | 自检推送 · 违约实时 · 端到端验收 | ✅ |

**实测数字**:段2 新增源码 ≈ **13,600 行**;`ruff check .` 全过;
迁移 `0003`(6 张表 + 1 列)+ `0004`(`alert_logs` 键拆分);任务注册表 9 → **19 个**;
**V21–V60:39 PASS / 0 FAIL / 1 BLOCKED**;段1 回归 **20/20 PASS**;

---

## 段2 修掉的 5 个旧缺陷(这才是段2 的核心价值)

| # | 旧缺陷 | 后果 | 段2 的做法 | 验收 |
|---|---|---|---|---|
| **D1** | 多机器人下 `set_bot(None)` → `send_alert` **恒返回 False** | 🔴 登录失效 / 机器人掉线等告警**根本发不出去** | 告警走 `BotManager.send_alert()`,**遍历在线机器人**,返回**逐群结果**(非裸 bool),失败写 `logger.error` | V23 专测 |
| **D5** | 限频键是 **chatid**(类文档写的是"机器人侧") | 🟡 30 机器人分摊**实际失效** | 限频键改为**机器人** + 保留群级二次节流 | V35 |
| **D9** | 条件 `in` 分支**不可达**(`op not in _OPS` 先返回 True) | 🟡 文档承诺的判定**恒真** | `in` 进算子白名单;验收**正例 + 反例成对** | V43 |
| **D10** | 周报 `compare` 硬编码 `None` | 🟡 周报没有上期列、没有环比 | 本期 + 上期两段聚合 → 渲染四列 | V45 |
| **D17** | `push_logs.bot_id` 列是 `INTEGER` 而代码写机器人名 | 🟡 审计不可靠,无法按机器人统计 | 迁移直接建 **`Text`** | V38 |
| **D18** | 「状态」命令读 `health["online"]`,而 `get_health()` 返回 `{名字: bool}` | 🟡 显示不正确 | `BotManager.health() -> dict[str, bool]` **统一契约** | V23 + V59 |

> 施工中还**新发现并修掉**了 6 个计划书里没有的问题(含一个客户端**订阅死锁**和一个
> 与 D9 同类的"`op` 形式条件恒真")—— 逐条见
> [`docs/段2-验收记录.md` §4](docs/段2-验收记录.md)。

---

## 常用命令(段2)

| 命令 | 说明 |
|---|---|
| `hoteldata bots list` / `add` / `delete` / `health` | 机器人管理 / 真连一次看每实例状态 |
| `hoteldata push now --group <chatid> [--force]` | 手动推一次日报(真实发送) |
| `hoteldata push log [--today] [--group] [--hotel] [--type]` | 推送审计 + 当日成功率 |
| `hoteldata bind list` / `add` / `remove` | 群 ↔ 酒店绑定 |
| `hoteldata alert test [--rule] [--slot] [--hotel] [--send]` | 预警干跑(**默认不发送不写状态**) |
| `hoteldata alert status` / `summary` | 预警状态 / 每日汇总 + 送达率 |
| `hoteldata review draft` / `auto` / `analysis` / `status` | 点评各环节 |
| `hoteldata report ls` / `run <item_id>` | 22 项排期 / 单跑一项 |

### 群内命令(18 条,逐字继承旧系统)

| 类别 | 命令 |
|---|---|
| 通用 | `帮助` |
| 绑定管理 | `绑定 <酒店名[,酒店名...]>` / `解绑 [酒店名]` / `我的酒店` |
| 数据 | `今日数据` / `重推` |
| **管理群**(11 条) | `汇总` / `状态` |
| **管理群** | `预警测试` / `预警状态` / `忽略此店 <店> [天数]` / `预警线 <店> 高 <v> 低 <v>` |
| **管理群** | `点评待办` / `点评状态` / `点评策略 <店> 差评 silent\|template` / `回复确认` / `已处理` / `已忽略` |

> ⚠️ **未配置 `MANAGE_CHATIDS` 时,管理群命令一律拒绝**(这是有意的安全默认,不是 bug)。

---

## 段2 三条必须理解的"业务事实"(写错就全盘错)

| 事实 | 为什么 |
|---|---|
| **「连续 7 天关房」是数据判定,不是 streak 门槛** | `unavailable_days` 由引擎从 `alert_room_states` **逐日推导**(可订即断、今日缺数据**保守不触发**);`alert_states.streak` **只是展示用**。改成"streak >= 7 才推"语义直接漂移(V48 专测) |
| **预警的时刻是两层** | 规则声明**名义时刻** 09:00/14:30/19:00(业务语义层);调度 cron 是 **09:04/14:34/19:04**(运维层错峰 +4 分钟);引擎再用 slot **映回**名义时刻去匹配。合并成一层,slot 匹配立刻失效(V49 专测) |
| **点评审计是 append-only,且有两套口径** | 状态流转**新增行不 UPDATE**;差评 `silent` → `ignored` **且 `replied=1`**(业务决定不回复=已处理完),自动失败 → `failed` **且 `replied=0`**(仍在待回复池)。把 `failed` 标成已回复会让点评永远消失(V56 专测) |

---

## 目录结构(段2 新增)

```
hotel-data-system/
├── config/                     # ★ 只放"运行规则"
│   ├── api_rules.json / push_rotation.json   # 段1(A 级遗产)
│   ├── report_schedule.json    #   22 项报告契约(A3-4)
│   ├── alert_rules.json / alert_shots.json / alert_lines.json
│   ├── review_templates.json / review_sources.json
│   └── prompts/alert_*.md      #   预警文案模板(10 个)
├── knowledge/faq.json          #   FAQ 知识库(A3-3,9 条)
├── src/hoteldata/
│   ├── push/                   # ★ 共享能力层(段3 复用)
│   │   ├── audit.py            #   push_logs 唯一写入口(去重 + 成功率)
│   │   ├── bindings.py         #   群 ↔ 酒店(UNIQUE 幂等,过滤 paused)
│   │   ├── sender.py           #   发送原语(3500 字拆分 / ≤5 图 / 素材缓存)
│   │   ├── dispatcher.py       #   ★ 限频(机器人级)+ 重试 + 异步队列
│   │   └── service.py          #   ★ 对段3 的推送契约(含 send_alert,D1 修复点)
│   ├── domains/
│   │   ├── bot/                # ★ 企微机器人域
│   │   │   ├── protocol.py     #   ★★ 协议层(10 命令字/req_id/心跳/分片)—— 逐字节继承
│   │   │   ├── client.py       #   单实例 async 客户端(订阅/收发/重连)
│   │   │   ├── manager.py      #   ★ 多实例 + 路由 + 健康(D1/D5/D18 修复点)
│   │   │   ├── uploader.py     #   素材三步上传(512KB 分片 + 内容缓存)
│   │   │   ├── commands.py / faq.py / realtime.py / router.py
│   │   ├── report/             # ★ 报告节奏引擎(22 项 + 日报)
│   │   ├── alert/              # ★ 预警(6+1 规则 + 状态机 + 附图 + 送达率)
│   │   ├── review/             # ★ 点评交互(草稿 / 门控 / 分析 / append-only 审计)
│   │   └── ops/                #   自检推送 + 违约实时
│   └── web/routes/             #   /healthz /status(含 robots + push 快照)
└── scripts/verify_acceptance2.py   # ★ 段2 验收执行器(V21–V60)
```

---

## 段2 的任务注册表(19 个;k8s/cron 之类**不存在**,时刻表只在这里)

| 任务 | cron | 补跑 | 说明 |
|---|---|---|---|
| `push.daily` | `0 9 * * *` | ✅ | 日报(标题行 + ≤5 张轮换图) |
| `push.schedule` | `0 9 * * *` | ✅ | 22 项报告节奏(**按群合并成 1~2 条**) |
| `alert.room` | `4 9,14,19 * * *` | ✅ | 关房预警(**错峰 +4 分钟**) |
| `alert.data` | `10 9 * * *` | ✅ | 数据预警 |
| `alert.summary` | `30 9 * * *` | ❌ | 每日汇总 + 送达率 |
| `review.suggest` | `50 8 * * *` | ✅ | 点评建议草稿 |
| `review.analysis` | `0 9 * * *` | ✅ | 点评分析日报 |
| `review.auto` | `40 9 * * *` | ✅ | 自动回复(三重门控) |
| `review.realtime` | `0 8-23 * * *` | ❌ | 实时回复轮询 |
| `ops.violation` | `0 * * * *` | ❌ | 违约实时监听(变化才推) |
| `ops.selfcheck` | `0 6 * * *` | ❌ | 自检 + 推运维群 |

> ★ **`catch_up` 的取舍**:内容型推送**可补跑**(晚推比不推好);
> **汇总与轮询类不补**(过时无意义)。
> ★ `review.realtime` 的 cron 与计划书字面"每 59 分钟"不同:``*/59`` 会展开成
> `[0,59]` 两分钟(上一小时 59 分与下一小时 0 分只隔 **1 分钟**,比 59 分钟更糟)
> → 取等价的整点 `0 8-23 * * *`。

---

## 段2 的三条硬约束(依赖纪律,与段1 一致)

1. 依赖方向**单向向下**,禁止反向 import。
2. **域之间不互相 import**(`domains/report` 需要推送就去调 `push/service.py`)。
3. **域之间不直接 join 别人的表**;段2 消费段1 数据**只能走 `domains/collect/service.py`**。

---

## 段1 快速开始(段2 未改动,照旧可用)

### 0. 前置

| 项 | 要求 | 校验 |
|---|---|---|
| Python | **3.14.x 标准版**(禁 free-threaded) | `python -c "import sysconfig; print(sysconfig.get_config_var('Py_GIL_DISABLED'))"` → `0` 或 `None` |
| 时区库 | `tzdata`(Windows 必需) | `python -c "from zoneinfo import ZoneInfo; print(ZoneInfo('Asia/Shanghai'))"` |
| PostgreSQL | 16(本机用 Docker) | `docker compose up -d pg` |
| Poetry | 2.x | `pip install poetry` |

### 1. 装依赖

```bash
poetry install
```

### 2. 配置

```bash
cp .env.example .env      # 按需改 DB_URL / MANAGE_CHATIDS / OPS_CHATID
```

> 🚫 时刻表**不进** `.env`:全部写在 `src/hoteldata/jobs.py` 的任务注册表里。
> 段1 的 `PATROL_TIME` / `SCREENSHOT_TIME` / `BACKUP_TIME` / `CLEANUP_TIME` 是
> **刻意的过渡安排**;段2 的 11 个任务**一步到位**写进注册表,一个时刻都不进 `.env`。

### 3. 起数据库 + 建表

```bash
docker compose up -d pg
poetry run alembic upgrade head        # 0001(7 表)→ 0002(4 表)→ 0003(6 表 + 1 列)→ 0004(键拆分)
```

### 4. 校验环境

```bash
poetry run hoteldata env          # 解释器 / tzdata / 目录 / 段1+段2 规则文件自检
poetry run hoteldata db ping      # 连不上 DB 直接失败,不静默降级
poetry run hoteldata rules check  # 规则 + 轮换清单一致性(带路径定位)
```

### 5. 登录一次(全系统唯一需要人工的步骤)

```bash
poetry run hoteldata login --platform ctrip --alias ctrip001
poetry run hoteldata sessions --check
```

> 旧系统的 `storage_states/*.json` 可**直接迁移**过来用,不必重新登录:
> `python scripts/migrate_states.py --from <旧系统根目录>`
> (登录态是**缓存**,过期了就得重登。)

### 6. 取数 / 截图

```bash
poetry run hoteldata collect --rotation        # 当日轮换 5 项 + 每日固定 7 项
poetry run hoteldata collect --screenshot      # 模块截图并回填
```

---

## 段1 实现状态

| 批次 | 内容 | 状态 |
|---|---|---|
| **A** 地基 | 项目骨架 · 配置 · 数据层 · Alembic(11 表)· Runtime · Web · 日志时区 | ✅ |
| **B** 会话与登录 | session_store(三元组寻址)· 浏览器池(上限+LRU+CDP)· 两层限频 · 登录管家 · 巡检 | ✅ |
| **C** 数据中心提取 | 规则引擎(热加载+强校验)· 提取器契约 · 双通道 · 四态 · 占位符守卫 · 轮换 · 落库 · CLI | ✅ |
| **D** 其余提取器 | 预警三源 · 房态 · 点评(**不回溯**) | ✅ |
| **E** 截图 | 独立截图器 · 两套就绪判定 · 交互语义 · COALESCE 回填 | ✅ |
| **F** 调度与运维 | 任务注册表 · `job_runs` + advisory lock · 补跑 · 清理 · 冷备 · 自检 | ✅ |

**实测数字**:段1 源码 57 文件;轮换 **21 天无重无漏**;限频实测最小相邻间隔 **0.6003s**(阈值 0.6s);
规则资产 **8 页 / 24 子模块 / 103 接口定义 / 485 字段 / 22 截图选择器** 原样继承。

**真平台端到端实测**:`collect --rotation` → 19 个「模块×窗口」→ 18 ok / 1 degraded / 0 failed
→ **246 个真实指标**,全部走 API 直连。

---

## 常用命令(段1)

| 命令 | 说明 |
|---|---|
| `hoteldata serve [--host] [--port]` | 启动全部服务(主入口) |
| `hoteldata env` | 环境自检(解释器 / tzdata / 目录 / 规则) |
| `hoteldata db ping` / `db upgrade` / `db downgrade` | 数据库 |
| `hoteldata rules check` | 校验 `api_rules.json` + 轮换清单一致性 |
| `hoteldata login --platform <ctrip\|meituan> --alias <别名>` | 人工登录 |
| `hoteldata sessions [--check]` | 查看登录态与有效性 |
| `hoteldata collect [--hotel] [--page] [--module] [--window] [--all-windows]` | 提取 |
| `hoteldata collect --rotation` / `--screenshot` | 只采当日轮换 / 只截图 |
| `hoteldata collect portal\|room\|review` | 批次 D 三个提取器 |
| `hoteldata rotation [--date] [--verify]` | 预览轮换 / 验证 21 天无重无漏 |
| `hoteldata task ls [--today]` / `run <task>` / `log <task>` | 任务管理 |
| `hoteldata ops patrol` / `backup` / `cleanup [--dry-run]` / `selfcheck` | 运维 |

### 脚本(`scripts/`)

| 脚本 | 用途 |
|---|---|
| `verify_acceptance.py` | **段1 验收执行器(V1–V20)** |
| `verify_acceptance2.py` | **段2 验收执行器(V21–V60)** |
| `migrate_states.py` | 迁移旧登录态到 `var/states/`(免重新登录) |
| `import_accounts.py` | 从旧 `db/ebooking.db` 只读导入账号与酒店 |
| `record_apis.py` | **接口录制**(S1 对策:平台改签名时重新录 `api_defs` 草稿) |
| `rescue_state.py` | 从"写了又被回滚"的状态文件里抢救有效登录态 |

---

## 接口契约:段1 → 段2/段3

段1 对外交付三样东西,段2/段3 只能通过它们取数,**不许绕过**:

| 契约 | 形态 |
|---|---|
| ① 结构化数据 | PG 表 `collect_reports` / `collect_modules` / `alert_portal_columns` / `alert_room_states` / `review_reviews` / `review_materials` |
| ② 查询服务 | `domains/collect/service.py`:`fetch_module_record()` / `build_payload()` / `aggregate_daily()` / `today_module_shots()` / `pick_screenshot()` |
| ③ 基础设施 | `infra/browser.py` 浏览器池 · `infra/session_store.py` 登录态 · `infra/rate_limit.py` 限频 · `infra/db.py` |

> 🚫 **禁止**:段2/段3 直接 import `domains/collect/` 内部的提取器实现,或直接读它的表。
> 要走 service。
>
> ✅ 段2 另有两个**放宽的例外**(都是"只读、不重写提取逻辑"):
> `domains/collect/repository.py`(公开仓储,用于播种/读取原始行)与
> `domains/collect/screenshot.py::Screenshoter.shot_url()`(现场单张截图,预警附图与
> 报告项「类型二」附图用;段1 的批量 `run()` 路径一行未改)。

---

## 业务事实清单(不是配置,是事实 —— 不许改)

### 段1

| 事实 | 出处 |
|---|---|
| `available=1` **当且仅当** `roomStatus=='G'`(售完但 `canUsedQuantity=0` **仍算可订**) | 旧 `room_state.py` |
| 星级 = `score.avgScoreSimple`,**是对象不是 int** | 旧 `comment_collector.py` |
| 情感阈值:**≥4 good,≤3 bad,无星级 unknown**;unknown **永不**自动回复 | 旧 `review_reply.py` |
| `addtime` 格式 `/Date(ms+0800)/` | 旧 `comment_collector.py` |
| "上周" = **上一自然周**(周一~周日),基准是**今日** | 旧 `api_collector.py:140-173` |
| `no_data` **不算失败** | 旧 `ebooking.py` |
| 请求间隔 **0.6s**、超时 **20s**、同时浏览器 **≤4** | 旧 `config.py` |
| 占位符守卫:**绝不携带空日期或占位符原文请求平台** | 旧 `_guard_placeholders` |

### 段2

| 事实 | 出处 |
|---|---|
| **回复必须回填入站 `req_id`**,否则群内看不到回复 | 企微协议 |
| **发送不等 ack**(避免重复回复) | 旧 `aibot.py:238-256` |
| **心跳连丢 2 次才判死**(丢 1 次不重连) | 旧 `aibot.py:33-35` |
| **无 news 卡片类型** → 图文 = md 文本 + **逐张**图片消息 | 企微协议 |
| 素材 **512KB/片**、**>100 片报错** | 旧 `aibot.py` |
| 预警的「连续 7 天」是**数据判定**,不是 streak | 旧 `alert_engine.py:479` |
| 差评 `silent` → `ignored` **且 `replied=1`**;失败 → **`replied=0` 仍算待回复** | 旧 `review_reply.py` |
| 条件 DSL 里 **`0`/`""`/`None`/空容器 = 无值**;字段缺失 → 条件 **`False`**(防误发) | 旧 `report_engine.py:185-199` |
| 单群日消息 **≤4 条**(甲方防轰炸):日报 1 + 报告 ≤2 + 点评分析 1 | 甲方口径 |

### 预警日志的键:`alert_logs` 一行 = 触发 × 收件人

| 列 | 值 | 职责 |
|---|---|---|
| `delivery_key` | `rule:hotel:entity:date:recipient` | **行身份**(UNIQUE) |
| `trigger_key` | `rule:hotel:entity:date` | **触发身份**(按它聚合"这条预警推给了谁") |

送达率的定义(计划书 §5.7:成功行 ÷ 总行,无管理群也要计入分母并写
`recipient='manage-none'`)本身就要求**行带收件人** —— `manage-none` 是一个收件人值。
一行代表一个触发的话,"推 3 个目标只留 1 行"既不能写成功(掩盖 B/C 的失败)
也不能写失败(抹掉 A 的成功)。

冲突策略是 **UPSERT(最后一次结果胜出)** 而不是 `DO NOTHING`:后者会让
`check(force=True)` 的重推结果被丢掉,表里留着旧的 `failed` 行 ——
运维看到"一直失败",实际早已恢复。稳态仍是 1 行/触发×目标,分母不变。

---

## 验收

| 段 | 执行器 | 结果 | 记录 |
|---|---|---|---|
| 段1 | `scripts/verify_acceptance.py`(V1–V20) | **20 / 20 PASS,0 BLOCKED** | [`docs/段1-验收记录.md`](docs/段1-验收记录.md) |
| 段2 | `scripts/verify_acceptance2.py`(V21–V60) | **39 PASS / 0 FAIL / 1 BLOCKED** | [`docs/段2-验收记录.md`](docs/段2-验收记录.md) |

```bash
.venv\Scripts\python.exe scripts\verify_acceptance2.py            # V21–V60
.venv\Scripts\python.exe scripts\verify_acceptance2.py --only V35 V36 V37
.venv\Scripts\python.exe scripts\verify_acceptance.py             # V1–V20 回归
```

> 段1 的验收器**用真实录制响应**驱动 API 通道(旧系统抓包样本在
> `docs/参考/旧系统/诊断证据/responses/`);段2 的验收器**用真 WebSocket 服务端**
> 驱动协议通道。**两者都不是 mock 造数据。**

## 视觉模型

`VISION_ENABLED=0`(**默认关闭**)。🔴 启用前必须取得**客户书面授权**:
GLM-4.6V 是第三方云服务,调用 = 把客户酒店经营数据截图**外发**到智谱服务器。
