# 南林课表 Apple 日历同步

把南京林业大学强智教务系统课表转换成可订阅的 iCalendar 文件，适合 iPhone、iPad、Mac 自带日历 App 订阅。

## 工作方式

GitHub Actions 定时运行脚本，登录教务系统，拉取整学期课表，生成：

- `public/calendar.ics`：给 Apple 日历订阅
- `data/timetable.json`：便于排查和后续扩展

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python scripts/sync_calendar.py
```

编辑 `.env` 时注意：

- `JW_USERNAME`：学号
- `JW_PASSWORD`：教务系统密码
- `JW_SEMESTER`：学期，例如 `2025-2026-2`。不填时会按日期自动推断。
- `TERM_FIRST_MONDAY`：教学第一周周一日期，这个必须准确，否则日历日期会偏移。

`.env` 已被 `.gitignore` 忽略，不要提交。

不登录教务系统也可以先验证 iCalendar 生成：

```bash
JW_USERNAME=demo JW_PASSWORD=demo TERM_FIRST_MONDAY=2026-02-23 \
python scripts/sync_calendar.py --raw-json examples/sample-qz-app.json
```

## GitHub Actions 配置

在仓库的 Settings 中配置：

Secrets:

- `JW_USERNAME`
- `JW_PASSWORD`

Variables:

- `TERM_FIRST_MONDAY`，例如 `2026-02-23`
- `JW_SEMESTER`，例如 `2025-2026-2`
- `TERM_WEEKS`，默认 `20`
- `CALENDAR_NAME`，默认 `南林课表`
- `JW_BASE_URL`，默认 `https://jwxt.njfu.edu.cn`

工作流文件在 `.github/workflows/sync-calendar.yml`，默认每 6 小时同步一次，也支持手动运行。

## 发布订阅地址

最简单的方式是开启 GitHub Pages：

1. 进入仓库 Settings -> Pages。
2. Source 选择 `Deploy from a branch`。
3. Branch 选择 `main`，目录选择 `/ (root)`。
4. 保存后等待 Pages 部署完成。

之后 Apple 日历订阅地址类似：

```text
https://<你的 GitHub 用户名>.github.io/<仓库名>/public/calendar.ics
```

在 iPhone 上：设置 -> 日历 -> 账户 -> 添加账户 -> 其他 -> 添加已订阅的日历。

在 Mac 上：日历 App -> 文件 -> 新建日历订阅。

## 重要说明

生成的 `.ics` 只包含课程名、上课时间、地点、教师，不包含密码。  
如果仓库或 GitHub Pages 是公开的，课程地点和时间也会公开。介意隐私时，请使用私有托管或难猜路径。

南京林业大学网页登录入口会跳统一认证。本项目默认使用 Playwright 无头浏览器模拟真实网页登录，再解析网页课表生成订阅日历。
