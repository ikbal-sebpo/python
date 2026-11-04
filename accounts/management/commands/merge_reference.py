"""Merge a duplicate org-reference row (Department / JobLocation / Designation)
into the real one, then delete the duplicate.

Why this exists (2026-10-10). The SSO backend's one-time Graph enrichment
(`accounts/auth_backends.py::_provision_from_claims`) used to
``get_or_create`` Department, Designation and JobLocation rows straight
from Microsoft Entra's free-text attributes, so an intern whose Entra
profile read "Human Resource" (singular) created a second HR department on
first login, and the raw ``officeLocation`` string "Monem Business
District, Level-7 Dhaka 1205 BD" created a second MBD row. Nobody in Orbis
added either. The backend still auto-creates by design (an admin merges
duplicates afterwards -- this is that merge), so the duplicates it has made
(on prod, and on any dump restored from it) are folded back into the real
rows here, and a hand-typed duplicate in the admin gets the same treatment.

What a merge does, in one transaction:

1. Re-points every FK and M2M onto the duplicate at the target. The
   references are discovered by reflection over every installed model, so a
   relation hidden with ``related_name="+"`` (``employees.TeamAssignment``'s
   PROTECT FK is one) can't be missed -- the Django admin's delete would
   bounce off exactly that row.
2. Collapses the fake "transfer" the duplicate leaves behind in the
   effective-dated history (``TeamAssignment`` for a department,
   ``DesignationHistory`` for a designation): once the duplicate becomes the
   target, an employee who was "moved" from "Human Resource" to "Human
   Resources" has two contiguous rows that now say the same thing, and they
   become one row again, keeping the earlier row's start date and reason.
3. Refuses -- before writing anything -- when retargeting would violate a
   unique constraint (e.g. the same Team name under both departments).
   Resolve those by hand first; this command never guesses which to keep.
4. Deletes the duplicate (or deactivates it with ``--keep-source``). The
   target row is otherwise untouched: its own fields win. The only copy is
   the duplicate's ``description`` onto a target whose description is blank.

Writes go through queryset ``update()`` on purpose: the ``CustomUser``
``post_save`` signals that maintain ``TeamAssignment`` /
``DesignationHistory`` must *not* fire here, because the history is
retargeted and collapsed explicitly in step 2 rather than re-derived.
Historical label snapshots (``payroll.PayrollRunEmployee.department_name``,
``audit.AccessLog.subject_label``) are deliberately left as they were
written -- they record what the screen said at the time.

Dry-run by default -- pass ``--apply`` to write.

Usage::

    manage.py merge_reference department --from "Human Resource" --into "Human Resources"
    manage.py merge_reference department --from "Human Resource" --into "Human Resources" --apply
    manage.py merge_reference location --from "Monem Business District, Level-7 Dhaka 1205 BD" --into MBD --apply
    manage.py merge_reference designation --from "Sr. Executive" --into "Senior Executive" --apply
    manage.py merge_reference department --from id:25 --into id:14 --apply

``--from`` / ``--into`` take the exact name (case-insensitive) or ``id:<pk>``.

Related one-time tools: ``consolidate_job_locations`` buckets *every*
JobLocation onto MHK/MBD/RAOWA by address pattern; this command merges one
named pair of any kind and is the one to reach for from now on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Model, ProtectedError, Q, UniqueConstraint

from accounts.models import Department, Designation, JobLocation, directory_stamp

logger = logging.getLogger(__name__)

KINDS = {
    "department": Department,
    "location": JobLocation,
    "designation": Designation,
}


def _history_spec(model):
    """The effective-dated history model whose rows must be collapsed after
    a merge of ``model``, as ``(history_model, fk_name, identity_fields)``.
    Imported lazily: accounts must not import employees at module load."""
    from employees.models import DesignationHistory, TeamAssignment

    return {
        Department: (TeamAssignment, "department", ("department_id", "team_id", "resource_status")),
        Designation: (DesignationHistory, "designation", ("designation_id",)),
    }.get(model)


@dataclass
class _FkRef:
    model: type[Model]
    field_name: str
    count: int = 0
    collisions: list = field(default_factory=list)


@dataclass
class _M2MRef:
    model: type[Model]
    field_name: str
    through: type[Model]
    owner_fk: str      # through-table column naming the owning row
    target_fk: str     # through-table column naming our reference row
    count: int = 0
    duplicates: int = 0  # owner already linked to the target -> row dropped, not moved


def discover_references(model):
    """Every concrete FK and auto-through M2M onto ``model``, across all apps.

    Relations with ``related_name="+"`` have no reverse accessor and so are
    absent from ``model._meta.related_objects`` -- which is why this walks
    the *forward* fields of every model instead. An M2M with an explicit
    ``through`` model is skipped here because its through table shows up as
    an ordinary FK in the same walk.
    """
    fks, m2ms = [], []
    for other in apps.get_models():
        for f in other._meta.get_fields():
            if not getattr(f, "concrete", False) or f.related_model is not model:
                continue
            if f.many_to_many:
                through = f.remote_field.through
                if not through._meta.auto_created:
                    continue
                m2ms.append(_M2MRef(
                    model=other, field_name=f.name, through=through,
                    owner_fk=f.m2m_field_name(), target_fk=f.m2m_reverse_field_name(),
                ))
            elif f.many_to_one or f.one_to_one:
                fks.append(_FkRef(model=other, field_name=f.name))
    return fks, m2ms


def _unique_groups(model, field_name):
    """Field-name tuples of every unconditional unique constraint on ``model``
    that includes ``field_name`` (``unique_together`` + ``UniqueConstraint``)."""
    groups = [tuple(g) for g in model._meta.unique_together if field_name in g]
    for c in model._meta.constraints:
        if isinstance(c, UniqueConstraint) and c.condition is None and c.fields and field_name in c.fields:
            groups.append(tuple(c.fields))
    return groups


def _fk_collisions(ref: _FkRef, source, target):
    """Rows on ``source`` whose retarget would duplicate an existing row on
    ``target`` under one of the model's unique constraints."""
    found = []
    for group in _unique_groups(ref.model, ref.field_name):
        others = [g for g in group if g != ref.field_name]
        for row in ref.model._base_manager.filter(**{f"{ref.field_name}_id": source.pk}):
            lookup = {f"{ref.field_name}_id": target.pk}
            lookup.update({g: getattr(row, ref.model._meta.get_field(g).attname) for g in others})
            if ref.model._base_manager.filter(**lookup).exists():
                found.append((row, group))
    return found


def collapse_plan(rows, identity_fields):
    """Given one employee's history rows (any order), return ``(keep, drop)``
    pairs where ``drop`` directly continues ``keep`` with an identical
    identity -- i.e. a "transfer" that no longer changes anything.

    Pure, so the dry run can preview exactly what ``--apply`` will do.
    Contiguous means ``drop`` starts the day after ``keep`` ends, or earlier
    (an overlap the signal's same-day in-place update can leave behind).
    """
    ordered = sorted(rows, key=lambda r: (r.effective_from, r.pk))
    plan = []
    keep = None
    for row in ordered:
        if keep is not None and keep.effective_to is not None:
            same = all(getattr(keep, f) == getattr(row, f) for f in identity_fields)
            if same and row.effective_from <= keep.effective_to + timedelta(days=1):
                plan.append((keep, row))
                # The surviving row now extends to the dropped row's end, so a
                # third contiguous row still compares against the right span.
                keep.effective_to = row.effective_to
                continue
        keep = row
    return plan


class Command(BaseCommand):
    help = "Merge a duplicate Department / JobLocation / Designation into the real row. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("kind", choices=sorted(KINDS), help="Which reference table.")
        parser.add_argument("--from", dest="source", required=True, help='Duplicate row: exact name or "id:<pk>".')
        parser.add_argument("--into", dest="target", required=True, help='Row to keep: exact name or "id:<pk>".')
        parser.add_argument("--apply", action="store_true", help="Write to the database (default: dry-run).")
        parser.add_argument(
            "--keep-source", action="store_true",
            help="Deactivate the duplicate instead of deleting it once nothing references it.",
        )

    # -- resolution -------------------------------------------------------

    def _resolve(self, model, spec, role):
        label = model.__name__
        if spec.lower().startswith("id:"):
            try:
                pk = int(spec[3:])
            except ValueError:
                raise CommandError(f"--{role}: {spec!r} is not a valid id:<pk>.")
            row = model._base_manager.filter(pk=pk).first()
            if row is None:
                raise CommandError(f"--{role}: no {label} with id={pk}.")
            return row
        matches = list(model._base_manager.filter(name__iexact=spec.strip()))
        if not matches:
            raise CommandError(f"--{role}: no {label} named {spec!r}. Use id:<pk> if the name is ambiguous.")
        if len(matches) > 1:
            ids = ", ".join(f"id={m.pk} {m.name!r}" for m in matches)
            raise CommandError(f"--{role}: {spec!r} matches more than one {label} ({ids}); use id:<pk>.")
        return matches[0]

    # -- main -------------------------------------------------------------

    def handle(self, *args, **options):
        model = KINDS[options["kind"]]
        source = self._resolve(model, options["source"], "from")
        target = self._resolve(model, options["target"], "into")
        if source.pk == target.pk:
            raise CommandError("--from and --into resolve to the same row; nothing to merge.")

        label = model.__name__
        self.stdout.write(f"Merge {label} id={source.pk} {source.name!r}  ->  id={target.pk} {target.name!r}")

        fks, m2ms = discover_references(model)
        for ref in fks:
            ref.count = ref.model._base_manager.filter(**{f"{ref.field_name}_id": source.pk}).count()
            if ref.count:
                ref.collisions = _fk_collisions(ref, source, target)
        for ref in m2ms:
            links = ref.through._base_manager.filter(**{f"{ref.target_fk}_id": source.pk})
            ref.count = links.count()
            if ref.count:
                owners_on_target = set(
                    ref.through._base_manager.filter(**{f"{ref.target_fk}_id": target.pk})
                    .values_list(f"{ref.owner_fk}_id", flat=True)
                )
                ref.duplicates = sum(
                    1 for oid in links.values_list(f"{ref.owner_fk}_id", flat=True) if oid in owners_on_target
                )

        self.stdout.write("\nReferences to re-point:")
        touched = [r for r in fks if r.count] + [r for r in m2ms if r.count]
        if not touched:
            self.stdout.write("  (none)")
        for ref in fks:
            if ref.count:
                self.stdout.write(f"  {ref.model._meta.label}.{ref.field_name}: {ref.count} row(s)")
        for ref in m2ms:
            if ref.count:
                extra = f" ({ref.duplicates} already linked to the target, dropped)" if ref.duplicates else ""
                self.stdout.write(f"  {ref.model._meta.label}.{ref.field_name} (M2M): {ref.count} link(s){extra}")

        collisions = [(ref, row, group) for ref in fks for row, group in ref.collisions]
        if collisions:
            self.stdout.write(self.style.ERROR("\nUnique-constraint collisions -- resolve these by hand first:"))
            for ref, row, group in collisions:
                self.stdout.write(f"  {ref.model._meta.label} id={row.pk} {row!s} would duplicate an existing "
                                  f"row on the target under unique({', '.join(group)})")
            raise CommandError("Merge refused: retargeting would violate a unique constraint.")

        # History collapse preview -- simulate the retarget in memory.
        history = _history_spec(model)
        collapse = []
        if history is not None:
            hist_model, fk_name, identity = history
            attname = f"{fk_name}_id"
            employee_ids = set(
                hist_model._base_manager.filter(**{attname: source.pk}).values_list("employee_id", flat=True)
            )
            for emp_id in employee_ids:
                rows = list(hist_model._base_manager.filter(employee_id=emp_id))
                for r in rows:
                    if getattr(r, attname) == source.pk:
                        setattr(r, attname, target.pk)
                collapse.extend(collapse_plan(rows, identity))
            self.stdout.write(f"\n{hist_model.__name__} rows that become a no-op transfer and will be folded away:")
            if not collapse:
                self.stdout.write("  (none)")
            for keep, drop in collapse:
                self.stdout.write(
                    f"  employee {keep.employee_id}: keep id={keep.pk} (from {keep.effective_from}, {keep.reason}), "
                    f"drop id={drop.pk} (from {drop.effective_from}, {drop.reason}); "
                    f"kept row now ends {drop.effective_to or 'open'}"
                )

        if not options["apply"]:
            self.stdout.write(self.style.WARNING(
                "\nDry-run only -- no database changes made. Re-run with --apply to write."
            ))
            return

        source_pk, source_name = source.pk, source.name  # delete() clears source.pk
        with transaction.atomic():
            for ref in fks:
                if ref.count:
                    ref.model._base_manager.filter(**{f"{ref.field_name}_id": source.pk}).update(
                        **{ref.field_name: target},
                        **directory_stamp(ref.field_name, model=ref.model),
                    )
            for ref in m2ms:
                if not ref.count:
                    continue
                owners_on_target = set(
                    ref.through._base_manager.filter(**{f"{ref.target_fk}_id": target.pk})
                    .values_list(f"{ref.owner_fk}_id", flat=True)
                )
                links = ref.through._base_manager.filter(**{f"{ref.target_fk}_id": source.pk})
                links.filter(**{f"{ref.owner_fk}_id__in": owners_on_target}).delete()
                links.update(**{f"{ref.target_fk}_id": target.pk})

            # Collapse using fresh rows (the in-memory preview objects were
            # mutated for the simulation); the plan is recomputed on real data.
            if history is not None:
                hist_model, fk_name, identity = history
                for emp_id in employee_ids:
                    rows = list(hist_model._base_manager.filter(employee_id=emp_id))
                    for keep, drop in collapse_plan(rows, identity):
                        keep_db = hist_model._base_manager.get(pk=keep.pk)
                        new_to = drop.effective_to
                        note = keep_db.note
                        if drop.note and drop.note not in note:
                            note = f"{note}\n{drop.note}".strip()
                        # Delete first so the one-open-row invariant never has
                        # two open rows, even momentarily.
                        hist_model._base_manager.filter(pk=drop.pk).delete()
                        keep_db.effective_to = new_to
                        keep_db.note = note
                        keep_db.save(update_fields=["effective_to", "note", "updated_at"])

            if not target.description and source.description:
                target.description = source.description
                target.save(update_fields=["description", "updated_at"])

            remaining = sum(
                ref.model._base_manager.filter(**{f"{ref.field_name}_id": source.pk}).count() for ref in fks
            ) + sum(
                ref.through._base_manager.filter(**{f"{ref.target_fk}_id": source.pk}).count() for ref in m2ms
            )
            if remaining:
                raise CommandError(f"{remaining} reference(s) to the duplicate still remain after retargeting; aborted.")

            if options["keep_source"]:
                source.is_active = False
                source.save(update_fields=["is_active", "updated_at"])
                outcome = f"deactivated duplicate id={source_pk}"
            else:
                try:
                    source.delete()
                except ProtectedError as exc:
                    raise CommandError(f"Could not delete the duplicate, something still protects it: {exc}")
                outcome = f"deleted duplicate id={source_pk}"

        logger.info(
            "merge_reference: %s %r (id=%s) merged into %r (id=%s); %d fk field(s), %d m2m field(s), "
            "%d history row(s) folded; %s",
            label, source_name, source_pk, target.name, target.pk,
            sum(1 for r in fks if r.count), sum(1 for r in m2ms if r.count), len(collapse), outcome,
        )
        self.stdout.write(self.style.SUCCESS(
            f"\nMerged {label} {source_name!r} into {target.name!r}; {len(collapse)} history row(s) folded; {outcome}."
        ))
