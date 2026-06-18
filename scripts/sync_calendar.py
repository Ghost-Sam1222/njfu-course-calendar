#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

DEFAULT_BASE_URL = "https://jwxt.njfu.edu.cn"
DEFAULT_TZ = "Asia/Shanghai"


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    base_url: str
    username: str
    password: str
    semester: str
    first_monday: date
    term_weeks: int
    calendar_name: str
    timezone_id: str
    output_ics: Path
    output_json: Path
    provider: str


@dataclass(frozen=True)
class CourseEvent:
    title: str
    teacher: str
    location: str
    starts_at: datetime
    ends_at: datetime
    week: int
    raw: dict[str, Any]


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def require_env(name: str) -> str:
    value = env(name)
    if not value:
        raise SyncError(f"Missing required environment variable: {name}")
    return value


def infer_semester(today: date) -> str:
    # Chinese universities commonly use "-1" for autumn and "-2" for spring.
    if today.month >= 8:
        return f"{today.year}-{today.year + 1}-1"
    return f"{today.year - 1}-{today.year}-2"


def parse_date(value: str, name: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SyncError(f"{name} must use YYYY-MM-DD, got {value!r}") from exc


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_settings(args: argparse.Namespace) -> Settings:
    load_dotenv(Path(".env"))
    today = date.today()
    first_monday = require_env("TERM_FIRST_MONDAY")
    return Settings(
        base_url=env("JW_BASE_URL", DEFAULT_BASE_URL).rstrip("/") + "/",
        username=require_env("JW_USERNAME"),
        password=require_env("JW_PASSWORD"),
        semester=env("JW_SEMESTER", infer_semester(today)),
        first_monday=parse_date(first_monday, "TERM_FIRST_MONDAY"),
        term_weeks=int(env("TERM_WEEKS", "20")),
        calendar_name=env("CALENDAR_NAME", "南林课表"),
        timezone_id=env("CALENDAR_TIMEZONE", DEFAULT_TZ),
        output_ics=Path(args.output_ics),
        output_json=Path(args.output_json),
        provider=env("JW_PROVIDER", "qz_app"),
    )


class QiangzhiAppClient:
    def __init__(self, settings: Settings) -> None:
        try:
            import requests
        except ImportError as exc:
            raise SyncError("Missing dependency: requests. Run `pip install -r requirements.txt`.") from exc

        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
                "Accept": "application/json, text/plain, */*",
            }
        )

    def login(self) -> str:
        params = {
            "method": "authUser",
            "xh": self.settings.username,
            "pwd": self.settings.password,
        }
        payload = self._request_json("login", params=params)
        if not isinstance(payload, dict):
            raise SyncError(f"Unexpected login response: {payload!r}")
        token = payload.get("token")
        if not token or token == "-1":
            message = payload.get("msg") or payload.get("message") or payload.get("error") or payload
            raise SyncError(f"Login failed: {message}")
        return str(token)

    def fetch_week(self, token: str, week: int) -> list[dict[str, Any]]:
        params = {
            "method": "getKbcxAzc",
            "xh": self.settings.username,
            "xnxqid": self.settings.semester,
            "zc": str(week),
        }
        payload = self._request_json(f"week {week}", params=params, headers={"token": token})
        if isinstance(payload, dict) and payload.get("token") == "-1":
            raise SyncError(f"Timetable request for week {week} was rejected: token invalid")
        if not isinstance(payload, list):
            raise SyncError(f"Unexpected timetable response for week {week}: {payload!r}")
        return payload

    def fetch_term(self) -> list[dict[str, Any]]:
        token = self.login()
        rows: list[dict[str, Any]] = []
        for week in range(1, self.settings.term_weeks + 1):
            for item in self.fetch_week(token, week):
                if isinstance(item, dict):
                    item["_week"] = week
                    rows.append(item)
        return rows

    def _request_json(
        self,
        label: str,
        params: dict[str, str],
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        errors: list[str] = []
        endpoint = urljoin(self.settings.base_url, "app.do")
        for method in ("get", "post"):
            request = getattr(self.session, method)
            kwargs = {"params": params} if method == "get" else {"data": params}
            response = request(endpoint, headers=headers, timeout=30, **kwargs)
            try:
                return self._json_response(response, f"{label} {method.upper()}")
            except SyncError as exc:
                errors.append(str(exc))
        raise SyncError("; ".join(errors))

    def _json_response(self, response: Any, label: str) -> Any:
        text = response.text.strip()
        if response.status_code >= 400:
            raise SyncError(f"{label} HTTP {response.status_code}: {text[:200]}")
        try:
            return response.json()
        except ValueError as exc:
            raise SyncError(
                f"{label} did not return JSON. First 200 chars: {text[:200]!r}"
            ) from exc


def parse_time_value(value: Any, field_name: str) -> time:
    if value is None:
        raise SyncError(f"Missing time field: {field_name}")
    text = str(value).strip()
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, pattern).time()
        except ValueError:
            pass
    raise SyncError(f"Invalid time in {field_name}: {text!r}")


def weekday_from_kcsj(kcsj: str) -> int:
    if not kcsj or not kcsj[0].isdigit():
        raise SyncError(f"Invalid kcsj weekday: {kcsj!r}")
    weekday = int(kcsj[0])
    if weekday < 1 or weekday > 7:
        raise SyncError(f"Invalid kcsj weekday: {kcsj!r}")
    return weekday


def normalize_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or fallback


def course_rows_to_events(settings: Settings, rows: list[dict[str, Any]]) -> list[CourseEvent]:
    events: list[CourseEvent] = []
    seen: set[str] = set()
    for row in rows:
        title = normalize_text(row.get("kcmc"), "未命名课程")
        if title in {"无", "无课"}:
            continue
        week = int(row.get("_week", 0))
        kcsj = normalize_text(row.get("kcsj"))
        if not week or not kcsj:
            continue
        class_date = settings.first_monday + timedelta(weeks=week - 1, days=weekday_from_kcsj(kcsj) - 1)
        start = datetime.combine(class_date, parse_time_value(row.get("kssj"), "kssj"))
        end = datetime.combine(class_date, parse_time_value(row.get("jssj"), "jssj"))
        if end <= start:
            end = start + timedelta(minutes=45)
        event = CourseEvent(
            title=title,
            teacher=normalize_text(row.get("jsxm")),
            location=normalize_text(row.get("jsmc")),
            starts_at=start,
            ends_at=end,
            week=week,
            raw=row,
        )
        key = stable_event_uid(event)
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
    return sorted(events, key=lambda item: (item.starts_at, item.ends_at, item.title))


def stable_event_uid(event: CourseEvent) -> str:
    identity = "|".join(
        [
            event.title,
            event.starts_at.isoformat(),
            event.ends_at.isoformat(),
            event.location,
            event.teacher,
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{digest}@course-calendar-sync"


def ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold_ics_line(line: str) -> str:
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts: list[str] = []
    current = ""
    current_len = 0
    for char in line:
        char_len = len(char.encode("utf-8"))
        limit = 75 if not parts else 74
        if current_len + char_len > limit:
            parts.append(current)
            current = char
            current_len = char_len
        else:
            current += char
            current_len += char_len
    parts.append(current)
    return "\r\n ".join(parts)


def fmt_local(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def generate_ics(settings: Settings, events: list[CourseEvent]) -> str:
    now = fmt_utc(datetime.now(timezone.utc))
    cal_id = uuid.uuid5(uuid.NAMESPACE_URL, settings.base_url + settings.username)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//course-calendar-sync//NJFU Timetable//CN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(settings.calendar_name)}",
        f"X-WR-TIMEZONE:{ics_escape(settings.timezone_id)}",
        f"X-WR-RELCALID:{cal_id}",
    ]
    for event in events:
        description_parts = [f"第{event.week}周"]
        if event.teacher:
            description_parts.append(f"教师：{event.teacher}")
        kkzc = normalize_text(event.raw.get("kkzc"))
        if kkzc:
            description_parts.append(f"节次：{kkzc}")
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{stable_event_uid(event)}",
                f"DTSTAMP:{now}",
                f"DTSTART;TZID={settings.timezone_id}:{fmt_local(event.starts_at)}",
                f"DTEND;TZID={settings.timezone_id}:{fmt_local(event.ends_at)}",
                f"SUMMARY:{ics_escape(event.title)}",
                f"LOCATION:{ics_escape(event.location)}",
                f"DESCRIPTION:{ics_escape(chr(10).join(description_parts))}",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


def write_json(path: Path, events: list[CourseEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "title": event.title,
            "teacher": event.teacher,
            "location": event.location,
            "starts_at": event.starts_at.isoformat(),
            "ends_at": event.ends_at.isoformat(),
            "week": event.week,
            "raw": event.raw,
        }
        for event in events
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_raw_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SyncError(f"Raw JSON must be a list, got {type(payload).__name__}")
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if "_week" not in item:
            raise SyncError("Every raw JSON row must include _week when using --raw-json")
        rows.append(item)
    return rows


def run(settings: Settings, raw_json: Optional[Path] = None) -> None:
    if raw_json:
        rows = load_raw_rows(raw_json)
    else:
        if settings.provider != "qz_app":
            raise SyncError(f"Unsupported JW_PROVIDER: {settings.provider}")
        rows = QiangzhiAppClient(settings).fetch_term()
    events = course_rows_to_events(settings, rows)
    settings.output_ics.parent.mkdir(parents=True, exist_ok=True)
    settings.output_ics.write_text(generate_ics(settings, events), encoding="utf-8")
    write_json(settings.output_json, events)
    print(
        f"Generated {settings.output_ics} with {len(events)} events "
        f"for {settings.semester}, weeks 1-{settings.term_weeks}."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Qiangzhi timetable to an iCalendar file.")
    parser.add_argument("--output-ics", default="public/calendar.ics")
    parser.add_argument("--output-json", default="data/timetable.json")
    parser.add_argument("--raw-json", help="Read raw Qiangzhi app timetable rows from a JSON file.")
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        settings = load_settings(args)
        run(settings, Path(args.raw_json) if args.raw_json else None)
        return 0
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
