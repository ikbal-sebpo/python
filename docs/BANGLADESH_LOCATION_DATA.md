# Bangladesh Location Data Research

Status: data research and import runbook. The schema support exists in
`employees`, but production import still requires a generated, validated source
artifact.

## Goal

For the Bangladesh-only address taxonomy, we need a verified dataset that can
eventually map onto:

- `State` as District or City Corporation
- `City` as Upazila, Thana, or Paurashava, scoped by the selected `State`
- `UnionWard` as Union or Ward No, scoped by the selected `City`
- `Address.union_name` as a temporary legacy/free-text fallback
- the existing detailed address fields after Union/Ward, effectively the local
  address line

The schema is now `Country -> Division -> State -> City -> UnionWard`, with
`Address.union_name` kept as a plain-string fallback during migration.

## Current Mapping Decision

Use the employees taxonomy models as:

- `Country`: Bangladesh
- `Division`: the 8 divisions
- `State`: either a district or a city corporation
- `City`: an upazila, thana, or paurashava under the selected district or
  city corporation
- `UnionWard`: union or ward no under the selected upazila/thana/paurashava
- `Address.union_name`: optional free-text fallback when the value is not listed
- local address detail: `village_area` as Address Line 1 / Village or Area,
  followed by `holding_no`, `flat_no`, and `post_code`

Operational labels should be Bangladesh-specific:

- `State` -> `District / City Corporation`
- `City` -> `Upazila / Thana / Paurashava`
- `UnionWard` -> `Union / Ward No`
- `union_name` -> `Union / Ward No (if not listed)`

This keeps Dhaka-style collisions safe because `City` is unique per `State`, so
the same thana name can exist once under a district and again under a city
corporation.

Paurashava rows are not `UnionWard` rows. They are equivalent to the
upazila/thana selection level for this address UI, so they import as
`City(kind=PAURASHAVA)` under the district `State`. For example, the source row
`BD30333499 Kaliganj` under Gazipur/Kaliganj is treated as `Kaliganj
Paurashava`, leaving the seven Kaliganj union parishads as `UnionWard` rows
under `Kaliganj` Upazila.

## Current Local State

`employees/models.py` has:

- `Country`
- `Division`
- `State(country, division, name, kind, source_code)`
- `City(state, name, kind, source_code)` where `kind` is upazila/thana,
  city-corporation thana, or paurashava
- `UnionWard(city, name, kind, source_code)`
- `Address(country, state, city, union_ward, union_name)`

`employees/fixtures/seed_taxonomy.json` is a small sample only. It includes
Bangladesh plus a few districts and thana-like city rows, not the complete
Bangladesh hierarchy.

## Sources Selected

### 1. Current public counts

Source: Bangladesh National Portal

- Division list: `https://bangladesh.gov.bd/views/division-list/`
- District list: `https://bangladesh.gov.bd/views/district-list`
- Upazila list: `https://bangladesh.gov.bd/views/upazila-list/Upazilla-List/`
- Union list: `https://bangladesh.gov.bd/views/union-list/`

As of the portal pages checked on 2026-07-21, the public counters show:

- 8 divisions
- 64 districts
- 499 upazilas
- 4599 unions

Use these as the high-level completeness targets. The national portal union
page exposes the count, but the visible page uses select controls rather than a
static all-unions table, so it is not the easiest bulk source.

### 2. Rural union hierarchy

Source: ArcGIS FeatureServer

`https://services.arcgis.com/5T5nSi527N4F7luB/ArcGIS/rest/services/bgd_adm4_union/FeatureServer/0`

The layer is named `bgd_bnda_adm4_union`, described as Bangladesh
administrative level 4 union boundaries. It exposes the hierarchy fields needed
for non-geometry import:

- `ADM1_EN`, `ADM1_PCODE` for division
- `ADM2_EN`, `ADM2_PCODE` for district
- `ADM3_EN`, `ADM3_PCODE` for upazila/thana
- `ADM4_EN`, `ADM4_PCODE` for union

This is the best machine-readable source for union names and parentage. The
fetch script requests attributes only, not polygons.

### 3. City corporation and thana separation

Source: Bangladesh Core FHIR Implementation Guide, DGHS/MoHFW

`https://fhir.dghs.gov.bd/core/CodeSystem-bd-geocodes.html`

The `bd-geocodes` CodeSystem is useful because it distinguishes city
corporation local-authority codes from district-level "N/A" codes. For example,
Dhaka North and Dhaka South have separate city corporation codes, and thana
codes under those prefixes are different from Dhaka district upazilas such as
Dhamrai, Dohar, Keraniganj, Nawabganj, and Savar.

Use this source to split:

- district/upazila rows for `State(kind=district)` -> `City`
- city corporation/thana rows for `State(kind=city_corporation)` -> `City`

Do not use the CodeSystem's "Unions Of ..." rows as actual union names; those
are grouping placeholders, not individual union parishads.

Known review flags from the raw CodeSystem JSON:

- `302625 Dhaka North City Corperation` has a spelling error in the source.
- `30260022 Gaffargaon` and `30260023 Gauripur` appear with a Dhaka district
  prefix even though the national portal lists Gafargaon/Gauripur under
  Mymensingh district. Do not seed those parent links without manual
  reconciliation.

### 4. Union/ward hierarchy with city corporation labels

Source: DGHS ArcGIS FeatureServer

`https://gis.dghs.gov.bd/server/rest/services/Hosted/All_Union/FeatureServer/0`

This layer is useful as a city-corporation cross-check because it exposes:

- `division`
- `district`
- `upazila`
- `union_`
- `adm4_pcode`

The layer metadata lists `district` as the renderer field and includes values
such as `Barisal City Corporation`, `Comilla City Corporation`, `Mymensingh
City Corporation`, and `Rajshahi City Corporation`. That makes it relevant for
the user's requirement that city corporation thanas remain distinct from
same-name districts.

The fetch script accepts exports from this layer through `--arcgis-file`; it
maps `division/district/upazila/union_` into the same normalized
`ADM1/ADM2/ADM3/ADM4` output used for other union-boundary sources. It can
also query this layer directly with `--union-source dghs-all-union`. The live
URL was not made the default because its coverage still needs to be counted and
compared with the national portal and ArcGIS ADM4 layer.

### 5. City corporation list ambiguity

Sources:

- Local Government Division subordinate offices:
  `https://lgd.gov.bd/pages/static-pages/69414020c4774958d7b54af5`
- Bangladesh Election Commission city corporation election page:
  `https://ecs.gov.bd/en/page/city-corporation-election-result`

The Election Commission page lists 12 city corporations. The Local Government
Division page lists those 12 and also includes "Bogura City Corporation" with a
blank website, plus a separate LGD page references a Bogura City Corporation
establishment notification.

For seeding, treat the 12 shared by DGHS/ECS/LGD as confirmed. Keep Bogura as a
manual-review candidate until its operational/electoral status is confirmed and
its thana/ward boundary data is available from an authoritative source.

Confirmed city corporations in the DGHS/ECS/LGD set:

- Barishal City Corporation
- Chattogram City Corporation
- Cumilla City Corporation
- Dhaka North City Corporation
- Dhaka South City Corporation
- Gazipur City Corporation
- Khulna City Corporation
- Mymensingh City Corporation
- Narayanganj City Corporation
- Rajshahi City Corporation
- Rangpur City Corporation
- Sylhet City Corporation

Manual-review candidate:

- Bogura City Corporation

### 6. Offline community fallback for complete unions

Source: Bangladesh GeoCode

`https://github.com/nuhil/bangladesh-geocode`

This MIT-licensed repository includes divisions, districts, upazilas, and
unions in CSV, JSON, PHP, SQL, and XML. Its README says the fields were taken
from `bangladesh.gov.bd`, Wikipedia, and Google Maps. It is useful when the
official ArcGIS endpoints are inaccessible from a local environment, and for
cross-checking union spellings and parentage.

Do not treat this repository as the final authority without validation: the
national portal currently shows 499 upazilas and 4599 unions, while several
community APIs and datasets seen during research expose lower counts such as
495 upazilas and about 4550 unions.

## Fetch Script

Run from the project root on a machine with internet access:

```bash
python3 tools/fetch_bangladesh_locations.py \
  --output data/research/bangladesh_locations.json \
  --csv-dir data/research/bangladesh_locations_csv
```

By default this queries the ArcGIS ADM4 union boundary layer for the union
hierarchy and the DGHS CodeSystem for city-corporation/thana separation.

To test the DGHS hosted `All_Union` layer as the union hierarchy instead:

```bash
mkdir -p data/source/bangladesh

curl -L \
  'https://gis.dghs.gov.bd/server/rest/services/Hosted/All_Union/FeatureServer/0/query?f=json&where=1%3D1&outFields=adm4_pcode,division,district,upazila,union_&returnGeometry=false&orderByFields=division,district,upazila,union_' \
  -o data/source/bangladesh/dghs_all_union.json
```

Then either use the live source:

```bash
python3 tools/fetch_bangladesh_locations.py \
  --union-source dghs-all-union \
  --output data/research/bangladesh_locations.json \
  --csv-dir data/research/bangladesh_locations_csv
```

Or use the downloaded export:

```bash
python3 tools/fetch_bangladesh_locations.py \
  --arcgis-file data/source/bangladesh/dghs_all_union.json \
  --dghs-file data/source/bangladesh/CodeSystem-bd-geocodes.json \
  --output data/research/bangladesh_locations.json \
  --csv-dir data/research/bangladesh_locations_csv \
  --fail-on-readiness-blockers
```

To use a downloaded checkout of `nuhil/bangladesh-geocode` as an offline union
fallback:

```bash
python3 tools/fetch_bangladesh_locations.py \
  --union-source geocode-complete \
  --geocode-complete-dir data/source/bangladesh/bangladesh-geocode \
  --dghs-file data/source/bangladesh/CodeSystem-bd-geocodes.json \
  --output data/research/bangladesh_locations.json \
  --csv-dir data/research/bangladesh_locations_csv \
  --fail-on-readiness-blockers
```

To inspect only the DGHS district/upazila and city-corporation/thana code
hierarchy without fetching union rows:

```bash
python3 tools/fetch_bangladesh_locations.py \
  --union-source none \
  --output data/research/bangladesh_locations.json \
  --csv-dir data/research/bangladesh_locations_csv
```

If this environment has no DNS/network access, download the official source
files manually and run the script offline:

```bash
mkdir -p data/source/bangladesh

curl -L \
  'https://services.arcgis.com/5T5nSi527N4F7luB/ArcGIS/rest/services/bgd_adm4_union/FeatureServer/0/query?f=json&where=1%3D1&outFields=ADM0_EN,ADM0_PCODE,ADM1_EN,ADM1_PCODE,ADM2_EN,ADM2_PCODE,ADM3_EN,ADM3_PCODE,ADM4_EN,ADM4_PCODE&returnGeometry=false&orderByFields=ADM1_EN,ADM2_EN,ADM3_EN,ADM4_EN' \
  -o data/source/bangladesh/arcgis_adm4_union.json

curl -L \
  'https://fhir.dghs.gov.bd/core/CodeSystem-bd-geocodes.json' \
  -o data/source/bangladesh/CodeSystem-bd-geocodes.json
```

Then run:

```bash
python3 tools/fetch_bangladesh_locations.py \
  --arcgis-file data/source/bangladesh/arcgis_adm4_union.json \
  --dghs-file data/source/bangladesh/CodeSystem-bd-geocodes.json \
  --output data/research/bangladesh_locations.json \
  --csv-dir data/research/bangladesh_locations_csv \
  --fail-on-readiness-blockers
```

If the ArcGIS query endpoint truncates results because of service page-size
limits, use the script's live mode on a network-enabled machine instead of a
single downloaded query result. The script paginates through FeatureServer
results; a manually downloaded one-shot query may not.

Accepted ArcGIS inputs are CSV, FeatureServer JSON, GeoJSON, or a plain JSON
list with the `ADM*_EN` / `ADM*_PCODE` fields. `--arcgis-file` also accepts a
DGHS `All_Union` export with `division`, `district`, `upazila`, `union_`, and
`adm4_pcode` fields. `--union-file` is an alias for the same local-file option.
Accepted DGHS CodeSystem inputs are JSON or saved HTML.

For secondary comparison only, the script can also read community static JSON
files named `divisions.json`, `districts.json`, `upazilas.json`, and
`unions.json` from one directory:

```bash
python3 tools/fetch_bangladesh_locations.py \
  --arcgis-file data/source/bangladesh/arcgis_adm4_union.json \
  --dghs-file data/source/bangladesh/CodeSystem-bd-geocodes.json \
  --community-source-dir data/source/bangladesh/bdlocation \
  --output data/research/bangladesh_locations.json \
  --csv-dir data/research/bangladesh_locations_csv
```

Community sources seen during research include `bdlocation.vercel.app`,
`bdopenapi.vercel.app`, and `bdapis.pro.bd`. Their published counts differ
from the national portal, so they should be used to spot spelling/parentage
differences, not as the final authority.

The script can also read `nuhil/bangladesh-geocode` as a cross-check without
selecting it as the union hierarchy:

```bash
python3 tools/fetch_bangladesh_locations.py \
  --arcgis-file data/source/bangladesh/arcgis_adm4_union.json \
  --dghs-file data/source/bangladesh/CodeSystem-bd-geocodes.json \
  --geocode-complete-dir data/source/bangladesh/bangladesh-geocode \
  --output data/research/bangladesh_locations.json \
  --csv-dir data/research/bangladesh_locations_csv
```

To validate a generated artifact later without re-fetching any source data:

```bash
python3 tools/fetch_bangladesh_locations.py \
  --validate-artifact data/research/bangladesh_locations.json \
  --csv-dir data/research/bangladesh_locations_csv \
  --fail-on-readiness-blockers
```

This recomputes the summary counts, validation-detail counts, seed-readiness
status, and hierarchy counts from the stored JSON rows. It exits with status 1
for an internally inconsistent artifact and status 2 when the artifact is
consistent but still has `seed_readiness` blockers.

The script creates parent directories and writes:

- source metadata
- selected union hierarchy rows
- unique division/district/thana/union summaries from the selected union source
- normalized source rows before deduplication, so malformed or duplicate rows
  remain visible during validation
- DGHS district/upazila and city-corporation/thana rows
- `orbis_current_schema_preview`, which maps districts and city corporations
  into `State`, maps upazilas/thanas/paurashavas into `City`, and maps
  unions/wards into `UnionWard`
- `review_hierarchy`, a parent-aware hierarchy matching the target review
  question: district parents and city-corporation parents, each with thana rows
  and attached union rows when the selected union source has them
- validation warnings against the national portal counts
- `validation_details`, including missing required fields, duplicate groups,
  selected-source thana names missing from DGHS district upazilas, DGHS
  district upazilas with no selected union rows, and city-corporation thana
  names that collide with district upazila names in the same district
- `seed_readiness`, a conservative gate that is `ready_for_schema_decision`
  only when required counts match, a non-community union hierarchy is selected,
  DGHS exposes the expected district/city-corporation separation, and blocking
  data-quality checks are clear. Otherwise it is `blocked` with explicit
  blocker messages. `--fail-on-readiness-blockers` exits with status 2 when
  blockers remain.

With `--csv-dir`, the script also writes review CSVs:

- `dghs_divisions.csv`
- `dghs_districts.csv`
- `dghs_local_authorities.csv`
- `dghs_district_upazilas.csv`
- `dghs_city_corporation_thanas.csv`
- `union_hierarchy_unions.csv`
- `model_paurashava_cities.csv`
- `review_hierarchy_thanas.csv`
- `review_hierarchy_unions.csv`
- `review_unmatched_union_thanas.csv`
- `validation_union_thanas_missing_in_dghs.csv`
- `validation_dghs_upazilas_without_union_rows.csv`
- `validation_city_thana_name_collisions.csv`
- `validation_union_rows_missing_required_fields.csv`

Before using any generated file for seed work, review `validation_warnings`
and the four `validation_*.csv` files. The collision CSV is especially
important for the Dhaka-style case in the user request: same-name thana rows
must stay attached to either their city corporation parent or their district
upazila parent, not collapse into one `City` row accidentally.

`seed_readiness.status = ready_for_schema_decision` does not mean "import this
blindly." It means the data has passed the scripted completeness and
malformation checks and is ready to seed into the current
`State`/`City`/`UnionWard` mapping after review.

The Codex sandbox used for this research cannot resolve external hosts from the
shell, so the script was added for repeatable local execution rather than run
successfully in this environment.

## Import Runbook

After generating and reviewing `data/research/bangladesh_locations.json`, apply
the schema migration:

```bash
python manage.py migrate employees
```

Then inspect the import without committing writes:

```bash
python manage.py import_bangladesh_locations --skip-union-wards --dry-run
```

For a clean replacement of Bangladesh `State` and `City` rows while the
Union/Ward source still has readiness blockers:

```bash
python manage.py import_bangladesh_locations --skip-union-wards --replace --dry-run
```

If existing employee addresses already reference old Bangladesh state/city rows,
the replacement command stops. Prefer remapping those old links by State/City
name first:

```bash
python manage.py import_bangladesh_locations \
  --skip-union-wards \
  --replace \
  --remap-addresses \
  --remap-report data/research/address_remap_report.csv \
  --dry-run
```

Remove `--dry-run` only when the `address_city_unresolved` and
`address_city_ambiguous` counts are acceptable. The remap report CSV remains on
disk even when the dry-run transaction rolls back.

After backing up and deciding old links can simply be cleared instead, run:

```bash
python manage.py import_bangladesh_locations \
  --skip-union-wards \
  --replace \
  --force-address-detach
```

Only import `UnionWard` rows after the artifact's Union/Ward validation issues
are reviewed. The ADM4 source fetched on 2026-07-22 included 5119 ADM4 rows,
while the national portal union counter is 4599; that difference appears to be
ward/municipality coverage plus some malformed/duplicate source rows, not a
clean rural-union-only list.

For development, after the `State`/`City` import has been reviewed, import the
Paurashava `City` rows and the attached Union/Ward rows without replacing
addresses or taxonomy:

```bash
python manage.py migrate employees

python3 tools/fetch_bangladesh_locations.py \
  --output data/research/bangladesh_locations.json \
  --csv-dir data/research/bangladesh_locations_csv

python manage.py import_bangladesh_locations \
  --allow-blocked-artifact \
  --dry-run
```

The artifact is still `seed_readiness: blocked`, so `--allow-blocked-artifact`
is intentional for this development import. Remove `--dry-run` only after the
Union/Ward count looks acceptable. With the 2026-07-22 source, the script
classifies 302 ADM4 rows as Paurashava `City` rows, including code-only
municipality rows such as `BD30333499 Kaliganj`. Those are skipped during
`UnionWard` import. After regenerating the artifact with the current
code-based matcher, the expected development dry-run count is 895 actual
`City` rows, including 302 paurashavas, and 4681 `UnionWard` rows. The command
also reports `city_source_refs: 897` because the DGHS source has duplicate
Eidgaon and Naldanga district-upazila concepts that collapse under the
`City(state, name)` uniqueness rule. Kaliganj Upazila in Gazipur imports
exactly seven union parishads.

Do not use `--allow-blocked-artifact` for production seed data. It exists only
for review databases when a source artifact is internally valid but still has
explicit seed-readiness blockers.

## Open Decisions Before Seeding

- Find or confirm an authoritative ward source for city corporations. Rural
  unions are covered by the selected union hierarchy, but city corporations
  usually need wards rather than unions.
- Decide whether to preserve English names only or store Bangla names. Source
  codes are now available on `State`, `City`, and `UnionWard`, but Bangla names
  would still need new fields or a separate fixture format.
- Verify Bogura City Corporation before including it in seed data.
