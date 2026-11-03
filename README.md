# PlaneTicketTracker

免费的机票价格追踪器：GitHub Actions 每天抓取 Google Flights（`fast-flights`），数据存在 `docs/data/`，GitHub Pages 展示。

## 启用
1. 合并到 `main`，Settings → Pages → Source 选 `main` / `/docs`。
2. Actions → track-prices → Run workflow，手动跑一次验证抓取。
3. 邮件（可选）：Settings → Secrets → Actions 添加 `SMTP_USER`（Gmail 地址）、`SMTP_PASS`（Gmail 应用专用密码）、`ALERT_TO`（收件地址）。
4. `config.json` 里改航线、日期、降价阈值（`below_price` / `pct_below_avg`）。

## 说明
- 淡旺季图的“估算”为经验值；“实测”随每日记录自动叠加。
- 抓取非官方 API，Google 改版或拦截时会失败，失败会写入 `docs/data/status.json`，全部失败时发邮件。

## 访问页面
合并到 `main` 并启用 Pages 后，地址是：`https://user202201.github.io/planetickettracker/`
