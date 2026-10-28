from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from sync_calendar import SyncError, build_parser, load_settings, run


class CalendarOverrideTests(unittest.TestCase):
    def test_empty_source_cannot_publish_only_manual_additions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            overrides = root / "overrides.json"
            overrides.write_text(json.dumps({"add": [{
                "title": "Synthetic Makeup", "teacher": "T", "location": "R", "week": 1,
                "starts_at": "2026-03-02T08:00:00", "ends_at": "2026-03-02T09:00:00",
            }]}), encoding="utf-8")
            args = build_parser().parse_args([
                "--output-ics", str(root / "calendar.ics"), "--output-json", str(root / "timetable.json"),
            ])
            with patch.dict("os.environ", {
                "JW_USERNAME": "synthetic", "JW_PASSWORD": "synthetic", "AUTO_TERM": "false",
                "JW_SEMESTER": "2025-2026-2", "TERM_FIRST_MONDAY": "2026-03-02",
                "AUTO_EXCLUDE_HOLIDAYS": "false", "CALENDAR_OVERRIDES_PATH": str(overrides),
            }, clear=True), patch("sync_calendar.load_dotenv"):
                settings = load_settings(args)
            outputs = (settings.output_ics, settings.output_json, root / "term-summary.json")
            for output in outputs:
                output.write_bytes(b"previous published output")
            for failure in (None, SyncError("HTTP 403")):
                with self.subTest(failure=failure), patch("sync_calendar.QiangzhiAppClient") as client:
                    client.return_value.fetch_term.return_value = []
                    client.return_value.fetch_term.side_effect = failure
                    expected = "HTTP 403" if failure else "Timetable returned no events"
                    with self.assertRaisesRegex(SyncError, expected):
                        run(settings)
                for output in outputs:
                    self.assertEqual(output.read_bytes(), b"previous published output")

    def test_updated_end_must_follow_start_and_preserve_outputs(self) -> None:
        fixture = Path(__file__).resolve().parents[1] / "examples/sample-qz-app.json"
        for end in ("2026-03-02T07:59:00", "2026-03-02T08:00:00"):
            with self.subTest(end=end), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                overrides = root / "overrides.json"
                overrides.write_text(json.dumps({"update": [{
                    "title": "数据库应用", "starts_at": "2026-03-02T08:00:00", "ends_at": end,
                }]}), encoding="utf-8")
                args = build_parser().parse_args([
                    "--output-ics", str(root / "calendar.ics"),
                    "--output-json", str(root / "timetable.json"),
                ])
                with patch.dict("os.environ", {
                    "JW_USERNAME": "demo", "JW_PASSWORD": "demo", "JW_SEMESTER": "2025-2026-2",
                    "TERM_FIRST_MONDAY": date(2026, 3, 2).isoformat(),
                    "AUTO_EXCLUDE_HOLIDAYS": "false", "CALENDAR_OVERRIDES_PATH": str(overrides),
                }, clear=True), patch("sync_calendar.load_dotenv"):
                    settings = load_settings(args)
                settings.output_ics.write_text("existing calendar", encoding="utf-8")
                settings.output_json.write_text("existing data", encoding="utf-8")
                with self.assertRaises(SyncError):
                    run(settings, fixture)
                self.assertEqual(settings.output_ics.read_text(), "existing calendar")
                self.assertEqual(settings.output_json.read_text(), "existing data")


if __name__ == "__main__":
    unittest.main()
