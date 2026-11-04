# Read-only. Piped into `manage.py shell` on the Orbis server by timelog.py:
#   ssh orbis-prod "cd ~/orbis && TL_EMP=539 TL_FROM=... TL_TO=... venv/bin/python manage.py shell" < orbis_source.py
# Prints one JSON object between markers: {date: {shift, category, minutes}}.
# Shift resolution is the Timesheet's own (attendance.views._resolve_shifts_matrix:
# Shift row wins, else the weekly pattern inside the employment window), so a
# day counts as working exactly when Orbis's "Unaccounted" check would.
import datetime as _dt
import json as _json
import os as _os

from accounts.models import CustomUser as _User
from attendance.models import AttendancePunch as _Punch
from attendance.views import _resolve_shifts_matrix as _resolve

_emp = _User.objects.get(employee_id=_os.environ["TL_EMP"])
_lo = _dt.date.fromisoformat(_os.environ["TL_FROM"])
_hi = _dt.date.fromisoformat(_os.environ["TL_TO"])
_dates = [_lo + _dt.timedelta(days=i) for i in range((_hi - _lo).days + 1)]
_shifts = _resolve([_emp], _dates)
_punch = {p.date: p for p in _Punch.objects.filter(employee=_emp, date__gte=_lo, date__lte=_hi)}
_out = {}
for _d in _dates:
    _row = _shifts.get((_emp.pk, _d))
    _p = _punch.get(_d)
    _minutes = None
    if _p is not None and _p.check_in is not None:
        _td = _p.time_in_office
        if _td is None and _p.check_out is not None:
            _td = _p.check_out - _p.check_in
        _minutes = int(round(_td.total_seconds() / 60)) if _td is not None else 0
    _out[_d.isoformat()] = {
        "shift": (_row.shift_attribute.name if _row is not None and _row.shift_attribute
                  else (_row.shift_type.name if _row is not None else None)),
        "category": _row.shift_type.category if _row is not None else None,
        "minutes": _minutes,
    }
print("@@TIMELOG@@" + _json.dumps(_out) + "@@END@@")
