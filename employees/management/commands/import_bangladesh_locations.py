"""Import reviewed Bangladesh location taxonomy into employees models."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from employees.models import Address, City, Country, Division, State, UnionWard
from tools.fetch_bangladesh_locations import (
    comparison_name,
    is_paurashava_adm4,
    paurashava_display_name,
    pcode_digits,
    validate_payload_artifact,
)


class _DryRunRollback(Exception):
    pass


class Command(BaseCommand):
    help = (
        "Import Bangladesh District/City Corporation, Upazila/Thana/"
        "Paurashava, and Union/Ward taxonomy from "
        "data/research/bangladesh_locations.json."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--artifact",
            type=Path,
            default=Path("data/research/bangladesh_locations.json"),
            help="Path to the generated Bangladesh locations research artifact.",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help=(
                "Replace existing Bangladesh State, City, and Union/Ward rows. "
                "Without this flag, rows are upserted and unrelated existing "
                "rows are left in place."
            ),
        )
        parser.add_argument(
            "--force-address-detach",
            action="store_true",
            help=(
                "When --replace is used, clear existing Bangladesh address "
                "state/city/union_ward references before deleting taxonomy rows."
            ),
        )
        parser.add_argument(
            "--remap-addresses",
            action="store_true",
            help=(
                "When --replace is used, snapshot existing Bangladesh address "
                "State/City names and best-effort remap those addresses after "
                "the new taxonomy is imported."
            ),
        )
        parser.add_argument(
            "--remap-report",
            type=Path,
            help=(
                "Optional CSV path for address remap details. Useful with "
                "--remap-addresses --dry-run because the file is not rolled "
                "back with the transaction."
            ),
        )
        parser.add_argument(
            "--allow-blocked-artifact",
            action="store_true",
            help=(
                "Allow import even when seed_readiness.status is blocked. Use "
                "only for review databases, not production seed data."
            ),
        )
        parser.add_argument(
            "--skip-union-wards",
            action="store_true",
            help=(
                "Import only Bangladesh divisions, State rows, and City rows. "
                "Use this when the artifact's Union/Ward source still has "
                "readiness blockers."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run the import in a transaction and roll it back.",
        )

    def handle(self, *args, **options):
        if options["force_address_detach"] and options["remap_addresses"]:
            raise CommandError(
                "--force-address-detach and --remap-addresses are mutually exclusive."
            )

        artifact_path: Path = options["artifact"]
        payload = self._load_payload(artifact_path)
        self._validate_payload(
            payload,
            allow_blocked=options["allow_blocked_artifact"],
            skip_union_wards=options["skip_union_wards"],
        )

        try:
            with transaction.atomic():
                counts = self._import_payload(
                    payload,
                    replace=options["replace"],
                    force_address_detach=options["force_address_detach"],
                    remap_addresses=options["remap_addresses"],
                    remap_report=options["remap_report"],
                    skip_union_wards=options["skip_union_wards"],
                )
                self._print_counts(counts)
                if options["dry_run"]:
                    self.stdout.write(self.style.WARNING("[dry-run] rolling back"))
                    raise _DryRunRollback()
        except _DryRunRollback:
            return

        self.stdout.write(self.style.SUCCESS("Bangladesh location import complete."))

    def _load_payload(self, artifact_path: Path | str) -> dict[str, Any]:
        artifact_path = Path(artifact_path)
        if not artifact_path.exists():
            raise CommandError(
                f"Artifact not found: {artifact_path}. Generate it first with "
                "tools/fetch_bangladesh_locations.py."
            )
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"Artifact is not valid JSON: {artifact_path}") from exc
        if not isinstance(payload, dict):
            raise CommandError("Artifact JSON must contain an object at the top level.")
        return payload

    def _validate_payload(
        self,
        payload: dict[str, Any],
        *,
        allow_blocked: bool,
        skip_union_wards: bool,
    ) -> None:
        if skip_union_wards:
            self._validate_dghs_only(payload)
            return

        validation = validate_payload_artifact(payload)
        if validation["status"] != "valid":
            raise CommandError(
                "Artifact validation failed. Regenerate it with "
                "tools/fetch_bangladesh_locations.py before importing:\n"
                + "\n".join(f"- {error}" for error in validation["errors"])
            )

        seed_readiness = payload.get("seed_readiness", {})
        if seed_readiness.get("status") == "blocked" and not allow_blocked:
            blockers = seed_readiness.get("blockers", [])
            raise CommandError(
                "Artifact seed_readiness.status is blocked. Re-run with "
                "--allow-blocked-artifact only for a review database.\n"
                + "\n".join(f"- {blocker}" for blocker in blockers)
            )

    def _validate_dghs_only(self, payload: dict[str, Any]) -> None:
        dghs = payload.get("dghs_geocode_hierarchy")
        if not isinstance(dghs, dict):
            raise CommandError("Artifact is missing dghs_geocode_hierarchy.")
        for key in (
            "divisions",
            "districts",
            "city_corporations",
            "district_upazilas",
            "city_corporation_thanas",
        ):
            if key not in dghs:
                raise CommandError(f"Artifact is missing dghs_geocode_hierarchy.{key}.")
        counts = dghs.get("counts", {})
        if counts.get("divisions") != 8:
            raise CommandError("DGHS division count is not 8.")
        if counts.get("districts") != 64:
            raise CommandError("DGHS district count is not 64.")
        if counts.get("city_corporations", 0) < 12:
            raise CommandError("DGHS city corporation count is below 12.")

    def _import_payload(
        self,
        payload: dict[str, Any],
        *,
        replace: bool,
        force_address_detach: bool,
        remap_addresses: bool,
        remap_report: Path | None,
        skip_union_wards: bool,
    ) -> dict[str, int]:
        country = (
            Country.objects.filter(iso_code="BD").first()
            or Country.objects.filter(name="Bangladesh").first()
        )
        if country:
            country.name = "Bangladesh"
            country.iso_code = "BD"
            country.is_active = True
            country.save(update_fields=["name", "iso_code", "is_active"])
        else:
            country = Country.objects.create(
                name="Bangladesh",
                iso_code="BD",
                is_active=True,
            )

        address_snapshots: list[dict[str, Any]] = []
        if replace:
            if remap_addresses:
                address_snapshots = self._snapshot_address_links(country)
            self._replace_taxonomy(
                country,
                force_address_detach=force_address_detach or remap_addresses,
            )

        dghs = payload["dghs_geocode_hierarchy"]

        division_by_code = self._import_divisions(country, dghs["divisions"])
        state_by_code = self._import_states(country, division_by_code, dghs)
        city_by_code, paurashava_count = self._import_cities(
            state_by_code,
            dghs,
            payload.get("union_hierarchy"),
        )
        union_ward_count = 0
        if not skip_union_wards:
            review_hierarchy = payload["review_hierarchy"]
            union_ward_count = self._import_union_wards(
                state_by_code,
                city_by_code,
                review_hierarchy,
            )
        address_remap_counts: dict[str, int] = {}
        if address_snapshots:
            address_remap_counts, report_rows = self._remap_address_links(
                address_snapshots,
                state_by_code,
                city_by_code,
            )
            if remap_report:
                self._write_remap_report(remap_report, report_rows)
                address_remap_counts["address_remap_report_rows"] = len(report_rows)

        city_count = len({city.pk for city in city_by_code.values()})
        city_source_refs = len(city_by_code)
        counts = {
            "countries": 1,
            "divisions": len(division_by_code),
            "states": len(state_by_code),
            "cities": city_count,
            "paurashavas": paurashava_count,
            "union_wards": union_ward_count,
        }
        if city_source_refs != city_count:
            counts["city_source_refs"] = city_source_refs
        counts.update(address_remap_counts)
        return counts

    def _replace_taxonomy(
        self,
        country: Country,
        *,
        force_address_detach: bool,
    ) -> None:
        impacted_addresses = Address.objects.filter(country=country).filter(
            Q(state__isnull=False) | Q(city__isnull=False) | Q(union_ward__isnull=False)
        )
        impacted_count = impacted_addresses.count()
        if impacted_count and not force_address_detach:
            raise CommandError(
                f"{impacted_count} Bangladesh address rows reference existing "
                "taxonomy. Re-run with --force-address-detach only after backing "
                "up or deciding those links can be cleared."
            )
        if impacted_count:
            impacted_addresses.update(state=None, city=None, union_ward=None)

        State.objects.filter(country=country).delete()

    def _snapshot_address_links(self, country: Country) -> list[dict[str, Any]]:
        rows = (
            Address.objects.filter(country=country)
            .filter(
                Q(state__isnull=False)
                | Q(city__isnull=False)
                | Q(union_ward__isnull=False)
            )
            .select_related("state", "city", "union_ward")
            .select_related("employee")
        )
        return [
            {
                "address_id": row.pk,
                "employee_id": row.employee_id,
                "employee_email": getattr(row.employee, "email", ""),
                "employee_name": getattr(row.employee, "display_name", str(row.employee)),
                "address_type": row.get_address_type_display(),
                "state_name": row.state.name if row.state else "",
                "city_name": row.city.name if row.city else "",
                "union_ward_name": row.union_ward.name if row.union_ward else "",
            }
            for row in rows
        ]

    def _remap_address_links(
        self,
        snapshots: list[dict[str, Any]],
        state_by_code: dict[str, State],
        city_by_code: dict[str, City],
    ) -> tuple[dict[str, int], list[dict[str, Any]]]:
        states_by_name = {
            comparison_name(state.name): state
            for state in state_by_code.values()
        }
        cities_by_state_and_name: dict[tuple[str, str], list[City]] = {}
        cities_by_district_and_name: dict[tuple[str, str], list[City]] = {}

        for city in city_by_code.values():
            city_key = comparison_name(city.name)
            state_key = comparison_name(city.state.name)
            cities_by_state_and_name.setdefault((state_key, city_key), []).append(city)

            district_code = (
                pcode_digits(city.source_code)[:4] if city.source_code else ""
            )
            district_state = state_by_code.get(district_code)
            if district_state:
                district_key = comparison_name(district_state.name)
                cities_by_district_and_name.setdefault(
                    (district_key, city_key),
                    [],
                ).append(city)

        counts = {
            "address_snapshots": len(snapshots),
            "address_state_remapped": 0,
            "address_state_unresolved": 0,
            "address_city_remapped": 0,
            "address_city_blank": 0,
            "address_city_unresolved": 0,
            "address_city_ambiguous": 0,
        }
        report_rows: list[dict[str, Any]] = []

        for snapshot in snapshots:
            state_key = comparison_name(snapshot["state_name"])
            city_key = comparison_name(snapshot["city_name"])
            new_state = states_by_name.get(state_key)
            new_city = None
            status = "state_only"
            note = ""

            if city_key:
                candidates = cities_by_state_and_name.get((state_key, city_key), [])
                if len(candidates) != 1:
                    candidates = cities_by_district_and_name.get((state_key, city_key), [])
                if len(candidates) == 1:
                    new_city = candidates[0]
                    new_state = new_city.state
                    status = "city_remapped"
                elif len(candidates) > 1:
                    counts["address_city_ambiguous"] += 1
                    status = "city_ambiguous"
                    note = "Multiple new City rows matched this old state/city pair."
                else:
                    counts["address_city_unresolved"] += 1
                    status = "city_unresolved"
                    note = "No new City row matched this old state/city pair."
            else:
                counts["address_city_blank"] += 1
                status = "state_remapped_no_old_city" if new_state else "state_unresolved"

            Address.objects.filter(pk=snapshot["address_id"]).update(
                state=new_state,
                city=new_city,
                union_ward=None,
            )
            if new_state:
                counts["address_state_remapped"] += 1
            else:
                counts["address_state_unresolved"] += 1
            if new_city:
                counts["address_city_remapped"] += 1
            report_rows.append(
                {
                    "address_id": snapshot["address_id"],
                    "employee_id": snapshot["employee_id"],
                    "employee_email": snapshot["employee_email"],
                    "employee_name": snapshot["employee_name"],
                    "address_type": snapshot["address_type"],
                    "old_state": snapshot["state_name"],
                    "old_city": snapshot["city_name"],
                    "old_union_ward": snapshot["union_ward_name"],
                    "new_state": new_state.name if new_state else "",
                    "new_state_kind": new_state.get_kind_display() if new_state else "",
                    "new_city": new_city.name if new_city else "",
                    "new_city_kind": new_city.get_kind_display() if new_city else "",
                    "status": status,
                    "note": note,
                }
            )

        return counts, report_rows

    def _write_remap_report(
        self,
        path: Path | str,
        rows: list[dict[str, Any]],
    ) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "address_id",
            "employee_id",
            "employee_email",
            "employee_name",
            "address_type",
            "old_state",
            "old_city",
            "old_union_ward",
            "new_state",
            "new_state_kind",
            "new_city",
            "new_city_kind",
            "status",
            "note",
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        self.stdout.write(f"wrote {path}")

    def _import_divisions(
        self,
        country: Country,
        rows: list[dict[str, str]],
    ) -> dict[str, Division]:
        by_code: dict[str, Division] = {}
        for row in rows:
            code = row.get("code", "")
            name = row.get("name", "")
            if not code or not name:
                continue
            division, _ = Division.objects.update_or_create(
                country=country,
                name=name,
                defaults={"is_active": True},
            )
            by_code[code] = division
        return by_code

    def _import_states(
        self,
        country: Country,
        division_by_code: dict[str, Division],
        dghs: dict[str, Any],
    ) -> dict[str, State]:
        by_code: dict[str, State] = {}

        for row in dghs["districts"]:
            state = self._upsert_state(
                country=country,
                division=division_by_code.get(row.get("division_code", "")),
                name=row.get("name", ""),
                kind=State.StateKind.DISTRICT,
                source_code=row.get("code", ""),
            )
            if state:
                by_code[row["code"]] = state

        for row in dghs["city_corporations"]:
            state = self._upsert_state(
                country=country,
                division=division_by_code.get(row.get("division_code", "")),
                name=row.get("name", ""),
                kind=State.StateKind.CITY_CORPORATION,
                source_code=row.get("code", ""),
            )
            if state:
                by_code[row["code"]] = state

        return by_code

    def _upsert_state(
        self,
        *,
        country: Country,
        division: Division | None,
        name: str,
        kind: str,
        source_code: str,
    ) -> State | None:
        if not name:
            return None
        state, _ = State.objects.update_or_create(
            country=country,
            name=name,
            defaults={
                "division": division,
                "kind": kind,
                "source_code": source_code,
                "is_active": True,
            },
        )
        return state

    def _import_cities(
        self,
        state_by_code: dict[str, State],
        dghs: dict[str, Any],
        union_hierarchy: dict[str, Any] | None = None,
    ) -> tuple[dict[str, City], int]:
        by_code: dict[str, City] = {}

        for row in dghs["district_upazilas"]:
            city = self._upsert_city(
                state=state_by_code.get(row.get("district_code", "")),
                name=row.get("name", ""),
                kind=City.CityKind.UPAZILA_THANA,
                source_code=row.get("code", ""),
            )
            if city:
                by_code[row["code"]] = city

        for row in dghs["city_corporation_thanas"]:
            city = self._upsert_city(
                state=state_by_code.get(row.get("parent_code", "")),
                name=row.get("name", ""),
                kind=City.CityKind.CITY_CORPORATION_THANA,
                source_code=row.get("code", ""),
            )
            if city:
                by_code[row["code"]] = city

        paurashava_count = 0
        if union_hierarchy:
            for row in union_hierarchy.get("unions", []):
                if not is_paurashava_adm4(row):
                    continue
                district_code = pcode_digits(row.get("ADM2_PCODE", ""))
                city = self._upsert_city(
                    state=state_by_code.get(district_code),
                    name=paurashava_display_name(row),
                    kind=City.CityKind.PAURASHAVA,
                    source_code=row.get("ADM4_PCODE", ""),
                )
                if city:
                    source_code = row.get("ADM4_PCODE", "")
                    fallback_code = (
                        f"PAURASHAVA:{district_code}:{comparison_name(city.name)}"
                    )
                    by_code[
                        source_code or fallback_code
                    ] = city
                    paurashava_count += 1

        return by_code, paurashava_count

    def _upsert_city(
        self,
        *,
        state: State | None,
        name: str,
        kind: str,
        source_code: str,
    ) -> City | None:
        if not state or not name:
            return None
        city, _ = City.objects.update_or_create(
            state=state,
            name=name,
            defaults={
                "kind": kind,
                "source_code": source_code,
                "is_active": True,
            },
        )
        return city

    def _import_union_wards(
        self,
        state_by_code: dict[str, State],
        city_by_code: dict[str, City],
        review_hierarchy: dict[str, Any],
    ) -> int:
        count = 0
        for parent in review_hierarchy["parents"]:
            state = state_by_code.get(parent.get("parent_code", ""))
            if not state:
                continue
            for thana in parent["thanas"]:
                city = city_by_code.get(thana.get("code", ""))
                if not city or city.state_id != state.pk:
                    continue
                for row in thana["unions"]:
                    name = row.get("ADM4_EN", "")
                    if not name:
                        continue
                    if is_paurashava_adm4(row):
                        continue
                    kind = self._union_ward_kind(parent.get("parent_kind", ""), name)
                    UnionWard.objects.update_or_create(
                        city=city,
                        name=name,
                        defaults={
                            "kind": kind,
                            "source_code": row.get("ADM4_PCODE", ""),
                            "is_active": True,
                        },
                    )
                    count += 1
        return count

    def _union_ward_kind(self, parent_kind: str, name: str) -> str:
        normalized = comparison_name(name)
        if parent_kind == "city_corporation" or normalized.startswith("ward "):
            return UnionWard.UnionWardKind.WARD
        return UnionWard.UnionWardKind.UNION

    def _print_counts(self, counts: dict[str, int]) -> None:
        for label, count in counts.items():
            self.stdout.write(f"{label}: {count}")
