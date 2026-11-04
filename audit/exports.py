"""Excel export of the access log — the filtered set, not just the page."""

from __future__ import annotations

import json

from django.utils import timezone
from openpyxl import Workbook

from core.exports import escape_spreadsheet, finalize_xlsx

#: Hard cap so a wide-open filter can't build a 3-million-row workbook.
EXPORT_ROW_CAP = 50_000

COLUMNS = (
    "Time (Dhaka)", "Actor", "Action", "Data class", "Subject", "Route", "Method",
    "Path", "Query", "Status", "IP", "Duration (ms)", "Object", "htmx", "Detail",
)


def build_access_log_export(queryset, filename: str):
    wb = Workbook()
    ws = wb.active
    ws.title = "Access log"
    ws.append(COLUMNS)
    tz = timezone.get_current_timezone()
    for row in queryset.select_related("actor", "subject")[:EXPORT_ROW_CAP]:
        obj = f"{row.object_type} {row.object_id}".strip()
        ws.append([
            timezone.localtime(row.occurred_at, tz).strftime("%Y-%m-%d %H:%M:%S"),
            escape_spreadsheet(row.actor_label),
            row.get_action_display(),
            row.get_data_class_display(),
            escape_spreadsheet(row.subject_label),
            escape_spreadsheet(row.route),
            row.method,
            escape_spreadsheet(row.path),
            escape_spreadsheet(row.query),
            row.status_code,
            row.ip_address or "",
            row.duration_ms,
            escape_spreadsheet(obj),
            "yes" if row.is_htmx else "",
            escape_spreadsheet(json.dumps(row.detail, sort_keys=True)) if row.detail else "",
        ])
    return finalize_xlsx(wb, filename, width_cap=60)
