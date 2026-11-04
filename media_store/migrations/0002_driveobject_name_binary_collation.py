"""Make ``DriveObject.name`` compare byte-for-byte on MySQL.

The column inherited the database default ``utf8mb4_0900_ai_ci``, which is
case- *and* accent-insensitive. The filesystem the names come from is neither,
so two real files differing only in case (``Photo.jpg`` / ``photo.jpg``) collided
on the unique index during ``migrate_media_to_drive`` (2026-10-16, production),
and the index lookups behind ``exists()``/``_open()`` could return the other
file. ``utf8mb4_0900_bin`` matches the filesystem exactly (and is NO PAD, so a
trailing space is significant too).

MySQL only: SQLite (tests) already compares ``=`` as BINARY. Not expressed as
``db_collation`` on the field because that name means nothing to SQLite.
"""

from django.db import migrations


def to_binary(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    schema_editor.execute(
        "ALTER TABLE media_store_driveobject "
        "MODIFY name varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin NOT NULL"
    )


def to_default(apps, schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return
    schema_editor.execute(
        "ALTER TABLE media_store_driveobject "
        "MODIFY name varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL"
    )


class Migration(migrations.Migration):

    atomic = False      # DDL; same reason as the collation CONVERT migrations

    dependencies = [
        ("media_store", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(to_binary, to_default),
    ]
