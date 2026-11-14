# ARCHITECTURE — 给开发者 / AI 的上下文文档

> 目的：以后用 AI（或自己）改这个项目时，先读这一份，不用重新翻代码。
> README.md 面向“使用者/运维”（怎么运转、出问题怎么办）；本文面向“开发”（每个模块的结构与实现方式、为什么这样设计、踩过的坑）。
> 改了结构后请同步更新本文。最后更新：2026-11-14。

---

## 0. 一句话

免费、全托管的机票价格追踪：**GitHub Actions 定时抓 Google Flights → 把结果存成 JSON 提交回仓库 → GitHub Pages 的静态网页读 JSON 展示**。没有服务器、没有数据库。

- 线上页面：`https://user202201.github.io/planetickettracker/`（仓库 `user202201/PlaneTicketTracker`，公开仓库才免费）
- 默认分支：`claude/admiring-goodall-vjmsqt`（因为仓库一开始是空的，没有 main）。Pages 从该分支的 `docs/` 发布。如果将来改名为 `main`，必须同步改 Pages 源，以及各 workflow 里的 `github.ref_name` 默认引用（它们都用 `github.ref_name`，通常不用改）。
- 旅行需求（决定了所有默认配置）：蒙特利尔 YUL → 北京 PEK / 上海 PVG / 郑州 CGO；经济舱、1 成人、最多转 1 次；出发 3/4/5 月任意日，6 月仅 18 日起，7/8 月要求 9/15 前返程，10 月任意日；停留约 42 天。

## 1. 目录结构

```
config.json                  # 唯一配置：航线、月份、矩阵、节奏、告警
README.md                    # 运维手册（中文）
ARCHITECTURE.md              # 本文
CLAUDE.md                    # 指向本文，AI 自动读取
tracker/
  fetch.py                   # 核心：抓取 + 节奏控制 + 矩阵 + 接力断点提交 (~810 行)
  next_run.py                # 接力决策：是否/多久后再触发下一批
  summarize.py               # 规则式三语“智能总结” → docs/data/summary.json
  probe.py                   # 一天中不同时段价格探测 → docs/data/tod/<route>.json
  alert.py                   # SMTP 降价邮件（默认关闭，需 Secrets）
  diagnose.py                # 诊断工具，写 docs/data/diagnose.json（手动触发）
  requirements.txt           # fast-flights==3.1.0, flights==0.9.0, typing_extensions, protobuf
.github/workflows/
  track.yml                  # 主采集（cron + 接力 + 手动/推送触发）
  probe.yml                  # 时段探测（每天 3 次）
  health.yml                 # 数据 >2 天未更新 → 自动开/关 Issue
  keepalive.yml              # 每月 1、15 号提交一次，防 60 天无活动被停用
  diagnose.yml               # 推送 .github/run-diagnose.txt 触发诊断
.github/run-trigger.txt      # 推送它 = 触发 track.yml（调试用）
.github/run-diagnose.txt     # 推送它 = 触发 diagnose.yml
docs/                        # GitHub Pages 根
  index.html                 # 整个前端（单文件，原生 JS + Chart.js）
  data/                      # 全部数据（由机器人提交）
```

## 2. 数据流

```
cron / 手动 / 接力 ──► track.yml
   pip install → python tracker/fetch.py
        ├─ 第 1 遍：所有矩阵航线的“日历列”（每月 1 次请求，稠密，几乎不限流）→ git_push("data: calendar columns")
        └─ 第 2 遍：PEK 的“精确列”（42 天，逐格 list 请求，被限流）+ 各航线窗口航班列表 → checkpoint 提交
   → python tracker/summarize.py → summary.json
   → python tracker/alert.py（如配置）
   → git commit/push 数据
   → 接力步骤：python tracker/next_run.py 打印延迟分钟数 → gh workflow run track.yml -f delay=N
```

页面只读 `docs/data/*.json`，**不会实时查价**。搜索栏只是在已采集数据里筛选/换算。

## 3. 外部数据源与关键行为（踩坑的结论，勿忘）

库：
- `flights==0.9.0`（fli）：真正使用的。`SearchFlights()._fetch_flights(filters, currency, language, country, capture_session)`；日历：`SearchDates`/`GetCalendarGraph`，`DateSearchFilters.format()` 的最后一个元素 `[dmin,dmax]` 是**停留天数范围**；`parse_first_wrb_payload`；`with_locale_params`。
- `fast-flights==3.1.0`：仅为兼容保留，`preflight()` 会 import 它，导入失败 → 整个 run 红色失败（防止“静默全失败”）。注意它的 `max_stops=1` 是“直飞”，我们不用。
- Google 枚举 MaxStops：ANY=0, NON_STOP=1, ONE_STOP_OR_FEWER=2, TWO=3。
- 预订/固定链接用的 Google Flights URL `tfs` 参数是 protobuf（前端 JS 自己编码）：FlightData 字段 date=2、max_stops=5、airlines=6、最早/最晚起飞小时=8/9、from=13、to=14、connecting_airports=15；Info：data=3、passengers=8（packed）、seat=9、trip=19。

Google 的行为：
1. **对 GitHub Runner 的 IP 限流**：list 请求大约 5~12 次后开始返回错误/94 字节空响应，几分钟后恢复。**日历端点不限流**。→ 整个架构围绕这点设计。
2. 日历/特惠最长只支持 **32 天**停留，所以“42 天精确价”只能靠 list 请求。
3. list 结果里“去程航班的价格”= Google 默认搭配的返程价，不是最低返程；所以对最便宜的去程还要补查返程（`_fli_return`，`expand`）。
4. **返程日对价格影响很大**（同一出发日，返程差一天可能 1811 vs 2216）。不要假设“停留天数无关紧要”。
5. 售票国家（point-of-sale）对价格无影响。
6. 郑州 CGO 线路日历几乎没有数据（冷门线路），只有零星日期有价。

## 4. tracker/fetch.py（核心）

### 4.1 全局状态
`PACE`（节奏参数）、`NET={"fails","pauses","stop"}`（网络状态）、`CHK`（检查点时间）、`PENDING`（剩余任务数）、`HORIZON={"days":330}`（可订天数上限，超出视为未开售，静默跳过）、`EXPAND`、`PRICING`。

### 4.2 节奏控制（防限流）
- `pace_init(cfg)`：读 `pacing` + `schedule.quota_range`，随机本次运行的请求配额与批次大小。
- `pace()`：每个请求间随机 sleep（`query_sleep_seconds_range`），每批后随机停顿（`pause_minutes_range`）。
- `guard()`：连续 3 次失败 → 暂停一次约 8 分钟 → 仍失败则 `NET["stop"]` 并抛异常停止本次运行（`stop:"throttled"`）。
- `time_up()`（仅时间预算 `max_runtime_minutes`）vs `expired()`（停止标志 / 配额 / 时间）。日历阶段只用 `time_up()`，因为日历不限流。
- `checkpoint(save, force)`：每 45 分钟保存并 `git_push(msg)`；`git_push` 只在 CI 里执行：`pull --rebase --autostash -X theirs` → `push HEAD:<branch>`。目的：run 被取消/超时也不丢数据。

### 4.3 数据获取函数
- `month_specs(mx)`：把 `matrix.months`（字符串或 `{month, from_day, return_by}`）规范化。
- `dates_for(route, cfg)`：窗口航班列表要查的日期（`sample_step`、`return_by`、horizon、跳过矩阵已覆盖的月份）。
- `fli_list(route, dep, ret, currency, expand)`：一次往返 list 请求（2 次尝试 + `guard()`），可选补查返程。
- `_itin(x)` / `_fli_offers(res)`：把 fli 返回规范成统一结构（见 §6）；`_ap()` + `AIRPORTS` → `docs/data/airports.json`（IATA→机场名）。
- `_fli_return(...)`：给一个去程查返程选项，得到 `ret` 对象及 `total`。
- `search(route, dep, ret, currency, expand)`：对外入口（mode "list"）。
- `calendar_range(route, first, last, dmin, dmax, currency)`：**一次请求**返回 `{(dep_iso, ret_iso): price}`，停留范围 `[dmin,dmax]`；当前用 28~32。
- `calendar_prices` / `run_matrix_calendar` / `oneway`：旧方案（估算、旧日历），保留作回退（`pricing` 为 `calendar`/`oneway_sum` 时走）。当前 `pricing:"list"`。

### 4.4 价格矩阵 `run_matrix_cells(route,cfg,cur,doc,today,errors,flush,only)`
矩阵 = 出发日 × 停留天数。两类列：
- **日历列**（`calendar_offsets` [28..32]）：每月 1 次 `calendar_range`，稠密；同时把每个出发日当天最低价写入 `doc["history"][date][today]`（走势图的稠密日序列来源）。
- **精确列**（`return_offsets` [42]，仅 `list_routes` 里的航线，目前只有 PEK）：每格一次 list 请求，带完整航班详情。

关键状态（函数内闭包）：
- `grid`：`{dep_iso: [price|None × len(allo)]}`，`allo = cal_offs + list_offs`，前 `nc` 个是日历列。今日快照文件存在且 `offsets` 一致就续用。
- `cells`：`{dep: {"42": {"ret","offers":[…],"d":"抓取日"}}}`，即 `offers.json` 内容。
- **跨天携带（2026-11 新增）**：`cells` 不再只保留当天；加载时保留所有未过期出发日，缺 `d` 的补成文件 `_date`。`restore_and_pick()` 把 cells 里的价格填回今天快照的空格，若 `d != today` 则登记到 `stale["dep|42"]=抓取日`；快照里以 `"st"` 字段输出。
- 精确列查询顺序：① 从未有价的格子 ② 价格最旧的携带格子（`stale` 值升序）③ 日历价最低的日期；同层随机打散。新查到的格子 → `stale.pop`，`cells[...]["d"]=today`。
- `pending()`：**只统计精确格子**中“无价或价格是旧的”数量。（日历格子不计，否则像郑州这样永远补不上的线路会让接力无限重试。）
- `valid(d,off,is_cal)`：校验该格是否应存在（`return_by`、horizon；`date_step>1` 时抽样日期，当前 `date_step:1` = 每天）。
- `want_expand(price)`：精确格子只有价格在当天最低价 `expand_within_pct`(8%) 以内才补查返程，省请求。
- `_pick(offers, n=3)`：每格只存最便宜 3 个 + 最快 1 个，控制文件体积。
- `exact_top_per_month`：>0 时只对日历价最便宜的 N 个出发日查精确列；现在是 0 = 全部日期（配合跨天携带）。

### 4.5 `main()`
`preflight()` → 读 config → `pace_init` → 随机打乱航线顺序 → 每条矩阵航线 `prep(route)`（读/写 `docs/data/<route>.json`、`save_doc` 含瘦身 + `HISTORY_DAYS` 截断、清掉已过期出发日详情）→
第 1 遍：全部矩阵航线只跑日历列并提交；第 2 遍：每条航线窗口列表（`dates_for`）+ `run_matrix(..., only="list")`。
结束写 `status.json`（ok/failed/errors）与 `progress.json`：
`{date, pending, updated, ok, requests, stop, runs_today, zero_streak}`。`zero_streak` = 连续“没拿到任何数据”的 run 数。

## 5. 其它 Python 模块
- **next_run.py**：读 `progress.json`。无活（pending=0 / 非今天）或 `runs_today>=30` 或 `zero_streak>=4` → 不输出（不再接力）。`ok<=0` → 25~40 分钟后重试；`stop=="throttled"` → 30~45 分钟；否则 4~12 分钟。
- **summarize.py**：`load(route)` 只用日历列、且列布局一致的快照，忽略覆盖率 <70% 的最新快照；`summarize()` 产出三语（zh/en/fr）规则式要点：月度最低/最贵、星期几、提前多久买、时段（读 `tod/<route>.json`）。字典 `CAT`、`MON`、`WDN`、`SLOT`。输出 `summary.json = {updated, routes:{id:{days, latest, bullets:{zh,en,fr}}}}`。
- **probe.py**：对 `config.tod.routes` 的一批样本格子（每 `every` 个取 1），按 UTC 小时判定 am/noon/pm，写入 `tod/<route>.json = {slots, obs:{dep:{date:{slot:price}}}}`。
- **alert.py**：`route_stats` + SMTP 发信；需要 Secrets `SMTP_USER/SMTP_PASS/ALERT_TO`；**尚未配置**，且没有“每天一封”的去重（启用前要加）。
- **diagnose.py**：一组探测函数（`list_probe`、`calendar_probe`、`calendar_compare`、`calendar_range_probe`、`span_probe` 等）。默认只跑最近一个“轻量”探测；`--all` 才跑旧用例（会耗尽请求额度）。结果写 `docs/data/diagnose.json`。

## 6. 数据格式（docs/data）

`<route>.json`（如 YUL-PEK.json）
```
{ currency, updated,
  route:{id,origin,dest,max_stops,cabin,adults,children,windows,stay_days,pricing,months},
  history:{ "<出发日>": { "<采集日>": 最低价 } },          # 走势/淡旺季/星期分析的原料
  latest:{ "<出发日>": { return:"<返程日>", offers:[offer…] } } }
offer = { airlines[], stops, duration(分钟), via[], dep:"YYYY-MM-DD HH:MM", arr, lay[](中转分钟),
          legs:[{a,b,dep,arr,min,al,no,c(IATA航司码),ac(机型)}], price, ret?:{同结构+total} }
```
`matrix/<route>/<UTC日期>.json`：`{months, offsets:[28..32,42], cal:[28..32], currency, pricing, st:{"dep|42":"抓取日"}, p:{dep:[price…]}}`
`matrix/<route>/index.json`：`{snapshots:[日期…], months}`
`matrix/<route>/offers.json`：`{_date, <dep>:{"42":{ret,offers,d}}}`（跨天携带）
`tod/<route>.json`、`summary.json`、`progress.json`、`status.json`、`airports.json`、`seasonality.json`（静态：月度季节指数 + 目的地系数，用于“预估”柱）、`diagnose.json`。

## 7. GitHub Workflows
- **track.yml**：cron `7 14 * * *`（每天主启动，约蒙特利尔 9 点）+ `37 17,20,23 * * *`（巡检，没活几秒退出）；`push` 触发（改 `.github/run-trigger.txt`）；`workflow_dispatch` 输入 `mode`(full|diagnose)、`delay`。并发组 `track`（同时只有一个在跑，一个排队；新排队会顶掉旧排队）。权限 `contents: write` + `actions: write`（用于自己触发下一批）。步骤：checkout(ref=`github.ref_name`) → pip install → git 身份 → “Relay delay”（按 `delay` sleep）→ 定时触发时随机延迟 → fetch → summarize → alert → 提交数据 → 接力步骤（`if: (success() || failure()) && inputs.mode != 'diagnose'`，**不要改成 always()**，否则手动取消也会触发接力）。timeout 100 分钟。
- **probe.yml**：每天 3 次，并发组 `probe`。
- **health.yml**：每天 2 次；`progress.json` 的日期 >2 天前 → 开 Issue「[机票追踪] 数据采集可能已停止」，恢复后自动关闭。
- **keepalive.yml**：每月 1、15 日往 `.github/keepalive.txt` 提交。
- **diagnose.yml**：推送 `.github/run-diagnose.txt` 触发。
- 手动取消 run 很慢（30~90 秒），期间新排队会替换旧排队，容易出现“重复重跑”——调试时别连点。

## 8. 前端 docs/index.html（单文件，约 810 行）

技术：原生 JS、Chart.js（CDN）、CSS 变量做明/暗主题。无构建步骤，改了直接提交即可（Pages 部署约 1~2 分钟，浏览器要 Ctrl+Shift+R）。

模块（函数）：
- **基础**：`applyTheme`（auto/light/dark，存 localStorage）、`applyI18n` + `I18N{zh,en,fr}`（`t(key,…args)`，`{0}` 占位）、`j(url)` 取 JSON（no-cache）、`mk(tag,txt,cls)` 造节点、`draw(id,cfg)` 画图、`cv(color,alpha)`（**canvas 不认 CSS 变量/color-mix，必须先解析成 rgba**）、`store`（localStorage 封装）。
- **设置面板**：主题、语言、综合排序权重 `inTv`（时间价值 元/小时）、`inSp`（每次转机成本）。综合成本 `cost = price + tv*(duration/60) + sp*stops`（`sortCfg`，`buildSort`/`initWeights`）。
- **搜索栏（`#f`，`run()`）**：出发地/目的地下拉（PEK/PVG/CGO）；出发日期（改后自动刷新，返程日只读 = 出发 + `stay_days`）；成人/儿童数量；停靠次数、舱位已隐藏（数据固定为 ≤1 转、经济舱）；月份按钮 `#chips` 来自 `config.windows`。
  - **人数换算**：`PF() = 成人 + 0.75×儿童`，`PX(v)=round(v*PF())`。作用于航班列表（`rows` 构建时）、走势图、矩阵单元格、Top10、KPI。儿童按成人价 75% 是**估算假设**。点击详情/Google 链接目前仍是 1 成人。
  - `run()` 做的事：读 `seasonality.json` 画“预估 vs 实测”月度柱；读路线 JSON；以出发日 ±7 天（`flex`）取 `latest` 过滤出航班行；画走势图；然后依次 `analyze`、`loadMatrix`、`loadSummary`、`loadTod`、`renderTrack`、`renderDiscover`。
- **航班卡片**：`renderRows`（排序：best/price/fast/stops）、`detail`（展开详情）、`itinSection`/`legList`/`timeline`（类似加航的可视化时间线）、`renderKpis`/`kpi`。
- **预订链接**：`bookLinks`（Google Flights 固定链接 `googlePinned` + 其它站点搜索）、`pbLeg`/`NAME2CODE`（把所选航班编码成 protobuf tfs，使链接直达该航班）。
- **价格矩阵 `loadMatrix`/`draw_`**：月份、查看日期(快照)、对比快照(显示 ▲▼ 差额)、价格区间过滤、“只看精确价”；**精确列排在最前**（`ord`）；日历列标“参考”；旧价格格子下方显示 `⏱ MM-DD 查询`（来自快照 `st`）；格子可点击 → `openCell(dep,off,isLatest,isCal)` 弹窗显示航班（读 `offers.json`，日历格没有详情则只给预订链接）。
- **Top 10 `renderTop`**：最新快照里每个出发日取最低价，排前 10，CSS 类 `.toprow`（注意不是 `.top`，早期类名冲突曾导致搜索栏撑满页面）。
- **Google 追踪 `renderTrack` / `renderDiscover`**：每月最便宜日期组合一键打开 Google（再手动开“跟踪票价”）；自然语言灵活日期搜索按钮（停留天数取 min(stay,30)，因为 Google 灵活搜索的上限）。
- **分析 `analyze` / `loadTod` / `loadSummary`**：星期几、提前多久买、一天中时段、规则总结；都靠 `history` 累积，**采集不满几周这些图是空的，属正常**。机场名查表 `AIR`。

## 9. config.json 字段速查
`currency`(CAD) · `default_route` · `stay_days`(42) · `flex_days`(7) · `sample_step`(窗口列表抽样) · `windows[]`({center, return_by?}) · `routes[]`({id,origin,dest,max_stops,cabin,adults,children}) · `matrix{months[], return_offsets[42], calendar_offsets[28..32], date_step(1), routes[], list_routes[PEK], exact_top_per_month(0)}` · `pricing`("list") · `expand_within_pct`(8) · `booking_horizon_days`(330) · `pacing{batch_size_range, pause_minutes_range, query_sleep_seconds_range, max_runtime_minutes(75)}` · `schedule.quota_range`([50,100]) · `tod{routes,every}` · `alert{email_to, below_price, pct_below_avg}`。

## 10. 设计取舍与已知限制
- 限流是第一约束：日历列（稠密）保证每日走势/淡旺季数据；精确列（稀疏、带详情）靠接力 + 跨天携带慢慢补。每天约 30~50 个精确格被查到，约 200 格需要 4~7 天轮一遍。
- 上海 PVG 只有日历列，没有精确列（`list_routes` 只有 PEK）；郑州 CGO 日历几乎空。
- 日历参考价与 42 天精确价：PEK 很接近（实测 4/6 完全一致，其余 ≤2.5%），PVG 一致性较差。
- 邮件提醒默认关闭；最稳的提醒是 Google 自带“跟踪票价”。
- 仓库会增长，历史压缩（squash）需用户同意，会改写历史。
- 抓取是非官方方式，Google 改版可能导致失败 → health Issue 会提醒；先看 Actions 日志，再用 diagnose。

## 11. 开发/调试备忘
- 本地没有 Google 的访问，测试用 monkeypatch：把 `F.search`、`F.calendar_range`、`F.expired`、`F.time_up` 换成假函数，`F.DATA` 指向临时目录，再调 `run_matrix_cells`（这样验证过跨天携带）。
- 前端语法检查：把 `<script>` 抽出来 `node --check`。
- 推送被拒：机器人经常先提交数据 → `git pull --rebase --autostash origin <branch>` 再 push。
- 机器人提交数据时用 `-X theirs`，不要把手改的数据文件和采集同时进行；要改数据文件时先暂停/等一批结束。
- 手动触发一批：Actions → track-prices → Run workflow（`delay=0`）；或改 `.github/run-trigger.txt` 并推送。
- Commit 规范：消息末尾带 Co-Authored-By / Claude-Session 行（见仓库既有提交）。

## 12. 相关但独立的项目
- **LEOGoals**（私有 Expo/React-Native PWA，儿童行为记录，本地 SQLite）：与本项目无关。若要托管到手机，评估过 Cloudflare Pages（支持私有仓库，`_headers` 设置 COOP/COEP 以支持 OPFS SQLite）与 GitHub Pages（需公开仓库）。用户尚未决定，也没有授权改动。

## 13. 待办 / 未决（截至 2026-11-14）
- 搜索栏：点击详情/Google 链接带上人数；
- 郑州 CGO：保留还是去掉（日历几乎无数据，每次浪费请求）；
- 上海是否也加精确列；
- 邮件告警：用户加 Secrets 后再启用，并加“每天一封”去重；
- “去航司官网查价”链接（提过，未做）；
- 默认分支改名 `main`、git 历史压缩：都需要用户同意；
- 免费额度到期（约 11 月 14 日）后确认 Actions/Pages 仍可用（公开仓库应当免费）。
