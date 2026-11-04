"""Write API for external systems — Orbis is authoritative, the caller is a client.

Auth: `Authorization: Token <EXTERNAL_API_TOKEN>` — see `ExternalTokenAuthentication`
below. It resolves to the shared external-integrations service account
(created via `manage.py create_external_service_account`), which only ever
proves "this is a legitimate integration calling us." It is NOT who gets
attributed on `Shift.added_by`/`updated_by` — every request must also supply
`actor_employee_id` (the real person performing the action), which is
resolved to a `CustomUser` and used for attribution instead. See
`ExternalShiftBatchAPI`'s docstring for the exact contract.

Per-app tokens (2026-10-14, docs/DIRECTORY_API_PLAN.md §4.4): dsndd now has
its own `accounts.ApiClient` with the `roster:write` scope, linked to this
same service account, so `ApiClientTokenAuthentication` hands the views the
identical `request.user`. `EXTERNAL_API_TOKEN` stays accepted until dsndd has
switched over, then it and `ExternalTokenAuthentication` are removed.

Every write here also feeds `roster.ShiftChangeLog`, an append-only audit
trail populated by signals in `roster/signals.py` (not by this module) —
see that file for the before/after diffing mechanism.

See /home/smrashel/.claude/plans/bubbly-finding-river.md for the full design
(Section A, written with dsndd as the motivating consumer). Phase 1 only:
this module has no dsndd-side counterpart yet.
"""

from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from rest_framework import authentication, exceptions
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.api.auth import ApiClientTokenAuthentication, HasApiScopeOrEnvToken
from accounts.locations import LOCATION_DROPDOWN_VALUES, resolve_location_choice
from accounts.models import ApiClient, CustomUser
from roster import night_window
from roster.models import Shift, ShiftAttribute, ShiftChangeLog, ShiftType
from roster.services import shift_is_after_last_working_date, shift_is_before_joining_date

MAX_BATCH_SIZE = 2000
SERVICE_ACCOUNT_EMAIL = "external-integrations@sebpo.internal"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class ExternalTokenAuthentication(authentication.BaseAuthentication):
    """Match `Authorization: Token <EXTERNAL_API_TOKEN>` against settings.

    Returns a real, persisted `CustomUser` (the shared external-integrations
    service account) since this endpoint writes `added_by`/`updated_by`
    FKs — unlike TimeLy's read-only synthetic `_ServiceAccount()`.

    Returns `None` (rather than raising) on an unconfigured or mismatched
    token, same rationale as `TimelyTokenAuthentication` — the two share the
    "Token" keyword on the shared read endpoints, so a non-match here must
    let DRF fall through to try Timely's authenticator rather than failing
    the request outright. Once the token itself is confirmed valid, a
    downstream problem (missing service account) still raises — that's a
    genuine misconfiguration on our side, not "try the next authenticator."
    """

    keyword = "Token"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Invalid token header.")
        try:
            token = header[1].decode()
        except UnicodeError as exc:
            raise exceptions.AuthenticationFailed("Malformed token.") from exc

        expected = getattr(settings, "EXTERNAL_API_TOKEN", "")
        if not expected or not constant_time_compare(token, expected):
            return None

        try:
            service_account = CustomUser.objects.get(
                email__iexact=SERVICE_ACCOUNT_EMAIL,
            )
        except CustomUser.DoesNotExist:
            raise exceptions.AuthenticationFailed(
                "External service account not provisioned — "
                "run manage.py create_external_service_account."
            )
        return (service_account, token)

    def authenticate_header(self, request):
        return "Token"


# ---------------------------------------------------------------------------
# Batch shift write endpoint
# ---------------------------------------------------------------------------

def _parse_item_date(value) -> tuple:
    """Return (date_or_None, error_or_None) — never raises."""
    if not isinstance(value, str):
        return None, "date is required, format YYYY-MM-DD"
    try:
        return datetime.strptime(value, "%Y-%m-%d").date(), None
    except ValueError:
        return None, "Bad date format, expected YYYY-MM-DD"


class ExternalShiftBatchAPI(APIView):
    """``POST /api/v1/external/shifts/batch/``.

    Body: ``{"actor_employee_id", "items": [{"client_ref", "employee_id",
    "date", "orbis_shift_type_id", "orbis_shift_attribute_id", "note"}, ...]}``.
    ``actor_employee_id`` is the real person performing the action (e.g. the
    dsndd supervisor who edited the cell) — distinct from each item's
    ``employee_id`` (whose shift it is). It's top-level, not per-item,
    because one request already represents one person's action (a single
    cell edit, or one bulk import/publish run attributed to whoever
    triggered it). Required; must resolve to a real `CustomUser` via
    `employee_id` — if missing or unresolvable, the **whole request** fails
    with 400 (you can't attribute any part of a batch to an unknown
    person). The resolved actor becomes `Shift.added_by`/`updated_by` on
    every write in the batch, replacing the shared service account that
    authenticates the request — token auth answers "is this a legitimate
    integration calling us," `actor_employee_id` answers "who, within that
    integration, did this."

    ``orbis_shift_type_id`` null (or omitted) deletes the cell.
    ``orbis_shift_attribute_id`` is optional — pins the shift to a specific
    time-variant under the shift type (e.g. "EVE 1-10" vs "EVE 3-11") instead
    of falling back to the type's own default start/end time; when omitted,
    `Shift.display_times` uses the shift type's default times. If given, it
    must belong to the given shift type and `orbis_shift_type_id` must also
    be given (an attribute implies its type).

    ``location`` is optional — one of ``"MHK"``/``"MBD"``/``"RAOWA"``/``"WFH"``,
    the same short codes the read endpoints already return as
    `office_location`/`work_location` (see `roster/api/timely.py`). Resolved
    via `accounts.locations.resolve_location_choice`, the same helper the
    native roster grid and `roster/upload.py` use. **Every write fully
    replaces the cell's location, not just when given** — if omitted, both
    `job_location`/`work_location` are set blank (which means "inherit from
    the employee's profile", per `Shift`'s own field semantics), even on an
    update that previously had a location set. An unrecognised code is a
    per-item error, not silently dropped.

    Always returns 200 with a per-item status unless the request itself is
    structurally broken (not JSON, no "items" list, over the batch cap, or a
    bad `actor_employee_id`) — a bad individual row never fails the whole
    batch. Mirrors the partial-success shape of `roster/upload.py`.

    Writes are bulked (`bulk_create(update_conflicts=True)` for
    creates/updates, one OR-chained delete for removals) rather than one
    query per item — see `_apply()`. Not a realistic scenario, but: sending
    the same (employee_id, date) twice in one request with different values
    still leaves the persisted `Shift` correct (last one in the list wins,
    same as a sequential per-item loop would), but both items report the
    same action/reason from the pre-batch snapshot, and the audit log's
    "previous" value for the second occurrence won't reflect the first
    occurrence's write. Not worth handling — a legitimate caller has no
    reason to list the same cell twice in one call.
    """

    authentication_classes = [ApiClientTokenAuthentication, ExternalTokenAuthentication]
    permission_classes = [HasApiScopeOrEnvToken]
    required_scope = ApiClient.Scope.ROSTER_WRITE

    def post(self, request):
        body = request.data
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            return Response(
                {"detail": "Body must be an object with an 'items' list."},
                status=400,
            )
        items = body["items"]
        if not items:
            return Response(
                {"detail": "'items' must not be empty."},
                status=400,
            )
        if len(items) > MAX_BATCH_SIZE:
            return Response(
                {"detail": f"Batch too large — max {MAX_BATCH_SIZE} items per request."},
                status=400,
            )

        actor_employee_id = body.get("actor_employee_id")
        if not actor_employee_id or not isinstance(actor_employee_id, str):
            return Response(
                {"detail": "'actor_employee_id' is required — the real person performing this action."},
                status=400,
            )
        actor = CustomUser.objects.filter(employee_id=actor_employee_id).first()
        if actor is None:
            return Response(
                {"detail": f"Unknown actor_employee_id '{actor_employee_id}'."},
                status=400,
            )

        results = self._apply(items, actor)
        summary = {"ok": 0, "conflict": 0, "error": 0}
        for r in results:
            summary[r["status"]] += 1
        return Response({"results": results, "summary": summary}, status=200)

    @transaction.atomic
    def _apply(self, items: list[dict], actor: CustomUser) -> list[dict]:
        # ---- parse + collect referenced ids, without touching the DB yet ----
        parsed = []  # list of dicts: client_ref, employee_id, date, shift_type_id, attribute_id, note, error
        employee_ids = set()
        shift_type_ids = set()
        shift_attribute_ids = set()
        for idx, raw in enumerate(items):
            client_ref = raw.get("client_ref") if isinstance(raw, dict) else None
            if client_ref is None:
                client_ref = f"item-{idx}"
            if not isinstance(raw, dict):
                parsed.append({"client_ref": client_ref, "error": "Item must be an object."})
                continue

            employee_id = raw.get("employee_id")
            if not employee_id or not isinstance(employee_id, str):
                parsed.append({"client_ref": client_ref, "error": "employee_id is required."})
                continue

            day, date_error = _parse_item_date(raw.get("date"))
            if date_error:
                parsed.append({"client_ref": client_ref, "error": date_error})
                continue

            shift_type_id = raw.get("orbis_shift_type_id")
            if shift_type_id is not None and not isinstance(shift_type_id, int):
                parsed.append({"client_ref": client_ref, "error": "orbis_shift_type_id must be an integer or null."})
                continue

            attribute_id = raw.get("orbis_shift_attribute_id")
            if attribute_id is not None and not isinstance(attribute_id, int):
                parsed.append({"client_ref": client_ref, "error": "orbis_shift_attribute_id must be an integer or null."})
                continue
            if attribute_id is not None and shift_type_id is None:
                parsed.append({"client_ref": client_ref, "error": "orbis_shift_attribute_id requires orbis_shift_type_id."})
                continue

            note = raw.get("note") or ""
            if not isinstance(note, str):
                parsed.append({"client_ref": client_ref, "error": "note must be a string."})
                continue

            location = raw.get("location") or ""
            if not isinstance(location, str):
                parsed.append({"client_ref": client_ref, "error": "location must be a string."})
                continue
            if location and location not in LOCATION_DROPDOWN_VALUES:
                parsed.append({
                    "client_ref": client_ref,
                    "error": f"Unknown location '{location}' — must be one of {', '.join(sorted(LOCATION_DROPDOWN_VALUES))}.",
                })
                continue

            employee_ids.add(employee_id)
            if shift_type_id is not None:
                shift_type_ids.add(shift_type_id)
            if attribute_id is not None:
                shift_attribute_ids.add(attribute_id)
            parsed.append({
                "client_ref": client_ref,
                "employee_id": employee_id,
                "date": day,
                "shift_type_id": shift_type_id,
                "attribute_id": attribute_id,
                "note": note,
                "location": location,
            })

        # ---- one pre-fetch each, no per-item queries ----
        employees_by_id = {
            e.employee_id: e
            for e in CustomUser.objects.filter(employee_id__in=employee_ids)
        }
        shift_types_by_id = {
            st.id: st
            for st in ShiftType.objects.filter(id__in=shift_type_ids, is_active=True)
        }
        shift_attributes_by_id = {
            sa.id: sa
            for sa in ShiftAttribute.objects.filter(id__in=shift_attribute_ids, is_active=True)
        }
        emp_pks = [e.pk for e in employees_by_id.values()]
        dates = {p["date"] for p in parsed if p.get("date")}
        # Previous field values, keyed by (employee_pk, date) — drives the
        # leave-lock check AND the ShiftChangeLog before/after diff below,
        # in one query instead of a query per item.
        previous_by_key = {
            (row["employee_id"], row["date"]): row
            for row in Shift.objects.filter(employee_id__in=emp_pks, date__in=dates).values(
                "employee_id", "date", "shift_type_id", "shift_attribute_id",
                "job_location_id", "work_location_id", "note", "leave_application_id",
            )
        }

        # Dedicated night shift windows (roster/night_window.py) — one query.
        night_windows = (
            night_window.windows_by_employee(emp_pks, min(dates), max(dates)) if dates else {}
        )

        # ---- classify: resolve + validate each item (no DB writes yet) ----
        now = timezone.now()
        results = []
        to_delete_keys = []
        objs: list[Shift] = []
        placeholder_refs = []  # (result_dict, key) — shift_id filled in after bulk_create
        log_intents = []
        upsert_emp_pks = set()
        upsert_dates = set()
        _WATCHED = ("shift_type_id", "shift_attribute_id", "job_location_id", "work_location_id", "note")

        for p in parsed:
            client_ref = p["client_ref"]
            if p.get("error"):
                results.append({"client_ref": client_ref, "status": "error", "reason": p["error"]})
                continue

            employee = employees_by_id.get(p["employee_id"])
            if employee is None:
                results.append({
                    "client_ref": client_ref, "status": "error",
                    "reason": f"Unknown employee_id '{p['employee_id']}'",
                })
                continue

            shift_type = None
            if p["shift_type_id"] is not None:
                shift_type = shift_types_by_id.get(p["shift_type_id"])
                if shift_type is None:
                    results.append({
                        "client_ref": client_ref, "status": "error",
                        "reason": f"Unknown or inactive orbis_shift_type_id '{p['shift_type_id']}'",
                    })
                    continue

            shift_attribute = None
            if p["attribute_id"] is not None:
                shift_attribute = shift_attributes_by_id.get(p["attribute_id"])
                if shift_attribute is None:
                    results.append({
                        "client_ref": client_ref, "status": "error",
                        "reason": f"Unknown or inactive orbis_shift_attribute_id '{p['attribute_id']}'",
                    })
                    continue
                if shift_attribute.shift_type_id != shift_type.id:
                    results.append({
                        "client_ref": client_ref, "status": "error",
                        "reason": (
                            f"orbis_shift_attribute_id '{p['attribute_id']}' does not belong to "
                            f"orbis_shift_type_id '{p['shift_type_id']}'"
                        ),
                    })
                    continue

            key = (employee.pk, p["date"])
            previous = previous_by_key.get(key)
            if previous is not None and previous["leave_application_id"] is not None:
                results.append({
                    "client_ref": client_ref, "status": "conflict", "action": "skipped",
                    "reason": "leave_application_owned",
                })
                continue

            if shift_type is None:
                if previous is None:
                    results.append({"client_ref": client_ref, "status": "ok", "action": "noop"})
                else:
                    to_delete_keys.append(key)
                    results.append({"client_ref": client_ref, "status": "ok", "action": "deleted"})
                continue

            # No Shift may exist past an employee's last working date — same
            # cutoff the week-grid prefill, the bulk uploader and the leave
            # materialiser enforce (see roster.services). Checked after the
            # delete branch above on purpose: removing a post-LWD row is
            # always allowed, only creating/updating one is not. Free — the
            # employee is already in memory from the pre-fetch.
            if shift_is_after_last_working_date(employee, p["date"]):
                results.append({
                    "client_ref": client_ref, "status": "conflict", "action": "skipped",
                    "reason": "past_last_working_date",
                })
                continue

            # Mirror image of the LWD guard above — no Shift may exist before
            # an employee's joining date.
            if shift_is_before_joining_date(employee, p["date"]):
                results.append({
                    "client_ref": client_ref, "status": "conflict", "action": "skipped",
                    "reason": "before_joining_date",
                })
                continue

            # Inside an APPROVED dedicated night shift window only NIG/CUST or
            # a non-working type may be written, unless the acting user is the
            # department head — a machine client never is, so it gets a
            # per-item conflict and the rest of the batch still applies.
            if not night_window.may_set_shift(
                actor, employee, p["date"], shift_type, windows=night_windows,
            ):
                results.append({
                    "client_ref": client_ref, "status": "conflict", "action": "skipped",
                    "reason": "dedicated_night_shift_window",
                })
                continue

            job_location, work_location = resolve_location_choice(p["location"])
            objs.append(Shift(
                employee_id=employee.pk, date=p["date"],
                shift_type=shift_type, shift_attribute=shift_attribute,
                job_location=job_location, work_location=work_location,
                note=p["note"],
                # added_* are only written on INSERT (not in update_fields)
                # so they stay untouched when an existing row is overwritten.
                added_by=actor, added_at=now,
                updated_by=actor, updated_at=now,
            ))
            upsert_emp_pks.add(employee.pk)
            upsert_dates.add(p["date"])
            result = {
                "client_ref": client_ref, "status": "ok",
                "action": "updated" if previous is not None else "created",
                "shift_id": None,
            }
            results.append(result)
            placeholder_refs.append((result, key))

            new_values = {
                "shift_type_id": shift_type.id,
                "shift_attribute_id": shift_attribute.id if shift_attribute else None,
                "job_location_id": job_location.id if job_location else None,
                "work_location_id": work_location.id if work_location else None,
                "note": p["note"],
            }
            if previous is not None and all(previous[f] == new_values[f] for f in _WATCHED):
                pass  # no-op overwrite — nothing actually changed, skip logging.
            else:
                log_intents.append({"key": key, "previous": previous, "new": new_values})

        # ---- bulk-write phase: each bucket written once, regardless of size ----
        if to_delete_keys:
            q = Q()
            for emp_pk, d in to_delete_keys:
                q |= Q(employee_id=emp_pk, date=d)
            # Stamp updated_by=actor before deleting — pre_delete reads
            # whatever's currently persisted on the row, so without this a
            # delete would attribute itself to whoever last saved it, not
            # whoever's deleting it now.
            qs = Shift.objects.filter(q)
            qs.update(updated_by=actor, updated_at=now)
            qs.delete()  # queryset.delete() still fires pre_delete per row — audit log needs no manual handling here.

        if objs:
            # `unique_fields` is required by SQLite/PostgreSQL but rejected by
            # MySQL (MySQL's ON DUPLICATE KEY UPDATE matches on any unique
            # constraint automatically) — same feature-flag dance as
            # roster/upload.py.
            bulk_kwargs: dict = {
                "update_conflicts": True,
                "update_fields": [
                    "shift_type", "shift_attribute",
                    "job_location", "work_location", "note",
                    "updated_by", "updated_at",
                ],
            }
            if connection.features.supports_update_conflicts_with_target:
                bulk_kwargs["unique_fields"] = ["employee", "date"]
            Shift.objects.bulk_create(objs, **bulk_kwargs)

            # bulk_create doesn't populate pks back onto `objs` for an
            # update_conflicts upsert (ambiguous insert-vs-update), so
            # re-fetch the (employee, date) → pk mapping in one query.
            shift_pks = {
                (eid, d): pk for eid, d, pk in
                Shift.objects.filter(employee_id__in=upsert_emp_pks, date__in=upsert_dates)
                .values_list("employee_id", "date", "id")
            }
            for result, key in placeholder_refs:
                result["shift_id"] = shift_pks.get(key)

            if log_intents:
                # bulk_create bypasses signals entirely, so ShiftChangeLog
                # rows are built by hand here, same technique as
                # roster/upload.py::apply_upload.
                log_objs = []
                for intent in log_intents:
                    key = intent["key"]
                    previous, new = intent["previous"], intent["new"]
                    log_objs.append(ShiftChangeLog(
                        shift_id=shift_pks.get(key),
                        employee_id=key[0], date=key[1],
                        action_type=(
                            ShiftChangeLog.ActionType.UPDATED if previous is not None
                            else ShiftChangeLog.ActionType.CREATED
                        ),
                        previous_shift_type_id=previous["shift_type_id"] if previous else None,
                        previous_shift_attribute_id=previous["shift_attribute_id"] if previous else None,
                        previous_job_location_id=previous["job_location_id"] if previous else None,
                        previous_work_location_id=previous["work_location_id"] if previous else None,
                        previous_note=previous["note"] if previous else "",
                        new_shift_type_id=new["shift_type_id"],
                        new_shift_attribute_id=new["shift_attribute_id"],
                        new_job_location_id=new["job_location_id"],
                        new_work_location_id=new["work_location_id"],
                        new_note=new["note"],
                        changed_by=actor, changed_at=now,
                    ))
                ShiftChangeLog.objects.bulk_create(log_objs)

        return results
