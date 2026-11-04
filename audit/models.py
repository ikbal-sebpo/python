"""Access log — who accessed which data or page, and when.

One append-only table for *reads* across the whole application (the domain
change ledgers — ShiftChangeLog, LeaveBalanceTransaction, PayrollRunAudit,
PolicyAcknowledgementEvent … — keep answering "what changed"). Rows are
written by ``audit.middleware.AccessLogMiddleware`` for every authenticated
request that reaches a view, and by ``audit.signals`` for login/logout/
failed-login. Spec: docs/ACCESS_LOG_PLAN.md.

Posture:

- **Append-only.** ``save()`` refuses an update and ``delete()`` refuses
  outright; only ``manage.py prune_access_log`` removes rows, by queryset,
  on the retention schedule. The admin is read-only. Production additionally
  revokes UPDATE on the table from the app's MySQL user (deploy/README.md).
- **No money, no secrets.** ``detail`` is a dict of counts, filenames and
  filter values; ``save()`` rejects a key that looks like an amount, the same
  way core/tests_money.py refuses an unformatted amount in a template.
- Both FKs are SET_NULL with a label snapshot beside them, so a row still
  reads after the person is deleted — PayrollRunAudit's posture.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class DataClass(models.TextChoices):
    PAGE = "PAGE", "Page"
    PROFILE = "PROFILE", "Employee profile"
    SALARY = "SALARY", "Salary / payroll"
    TAX = "TAX", "Tax"
    LOAN = "LOAN", "Salary loan"
    LEAVE = "LEAVE", "Leave"
    TIMESHEET = "TIMESHEET", "Timesheet"
    ROSTER = "ROSTER", "Roster"
    DOCUMENT = "DOCUMENT", "Document"
    POLICY = "POLICY", "Policy"
    OFFBOARDING = "OFFBOARDING", "Offboarding"
    USER_ADMIN = "USER_ADMIN", "User administration"
    ADMIN = "ADMIN", "Django admin"
    API = "API", "API"
    AUTH = "AUTH", "Authentication"
    AUDIT = "AUDIT", "Audit log"


class Action(models.TextChoices):
    VIEW = "VIEW", "View"
    WRITE = "WRITE", "Write"
    EXPORT = "EXPORT", "Export"
    DOWNLOAD = "DOWNLOAD", "Download"
    LOGIN = "LOGIN", "Login"
    LOGOUT = "LOGOUT", "Logout"
    LOGIN_FAILED = "LOGIN_FAILED", "Login failed"
    DENIED = "DENIED", "Denied"


#: ``detail`` keys that would carry a money figure. Checked on save so a
#: well-meaning ``note(request, amount=...)`` can never land in the log.
FORBIDDEN_DETAIL_KEYS = frozenset({
    "amount", "gross", "net", "tds", "salary", "balance", "total", "pay",
    "instalment", "installment", "tax", "password", "token", "secret",
})


class AccessLogQuerySet(models.QuerySet):
    def pages(self):
        """Full page loads only — drop the htmx drawer/fragment rows."""
        return self.filter(is_htmx=False)


class AccessLog(models.Model):
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="access_log_as_actor",
    )
    actor_label = models.CharField(
        max_length=150, blank=True,
        help_text="Email snapshot; a service-account name for API calls; "
                  "'anonymous' for the public verify pages.",
    )
    session = models.CharField(
        max_length=16, blank=True,
        help_text="Hashed prefix of the session key — groups one sitting, never the key itself.",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)

    method = models.CharField(max_length=8)
    route = models.CharField(
        max_length=200, blank=True, db_index=True,
        help_text="Resolver route name, e.g. payroll_salary_detail or admin:payroll_salarystructure_change.",
    )
    path = models.CharField(max_length=500)
    query = models.CharField(max_length=500, blank=True, help_text="Query string with secret keys stripped.")
    status_code = models.PositiveSmallIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    is_htmx = models.BooleanField(default=False)

    data_class = models.CharField(max_length=20, choices=DataClass.choices, default=DataClass.PAGE, db_index=True)
    action = models.CharField(max_length=16, choices=Action.choices, default=Action.VIEW, db_index=True)

    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="access_log_as_subject",
        help_text="The employee whose data was accessed, when the request names one.",
    )
    subject_label = models.CharField(max_length=150, blank=True)
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    objects = AccessLogQuerySet.as_manager()

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        verbose_name = "Access log entry"
        verbose_name_plural = "Access log"
        indexes = [
            models.Index(fields=["actor", "occurred_at"], name="audit_log_actor_idx"),
            models.Index(fields=["subject", "occurred_at"], name="audit_log_subject_idx"),
            models.Index(fields=["data_class", "occurred_at"], name="audit_log_class_idx"),
            models.Index(fields=["action", "occurred_at"], name="audit_log_action_idx"),
            models.Index(fields=["route", "occurred_at"], name="audit_log_route_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.actor_label or '-'} {self.action} {self.route or self.path} @ {self.occurred_at:%Y-%m-%d %H:%M}"

    # -- immutability -------------------------------------------------------

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("AccessLog rows are append-only; they cannot be updated.")
        bad = sorted(k for k in (self.detail or {}) if k.lower() in FORBIDDEN_DETAIL_KEYS)
        if bad:
            raise ValueError(f"AccessLog.detail must not carry money or secrets: {bad}")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("AccessLog rows cannot be deleted one by one; prune_access_log is the only remover.")
