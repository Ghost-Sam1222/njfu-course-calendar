# Repository Guidelines

## Project Structure & Module Organization

This repository builds an Apple Calendar subscription from the Nanjing Forestry University teaching timetable.

- `scripts/sync_calendar.py` contains all sync logic: login, timetable parsing, holiday filtering, optional exam parsing, JSON export, and iCalendar generation.
- `.github/workflows/sync-calendar.yml` runs the scheduled GitHub Actions sync and commits generated artifacts.
- `examples/sample-qz-app.json` is a small fixture for local generation checks without logging in.
- `public/calendar.ics` is the published calendar file served by GitHub Pages.
- `data/timetable.json` is generated debug/output data. Treat it as an artifact, not hand-authored source.
- `.env.example` documents local and Actions configuration variables.

## Build, Test, and Development Commands

Create a local environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Run the real sync after creating `.env` from `.env.example`:

```bash
python scripts/sync_calendar.py
```

Run an offline fixture check:

```bash
JW_USERNAME=demo JW_PASSWORD=demo TERM_FIRST_MONDAY=2026-03-02 \
AUTO_EXCLUDE_HOLIDAYS=false \
python scripts/sync_calendar.py --raw-json examples/sample-qz-app.json
```

Run a syntax check:

```bash
python -m py_compile scripts/sync_calendar.py
```

## Coding Style & Naming Conventions

Use Python 3.12-compatible code, 4-space indentation, type hints, and small pure helper functions where possible. Keep environment variables uppercase, for example `TERM_FIRST_MONDAY`, `MAKEUP_DATES`, and `INCLUDE_EXAMS`. Prefer dataclasses for structured settings/events and avoid broad refactors around the single-script design unless the workflow genuinely grows.

## Testing Guidelines

There is no formal test suite yet. For every change, run `py_compile` and the offline fixture command above. When changing timetable parsing, verify the generated `public/calendar.ics` contains expected `DTSTART`, `DTEND`, `SUMMARY`, and `LOCATION` fields. Avoid requiring live school login for routine checks.

## Commit & Pull Request Guidelines

History uses short imperative or descriptive commits such as `sync course calendar`, `allow school makeup class dates`, and `lower sync frequency and document data sources`. Keep commits scoped. Pull requests should describe behavior changes, list configuration variables affected, mention whether generated files changed, and include manual verification commands.

## Security & Configuration Tips

Never commit `.env`, passwords, cookies, or browser session data. Store `JW_USERNAME` and `JW_PASSWORD` only as GitHub Actions secrets. Public Pages exposes course names, times, locations, and teachers; do not add private data to event descriptions.
