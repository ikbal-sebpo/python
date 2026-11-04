"""The access-log viewer at /audit/access-log/. docs/ACCESS_LOG_PLAN.md §3.5."""

from __future__ import annotations

import datetime as dt
import uuid

from django.core.exceptions import PermissionDenied
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.generic import ListView

from accounts.permissions import can_view_access_log
from audit.capture import note
from audit.exports import EXPORT_ROW_CAP, build_access_log_export
from audit.models import AccessLog, Action, DataClass
from core.pagination import PageSizeMixin
from dynamic_menu.decorators import dynamic_allowed_users


def _parse_date(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


@method_decorator(dynamic_allowed_users, name="dispatch")
class AccessLogListView(PageSizeMixin, LoginRequiredMixin, ListView):
    """Filterable list of AccessLog rows. ProtectedView ``audit_access_log``
    is the menu/route gate (User Admins + Audit Viewers; superusers bypass);
    ``can_view_access_log`` re-checks the same rule in case the row is ever
    edited wider by hand."""

    model = AccessLog
    template_name = "audit/access_log_list.html"
    context_object_name = "rows"
    paginate_by = 25

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_view_access_log(request.user, request=request):
            raise PermissionDenied("You can't open the access log.")
        return super().dispatch(request, *args, **kwargs)

    # -- filters ------------------------------------------------------------

    def _filters(self) -> dict:
        g = self.request.GET
        today = timezone.localdate()
        date_from = _parse_date(g.get("from"))
        date_to = _parse_date(g.get("to"))
        if date_from is None and date_to is None and not g.get("subject") and not g.get("actor"):
            # Default window: last 7 days, so an unfiltered open isn't a 2-year scan.
            date_from, date_to = today - dt.timedelta(days=6), today
        return {
            "actor": (g.get("actor") or "").strip(),
            "subject": (g.get("subject") or "").strip(),
            "data_class": g.get("data_class") or "",
            "action": g.get("action") or "",
            "route": (g.get("route") or "").strip(),
            "path": (g.get("path") or "").strip(),
            "date_from": date_from,
            "date_to": date_to,
            "fragments": g.get("fragments") == "1",
        }

    def get_queryset(self):
        f = self._filters()
        qs = AccessLog.objects.select_related("actor", "subject")
        tz = timezone.get_current_timezone()
        if f["date_from"]:
            qs = qs.filter(occurred_at__gte=timezone.make_aware(dt.datetime.combine(f["date_from"], dt.time.min), tz))
        if f["date_to"]:
            qs = qs.filter(occurred_at__lt=timezone.make_aware(dt.datetime.combine(f["date_to"] + dt.timedelta(days=1), dt.time.min), tz))
        if f["actor"]:
            qs = qs.filter(Q(actor_label__icontains=f["actor"]) | Q(actor__employee_id__iexact=f["actor"]))
        if f["subject"]:
            subject_uuid = _as_uuid(f["subject"])
            if subject_uuid:
                qs = qs.filter(subject_id=subject_uuid)
            else:
                qs = qs.filter(Q(subject_label__icontains=f["subject"]) | Q(subject__employee_id__iexact=f["subject"]))
        if f["data_class"] in DataClass.values:
            qs = qs.filter(data_class=f["data_class"])
        if f["action"] in Action.values:
            qs = qs.filter(action=f["action"])
        if f["route"]:
            qs = qs.filter(route__icontains=f["route"])
        if f["path"]:
            qs = qs.filter(path__icontains=f["path"])
        if not f["fragments"]:
            qs = qs.pages()
        return qs

    # -- export / render ----------------------------------------------------

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "xlsx":
            qs = self.get_queryset()
            count = qs.count()
            filename = f"access-log-{timezone.localdate():%Y-%m-%d}.xlsx"
            note(request, action=Action.EXPORT, data_class=DataClass.AUDIT,
                 rows=min(count, EXPORT_ROW_CAP), file=filename, filters=self._filter_summary())
            return build_access_log_export(qs, filename)
        return super().get(request, *args, **kwargs)

    def _filter_summary(self) -> dict:
        f = self._filters()
        return {k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in f.items() if v}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        f = self._filters()
        ctx.update({
            "filters": f,
            "date_from": f["date_from"],
            "date_to": f["date_to"],
            "data_classes": DataClass.choices,
            "actions": Action.choices,
            "export_cap": EXPORT_ROW_CAP,
        })
        return ctx
