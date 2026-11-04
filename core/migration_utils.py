"""Helpers for the one-way encryption migrations.

docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.5: an ``AlterField`` from
``DECIMAL``/``VARCHAR``/``JSON`` to ``TEXT`` would *cast* existing values,
leaving plaintext in a column the application now expects to hold tokens.
There is no real money in any Orbis database yet, so instead of a backfill
the conversion deletes the converted tables' rows first — guarded so that
nobody can ever repeat it against real data by accident.
"""

from __future__ import annotations

import os

PURGE_FLAG = "PAYROLL_PURGE_ON_MIGRATE"


def guarded_purge(*model_labels: str):
    """Return a ``RunPython`` callable that empties ``model_labels`` (in the
    given order — children before parents when a FK is PROTECT).

    Refuses with a message naming the non-empty tables unless the
    ``PAYROLL_PURGE_ON_MIGRATE=1`` environment variable is set for that one
    ``migrate`` invocation. The flag must never be written into ``.env``.
    """

    def purge(apps, schema_editor):
        models = [apps.get_model(label) for label in model_labels]
        counts = {m._meta.db_table: m._default_manager.count() for m in models}
        non_empty = {table: n for table, n in counts.items() if n}
        if not non_empty:
            return
        if os.environ.get(PURGE_FLAG, "").strip() != "1":
            listing = ", ".join(f"{table} ({n} rows)" for table, n in non_empty.items())
            raise RuntimeError(
                "Refusing to convert money columns to encrypted storage while these tables "
                f"hold data: {listing}. This migration DELETES those rows (there is no backfill "
                "— see docs/PAYROLL_DATA_PROTECTION_PLAN.md §4.5). If that is really intended, "
                f"re-run with the environment variable {PURGE_FLAG}=1 for this one command."
            )
        for model in models:
            deleted, _ = model._default_manager.all().delete()
            print(f"  purged {model._meta.db_table}: {deleted} row(s)")

    purge.__name__ = "purge_" + "_".join(label.split(".")[-1].lower() for label in model_labels)
    return purge
