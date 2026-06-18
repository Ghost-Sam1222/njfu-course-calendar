#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
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
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://jwxt.njfu.edu.cn"
DEFAULT_TZ = "Asia/Shanghai"
TIMETABLE_URL = "https://jwxt.njfu.edu.cn/jsxsd/xskb/xskb_list.do?Ves632DSdyV=NEW_XSD_PYGL"
LOGIN_ENTRY_URL = "https://jwxt.njfu.edu.cn/jsxsd/framework/xsMainV.jsp"
DEFAULT_EXAM_URLS = [
    "https://jwxt.njfu.edu.cn/jsxsd/xsks/xsksap_list.do",
    "https://jwxt.njfu.edu.cn/jsxsd/xsks/xsksap_list",
]
DEFAULT_HOLIDAY_ICS_URLS = [
    "https://raw.githubusercontent.com/YangH9/ChinaCalendar/master/cal_holiday_1.ics",
]
SECTION_TIMES = {
    1: ("08:00", "08:45"),
    2: ("08:55", "09:40"),
    3: ("10:00", "10:45"),
    4: ("10:55", "11:40"),
    5: ("14:00", "14:45"),
    6: ("14:50", "15:35"),
    7: ("15:55", "16:40"),
    8: ("16:45", "17:30"),
    9: ("18:30", "19:15"),
    10: ("19:20", "20:05"),
    11: ("20:15", "21:00"),
    12: ("21:05", "21:50"),
}


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
    excluded_dates: frozenset[date]
    auto_exclude_holidays: bool
    holiday_ics_urls: tuple[str, ...]
    include_exams: bool
    exam_urls: tuple[str, ...]


@dataclass(frozen=True)
class CourseEvent:
    title: str
    teacher: str
    location: str
    starts_at: datetime
    ends_at: datetime
    week: int
    raw: dict[str, Any]
    event_type: str = "course"


@dataclass(frozen=True)
class BrowserFetchResult:
    timetable_html: str
    exam_html: Optional[str] = None


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


def parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_date_set(value: Optional[str], name: str) -> frozenset[date]:
    if not value:
        return frozenset()
    dates: set[date] = set()
    for raw_part in re.split(r"[,，\n]", value):
        part = raw_part.strip()
        if not part:
            continue
        separator = ".." if ".." in part else "~" if "~" in part else None
        if separator:
            start_text, end_text = [item.strip() for item in part.split(separator, 1)]
            start = parse_date(start_text, name)
            end = parse_date(end_text, name)
            if end < start:
                raise SyncError(f"{name} range ends before it starts: {part!r}")
            cursor = start
            while cursor <= end:
                dates.add(cursor)
                cursor += timedelta(days=1)
        else:
            dates.add(parse_date(part, name))
    return frozenset(dates)


def parse_url_list(value: Optional[str]) -> tuple[str, ...]:
    if not value:
        return tuple(DEFAULT_EXAM_URLS)
    return tuple(part.strip() for part in re.split(r"[,，\n]", value) if part.strip())


def parse_holiday_url_list(value: Optional[str]) -> tuple[str, ...]:
    if not value:
        return tuple(DEFAULT_HOLIDAY_ICS_URLS)
    return tuple(part.strip() for part in re.split(r"[,，\n]", value) if part.strip())


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
        excluded_dates=parse_date_set(
            env("EXCLUDE_DATES", env("SKIP_DATES")),
            "EXCLUDE_DATES",
        ),
        auto_exclude_holidays=parse_bool(env("AUTO_EXCLUDE_HOLIDAYS"), default=True),
        holiday_ics_urls=parse_holiday_url_list(env("HOLIDAY_ICS_URLS", env("HOLIDAY_ICS_URL"))),
        include_exams=parse_bool(env("INCLUDE_EXAMS"), default=False),
        exam_urls=parse_url_list(env("EXAM_URLS", env("EXAM_URL"))),
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


async def fetch_web_pages_with_browser(settings: Settings) -> BrowserFetchResult:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SyncError("Missing dependency: playwright. Run `pip install -r requirements.txt`.") from exc

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        try:
            await page.goto(LOGIN_ENTRY_URL, wait_until="domcontentloaded", timeout=60000)
            if "authserver/login" in page.url:
                await page.fill("#username", settings.username)
                await page.fill("#password", settings.password)
                await page.click('button[type="submit"]')
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
                await page.wait_for_timeout(2000)
            if "authserver/login" in page.url:
                raise SyncError("Browser login stayed on the unified-auth login page.")
            await page.goto(TIMETABLE_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_selector("#timetable", timeout=60000)
            timetable_html = await page.content()
            exam_html = None
            if settings.include_exams:
                for exam_url in settings.exam_urls:
                    try:
                        await page.goto(exam_url, wait_until="domcontentloaded", timeout=60000)
                        content = await page.content()
                        if looks_like_exam_page(content):
                            exam_html = content
                            break
                    except Exception as exc:  # pragma: no cover - depends on school website.
                        print(f"warning: could not fetch exam page {exam_url}: {exc}", file=sys.stderr)
                if exam_html is None:
                    print("warning: exam import is enabled, but no exam page was recognized.", file=sys.stderr)
            return BrowserFetchResult(timetable_html=timetable_html, exam_html=exam_html)
        finally:
            await browser.close()


def is_hidden_tag(tag: Any) -> bool:
    style = (tag.get("style") or "").replace(" ", "").lower()
    return "display:none" in style


def looks_like_exam_page(html: str) -> bool:
    return "考试" in html and any(word in html for word in ("考试时间", "考试日期", "考试地点", "课程名称", "考试安排"))


def expand_weeks(week_spec: str) -> list[int]:
    text = normalize_text(week_spec)
    match = re.search(r"([0-9,\-\s]+)\(周\)", text)
    if not match:
        return []
    weeks: set[int] = set()
    for part in match.group(1).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
            weeks.update(range(start, end + 1))
        else:
            weeks.add(int(part))
    if "单周" in text:
        weeks = {week for week in weeks if week % 2 == 1}
    if "双周" in text:
        weeks = {week for week in weeks if week % 2 == 0}
    return sorted(weeks)


def parse_sections(week_spec: str) -> list[int]:
    match = re.search(r"\[([0-9\-\s]+)节\]", normalize_text(week_spec))
    if not match:
        return []
    return [int(value) for value in re.findall(r"\d+", match.group(1))]


def time_for_sections(sections: list[int]) -> tuple[time, time]:
    if not sections:
        raise SyncError("Course is missing class sections.")
    start_text = SECTION_TIMES[min(sections)][0]
    end_text = SECTION_TIMES[max(sections)][1]
    return parse_time_value(start_text, "section start"), parse_time_value(end_text, "section end")


def parse_course_div(div: Any) -> list[dict[str, str]]:
    courses: list[dict[str, str]] = []
    current: dict[str, str] = {}

    def flush() -> None:
        nonlocal current
        if current.get("title") and current.get("week_spec"):
            courses.append(current)
        current = {}

    for child in div.children:
        text = normalize_text(child.get_text(" ", strip=True) if hasattr(child, "get_text") else str(child))
        if not text:
            continue
        if set(text) <= {"-"} and len(text) >= 5:
            flush()
            continue
        if getattr(child, "name", None) != "font" or is_hidden_tag(child):
            continue
        title = normalize_text(child.get("title"))
        if title == "教师":
            current["teacher"] = text
        elif title == "周次(节次)":
            current["week_spec"] = text
        elif title == "教室":
            current["location"] = text
        elif not title and "title" not in current:
            current["title"] = text
    flush()
    return courses


def web_html_to_events(settings: Settings, html: str) -> list[CourseEvent]:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise SyncError("Missing dependency: beautifulsoup4. Run `pip install -r requirements.txt`.") from exc

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", {"id": "timetable"})
    if table is None:
        raise SyncError("Could not find #timetable in the rendered timetable page.")
    events: list[CourseEvent] = []
    seen: set[str] = set()
    rows = table.find_all("tr")
    for row in rows[1:]:
        cells = row.find_all("td", recursive=False)
        for weekday_index, cell in enumerate(cells, start=1):
            for div in cell.find_all("div", class_="kbcontent", recursive=False):
                if is_hidden_tag(div):
                    continue
                for course in parse_course_div(div):
                    sections = parse_sections(course["week_spec"])
                    start_time, end_time = time_for_sections(sections)
                    for week in expand_weeks(course["week_spec"]):
                        class_date = settings.first_monday + timedelta(weeks=week - 1, days=weekday_index - 1)
                        event = CourseEvent(
                            title=course["title"],
                            teacher=course.get("teacher", ""),
                            location=course.get("location", ""),
                            starts_at=datetime.combine(class_date, start_time),
                            ends_at=datetime.combine(class_date, end_time),
                            week=week,
                            raw={
                                "source": "web",
                                "week_spec": course["week_spec"],
                                "sections": sections,
                                "weekday": weekday_index,
                            },
                        )
                        key = stable_event_uid(event)
                        if key not in seen:
                            seen.add(key)
                            events.append(event)
    return sorted(events, key=lambda item: (item.starts_at, item.ends_at, item.title))


def infer_exam_year(settings: Settings) -> int:
    match = re.match(r"(\d{4})-(\d{4})-([12])", settings.semester)
    if not match:
        return settings.first_monday.year
    start_year, end_year, term = match.groups()
    return int(end_year if term == "2" else start_year)


def normalize_header(value: str) -> str:
    return re.sub(r"[\s:：()（）\[\]【】]", "", normalize_text(value))


def find_row_value(row: dict[str, str], keywords: tuple[str, ...], avoid: tuple[str, ...] = ()) -> str:
    for header, value in row.items():
        if value and all(word in header for word in keywords) and not any(word in header for word in avoid):
            return value
    return ""


def parse_exam_date(text: str, settings: Settings) -> Optional[date]:
    normalized = normalize_text(text)
    patterns = [
        r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})",
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if match:
            year, month, day = (int(item) for item in match.groups())
            return date(year, month, day)
    match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日?", normalized)
    if match:
        month, day = (int(item) for item in match.groups())
        return date(infer_exam_year(settings), month, day)
    return None


def parse_exam_time_range(text: str) -> Optional[tuple[time, time]]:
    normalized = normalize_text(text)
    match = re.search(r"(\d{1,2}:\d{2})\s*(?:-|~|－|—|至|到)\s*(\d{1,2}:\d{2})", normalized)
    if not match:
        return None
    start_text, end_text = match.groups()
    return parse_time_value(start_text, "exam start"), parse_time_value(end_text, "exam end")


def exam_row_to_event(settings: Settings, row: dict[str, str]) -> Optional[CourseEvent]:
    title = (
        find_row_value(row, ("课程",), avoid=("代码", "编号"))
        or find_row_value(row, ("科目",))
        or find_row_value(row, ("名称",))
    )
    date_text = find_row_value(row, ("日期",)) or find_row_value(row, ("时间",))
    time_text = find_row_value(row, ("时间",)) or date_text
    exam_date = parse_exam_date(date_text + " " + time_text, settings)
    time_range = parse_exam_time_range(time_text)
    if not title or exam_date is None or time_range is None:
        return None
    location = (
        find_row_value(row, ("地点",))
        or find_row_value(row, ("考场",))
        or find_row_value(row, ("教室",))
    )
    seat = find_row_value(row, ("座位",)) or find_row_value(row, ("座号",))
    start_time, end_time = time_range
    raw = {"source": "exam", "row": row}
    if seat:
        raw["seat"] = seat
    return CourseEvent(
        title=f"期末考试：{title}",
        teacher="",
        location=location,
        starts_at=datetime.combine(exam_date, start_time),
        ends_at=datetime.combine(exam_date, end_time),
        week=0,
        raw=raw,
        event_type="exam",
    )


def exam_html_to_events(settings: Settings, html: str) -> list[CourseEvent]:
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise SyncError("Missing dependency: beautifulsoup4. Run `pip install -r requirements.txt`.") from exc

    soup = BeautifulSoup(html, "html.parser")
    events: list[CourseEvent] = []
    seen: set[str] = set()
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        headers: list[str] = []
        for row in rows:
            cells = row.find_all(["th", "td"], recursive=False)
            values = [normalize_text(cell.get_text(" ", strip=True)) for cell in cells]
            if not any(values):
                continue
            normalized = [normalize_header(value) for value in values]
            if not headers and any("课程" in item or "科目" in item for item in normalized) and any(
                "时间" in item or "日期" in item for item in normalized
            ):
                headers = normalized
                continue
            if not headers or len(values) < 2:
                continue
            row_data = {
                headers[index]: value
                for index, value in enumerate(values[: len(headers)])
                if value
            }
            event = exam_row_to_event(settings, row_data)
            if event is None:
                continue
            key = stable_event_uid(event)
            if key not in seen:
                seen.add(key)
                events.append(event)
    return sorted(events, key=lambda item: (item.starts_at, item.ends_at, item.title))


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


def filter_excluded_dates(settings: Settings, events: list[CourseEvent]) -> list[CourseEvent]:
    excluded_dates = set(settings.excluded_dates)
    if settings.auto_exclude_holidays:
        excluded_dates.update(fetch_holiday_dates(settings))
    if not excluded_dates:
        return events
    return [
        event
        for event in events
        if event.event_type != "course" or event.starts_at.date() not in excluded_dates
    ]


def fetch_holiday_dates(settings: Settings) -> set[date]:
    dates: set[date] = set()
    for url in settings.holiday_ics_urls:
        try:
            text = fetch_text(url)
            dates.update(parse_holiday_ics_dates(text))
        except Exception as exc:  # pragma: no cover - depends on external service.
            print(f"warning: could not fetch holiday calendar {url}: {exc}", file=sys.stderr)
    if dates:
        term_start = settings.first_monday
        term_end = settings.first_monday + timedelta(weeks=settings.term_weeks, days=-1)
        dates = {item for item in dates if term_start <= item <= term_end}
        print(f"Loaded {len(dates)} holiday skip dates from external calendar.")
    return dates


def fetch_text(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "course-calendar-sync/1.0 (+https://github.com/Ghost-Sam1222/njfu-course-calendar)",
            "Accept": "text/calendar,text/plain,*/*",
        },
    )
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def unfold_ics_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")) and lines:
            lines[-1] += raw_line[1:]
        elif raw_line:
            lines.append(raw_line)
    return lines


def split_ics_property(line: str) -> tuple[str, str]:
    if ":" not in line:
        return "", ""
    name, value = line.split(":", 1)
    return name.split(";", 1)[0].upper(), value.strip()


def parse_ics_date_value(value: str) -> Optional[date]:
    match = re.match(r"(\d{4})(\d{2})(\d{2})", value.strip())
    if not match:
        return None
    year, month, day = (int(item) for item in match.groups())
    return date(year, month, day)


def expand_date_span(start: date, end_exclusive: Optional[date]) -> set[date]:
    if end_exclusive is None or end_exclusive <= start:
        end_exclusive = start + timedelta(days=1)
    dates: set[date] = set()
    cursor = start
    while cursor < end_exclusive:
        dates.add(cursor)
        cursor += timedelta(days=1)
    return dates


def parse_holiday_ics_dates(text: str) -> set[date]:
    dates: set[date] = set()
    event: dict[str, str] = {}
    in_event = False
    for line in unfold_ics_lines(text):
        if line == "BEGIN:VEVENT":
            event = {}
            in_event = True
            continue
        if line == "END:VEVENT":
            summary = event.get("SUMMARY", "")
            if "假期" in summary and "补班" not in summary:
                start = parse_ics_date_value(event.get("DTSTART", ""))
                end = parse_ics_date_value(event.get("DTEND", ""))
                if start:
                    dates.update(expand_date_span(start, end))
            event = {}
            in_event = False
            continue
        if not in_event:
            continue
        name, value = split_ics_property(line)
        if name in {"SUMMARY", "DTSTART", "DTEND"}:
            event[name] = value
    return dates


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
        description_parts = ["期末考试" if event.event_type == "exam" else f"第{event.week}周"]
        if event.teacher:
            description_parts.append(f"教师：{event.teacher}")
        kkzc = normalize_text(event.raw.get("kkzc"))
        if kkzc:
            description_parts.append(f"节次：{kkzc}")
        sections = event.raw.get("sections")
        if sections:
            description_parts.append(f"节次：{min(sections):02d}-{max(sections):02d}节")
        seat = normalize_text(event.raw.get("seat"))
        if seat:
            description_parts.append(f"座位：{seat}")
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
            "event_type": event.event_type,
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
        events = course_rows_to_events(settings, rows)
    elif settings.provider == "qz_browser":
        pages = asyncio.run(fetch_web_pages_with_browser(settings))
        events = web_html_to_events(settings, pages.timetable_html)
        if pages.exam_html:
            events.extend(exam_html_to_events(settings, pages.exam_html))
    else:
        if settings.provider != "qz_app":
            raise SyncError(f"Unsupported JW_PROVIDER: {settings.provider}")
        rows = QiangzhiAppClient(settings).fetch_term()
        events = course_rows_to_events(settings, rows)
    events = filter_excluded_dates(settings, events)
    events = sorted(events, key=lambda item: (item.starts_at, item.ends_at, item.title))
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
