# Legacy data import — "Upload 60 Employees in Orbis - 2026.10.04.xlsx"

**Status: BUILT and APPLIED ON DEV 2026-10-05 (branch `legacy-60-import`,
not yet committed/deployed).** First batch of pre-Orbis HR data (60
HQ/support employees). Three data sets are in scope — **employee profile
information, salary history, and the AL calculation** — each with its own
upload file and importer. Out of scope for now: designation/department
changes (the `60` tab). Education and professional certifications **are**
imported, without institution/year, for audit purposes (decision 5 below).

**Dev run results (2026-10-05):**

| Upload | Result | Notes |
|---|---|---|
| 1a Profile (`/employees/bulk-upload/`, `profile_60.xlsx`) | 60 rows applied, 0 errors | personal/family/NID/education/certification columns; 52 education + 26 certification rows created without year/institution |
| 1b Addresses (`/employees/bulk-upload/`, `address_60.xlsx` — the user's cleaned `data/processed/Address_cleaned_upload_ready.xlsx`) | 60 rows, 0 errors, 0 warnings | 120 address rows; 119 resolve to a district + upazila/thana (one row has no permanent police station); 24 of 45 union texts also link to a `UnionWard` row, the other 21 stay as text (they are upazila/district names or wards the table has no row for) |
| 2 Salary history (`/payroll/salary/upload/history/`) | 60 employees, 420 rows, 0 errors | 59 first-row clamp warnings as predicted; 60 current APPROVED rows |
| 3 AL record (`/leave/al-history/upload/`, as of 2025-12-31) | 48 written, 12 skipped, 0 arithmetic warnings | skipped = the 8 2025 joiners + 4 2026 joiners (decision 6) |

Abir Hossain's follow-ups done on dev: stale AL cycle row deleted and
recreated on the 2026-04-01 anniversary; INITIAL designation-history row
moved to 2025-04-01. The four prepared files are in
`~/Downloads/orbis_upload_60/` for the prod run (`profile_60.xlsx` has no
address columns; `address_60.xlsx` carries them, cleaned and matched to
State / City / UnionWard by hand).

Source: `~/Downloads/Upload 60 Employees in Orbis - 2026.10.04.xlsx`:

| Tab | Rows | Content | Scope |
|---|---|---|---|
| `EMP Info 60` | 60 | 26 profile columns (parents, spouse, addresses, NID, blood group, DOB, height, education, certification) | **Upload 1** → `employees.*` profile sections |
| `Salary 60 2006-2026` | 420 | Per-employee gross salary history, one row per calendar year, Start/End/Amount | **Upload 2** → `payroll.SalaryStructure` |
| `AL Calc` | 60 | Till 2024 · 2025 AL Taken · 2025 AL Balance · 60 Cap Till 2025 | **Upload 3** → new `leave.LegacyAnnualLeaveRecord`, Service Book only |
| `60` | 60 | Designation, Department | not imported (FYI in §6) |
| `Sheet3 (2)` | 64 | `60` plus four extra IDs (4319, 4854, 4927, 4929) | scratch copy — ask HR whether the extras are a later batch |

All 60 IDs exist in Orbis and are active, and the same 60 appear in all three
data tabs. Production's `payroll_salarystructure` table was **empty** at the
2026-10-05 encryption rollout (`deploy/README.md`); dev holds ~1,500 dummy
rows dated 2026-07-01 with no `PayrollRun` referencing them — delete before
testing.

---

## 1. Decisions taken in review (2026-10-05)

1. **No `Address` model change.** The model already has every field the sheet
   needs; the drawer form labels `village_area` as "Address Line 1 / Village or
   Area", so a free-text present address lands there. The prep step splits the
   sheet's free-text columns into the model's columns (§2.1).
2. **No designation/department update** in this batch.
3. **AL figures are a historical HR record, not a ledger credit.** The four
   `AL Calc` columns are stored as given on a new, read-only model and shown
   **only in the Service Book** (Part 4) — not on `/leave/balance/`, not on the
   profile page, and never fed into `LeaveBalance`. The live Orbis AL ledger
   keeps running on 2026 roster data alone. Employees with **under 12 months'
   service at the as-of date (2025-12-31)** get no record, per the policy's
   eligibility gate.
4. **Service Book is HR + superuser only** — the employee's own self-view is
   removed from both the page and the PDF.
5. **Education and certifications are imported as HR recorded them** — a
   degree level and a certificate name, with no institution, year or result.
   HR needs them on file for audit now and will complete them later. This
   needs the two models relaxed (§2.3); nothing else in Orbis depends on those
   columns being present.
6. **AL eligibility is measured at the as-of date (2025-12-31)**, not today.
   The record is a snapshot of HR's position on that date, and Orbis's own
   ledger takes over from 2026-01-01: anyone who crosses 12 months' service in
   2026 starts their first AL cycle in Orbis at their anniversary and accrues
   live from there, so nothing is lost — the months before the anniversary
   carry no AL under the policy either way. 48 records written, 12 skipped.
7. **Abir Hossain (52756)**: joining date is **2025-04-01** (Orbis holds
   2025-05-01) and NID is **19902697557000100**. Both are fixed through
   Upload 1 (§2.1); his empty AL balance row is recreated afterwards (§5).
8. **`Sheet3 (2)` extras** (4319, 4854, 4927, 4929) are a later batch.

Consequence of decision 3, stated once and accepted: the live AL balance an
employee or supervisor sees in Orbis will not include the pre-Orbis bank (up
to 60 days). The advance-against-entitlement rule still allows requests up to
the 12-day annual cap; anything beyond that stays a manual HR decision. This
reverses the `upload_bank.py` posture recorded in
`docs/LEAVE_POLICY_IMPLEMENTATION.md` §5.7 ("pre-Orbis balance is seeded once
… into `LeaveBalance.opening`") — update that paragraph in the same change so
the doc records the reversal rather than silently diverging.

---

## 2. Upload 1 — employee profile information (`/employees/bulk-upload/`)

### 2.1 Prepared file `profile_60.xlsx`

One row per employee; blank cell = leave unchanged (the uploader's existing
contract). Columns and their mapping:

| Column | Sheet source | Target | Prep / normalisation |
|---|---|---|---|
| Employee ID | EID | key | |
| Joining Date | Joining date | `CustomUser.joining_date` | already supported (HR-only column). Blank for 59 rows; **2025-04-01 for 52756 only** |
| Father's Name / Mother's Name | | `PersonalInfo.father_name` / `.mother_name` | trim |
| Date of Birth | Birthday | `PersonalInfo.date_of_birth` | all 60 are real Excel dates; drop "Age" |
| Blood Group | | `PersonalInfo.blood_group` | `"O+ (O positive)"` → `O+`; the sheet's `O−`/`B−` use Unicode minus U+2212 → ASCII `-` |
| Height (inches) | Hight (Feet) + (Inc) | `PersonalInfo.height_in_inch` | `ft×12 + in`; 3 rows are `-` → blank |
| Identification Mark | | `PersonalInfo.identification_mark` | |
| Spouse Name | | `FamilyInfo.spouse_name` | 25 blank; set `marital_status=MARRIED` only when a spouse is given and the field is blank today |
| NID | National ID no. | `IdentificationNumber(id_type=NID)` | **as text**; see §6 for the one float value |
| Permanent Flat No / Holding No / Village or Area / Post Office / Union / Post Code | same-named columns | `Address(PERMANENT).flat_no / holding_no / village_area / post_office / union_name / post_code` | post codes are 4 digits (field allows 5); 3 blank. Where Village/Area is blank (2 rows) the free-text "Permanent Address" column fills `village_area` |
| Permanent District | District | `Address(PERMANENT).state` | strip " District"; Chittagong→Chattogram, Comilla→Cumilla, Bogra→Bogura, Barisal→Barishal, Netrokona→Netrakona, Brahmmanbaria→Brahmanbaria |
| Permanent Police Station | | `Address(PERMANENT).city` | `City` is the upazila/thana table; best-effort match within the district (e.g. Anowra→Anwara, Swandip→Sandwip, Gournadi→Gaurnadi); unmatched → warning, left null |
| Present Flat No / Holding No / Village or Area / Post Office / Union / Post Code / District / Police Station | split from the free-text "Present Address" | `Address(PRESENT).*` | **Superseded 2026-10-05**: the address columns were taken out of `profile_60.xlsx` and come from the user's hand-cleaned `address_60.xlsx` instead (both address types, district/upazila/union already matched to the lookup tables). Union text also sets the `union_ward` FK when a `UnionWard` row matches within the resolved city (numeric wards compared without leading zeros); otherwise the text is kept in `union_name` |
| Highest Education | Last Educational Qualification | `Education` row: `degree_level` + the sheet label kept in `degree_name` | Graduate→BACHELOR (33), Post Graduate→MASTER (21), Higher Secondary→HSC (2), Secondary School→SSC (1), Below Secondary School Certificate→**JSC** (3, confirmed 2026-10-05). Institution/year/result left empty (§2.3). Created only if the employee has no `Education` row at that level yet |
| Professional Certification | | `ProfessionalCertification.certificate_name` | one row per cell as written (34 blank), trimmed; institution/year empty. Created only if no row with the same name (case-insensitive) exists |

**Not imported** and reported as skipped by the parser: Age (derived from DOB).

### 2.2 Code — extend `employees/upload.py`

The uploader today only knows org columns (Team, Reports To, Resource/Billing
Status, Department, Joining Date, Gender, Register Serial No, Job/Work
Location, Last Working Date) and already upserts `PersonalInfo` for Gender.

- `_HEADER_ALIASES`: add the columns above (`Permanent …`/`Present …`
  prefixes resolve to the two address types).
- Scope rule: profile columns follow the same managed-scope rule as the
  drawers (HR/Admin everyone, a manager their own downline) — no new HR-only
  gate needed since nothing here is in `_HR_ONLY_FIELDS`.
- `apply_upload`: extend the `PersonalInfo` upsert; add `FamilyInfo`
  (OneToOne) upsert; `Address` upsert per `address_type` (update the
  employee's first row of that type, else create with `country=Bangladesh`
  — the FK isn't unique, so never create a second row of the same type);
  `IdentificationNumber` NID upsert (same "update the existing NID row, never
  add a second" rule — the model has no uniqueness constraint).
- `Education` / `ProfessionalCertification`: repeating sections, so the
  "blank = unchanged" rule becomes "create once": skip when a row at that
  `degree_level` / with that `certificate_name` already exists. The sheet's
  own label goes into `Education.degree_name` so the audit trail shows what HR
  recorded, not just our mapping.
- District/police-station resolution: a small normalisation map + name lookup
  against `State` and `City` (`City` filtered by the resolved `State`), built
  once per upload like the existing `_build_named_cache`.
- Template (`build_xlsx_template` HR variant): add the new columns with a
  Blood Group data-validation list.
- Tests: each new column parses/applies; blank leaves existing values;
  address upsert doesn't duplicate; NID upsert doesn't duplicate; education
  and certification create-once; degree-level mapping incl. the unknown-label
  error; district normalisation; unmatched police station is a warning not an
  error; Age is reported as skipped.

### 2.3 Model change — relax `Education` and `ProfessionalCertification`

Needed so a level-only / name-only row can exist (decision 5). Field
alterations only, no new table, so no collation step:

| Model | Field | Today | Change |
|---|---|---|---|
| `Education` | `passing_year` | `PositiveIntegerField` NOT NULL | `null=True, blank=True` |
| | `result`, `result_details`, `academic_institution`, `institution_category` | required | `blank=True` |
| `ProfessionalCertification` | `certificate_year` | `PositiveIntegerField` NOT NULL | `null=True, blank=True` |
| | `certificate_institution` | required | `blank=True` |

`__str__` on both, the two section partials (`_education.html`,
`_certification.html`) and the Service Book Part 1 lines (HTML template +
`seed_service_book_template.py` design) render "—" for the missing pieces.
The drawer forms keep working unchanged (`StyledMixin` ModelForms pick up the
relaxed requiredness). Ordering by `-passing_year` / `-certificate_year` is
fine with nulls. Record in `docs/PLAN.md` as a deliberate relaxation: the
profile sections now allow partial rows because HR's legacy records are
partial.

---

## 3. Upload 2 — salary history (`/payroll/salary/upload/history/`, new)

### 3.1 Why the existing gross-salary uploader can't take it

| Rule in `parse_salary_upload` / `apply_salary_upload` | What the sheet needs |
|---|---|
| `effective_from` within ±365 days of today | rows back to 2006-01-01 |
| one row per employee per file | 420 rows, ~7 per employee |
| supersedes **forward only** | the whole chain, oldest first, closed rows included |
| `effective_to` derived, never a column | explicit End Date; last row open |
| `employed_on(employee, effective_from)` | **59 of 60** first rows start on Jan 1 of the joining year, before the joining date (104: 2009-01-01 vs joined 2009-05-02) |

The data is otherwise clean: every Start is the 1st of a month, each
employee's rows are contiguous (End + 1 day = next Start), exactly one open
row (2026-01-01) each, positive integer amounts. The engine already prices a
chain of `SUPERSEDED` rows (`payroll/engine.py::_salary_segments`) and clamps
to the joining date, so no model change — only the importer.

### 3.2 Prepared file `salary_history_60.xlsx`

| Employee ID | Gross monthly salary | Effective from | Effective to | Reason (optional) |
|---|---|---|---|---|
| 104 | 33146 | 2009-01-01 | 2009-12-31 | |
| … | | | | |
| 104 | 145917 | 2026-01-01 | *(blank)* | |

Drop SL; Employee Name may stay as a trailing reference column. Blank Reason
= first row `New hire`, later rows `Annual increment` (labels as the existing
uploader accepts them).

### 3.3 Code — sibling "history" mode in `payroll/upload.py`

Leave the single-revision uploader untouched and add:

- `parse_salary_history_upload(file, filename)`: group by employee, sort by
  Effective from; **contiguity** (each End = next Start − 1; only the last
  row may have a blank End; exactly one open row); **no ±365-day bound**; a
  first row starting before the joining date is **clamped to the joining date
  with a warning** (HR's calendar-year convention — confirm, §6); any later
  row before joining is an error; amount > 0; Reason via `_coerce_reason`
  with the defaults above; employee lookup not restricted to `is_active`
  (same posture as the existing uploader) but every row must pass
  `employed_on` after clamping.
- `apply_salary_history_upload(viewer, parsed, *, note, batch)`: **one-shot
  per employee** — any existing `SalaryStructure` row → `skipped_existing`
  (later corrections use the normal forward uploader or admin). Writes the
  chain in one `transaction.atomic()` per employee: `revision_no` 1..n,
  closed rows `SUPERSEDED` with `effective_to`, last row `APPROVED` with
  `effective_to=NULL`, `processed_by/at` = importer, one
  `PayrollInputBatch(input_kind=SALARY, source=UPLOAD)` naming the source
  file. Constructs rows directly rather than via `supersede_salary_structure`
  (that is the forward path); the partial unique constraint is the net.
- `UploadSalaryHistoryView` at `/payroll/salary/upload/history/`, reusing
  `salary_upload.html` with a mode flag and its own report partial; a button
  from the salary list and the existing upload page. Same `payroll_salary`
  `ProtectedView` gate (Payroll Admins only) — the file is gross salary.
- Template download: prefilled Employee ID / Name / Joining date + one empty
  row per employee.
- Tests (`payroll/tests_upload_history.py`): gap, overlap, two open rows,
  first-row clamp warning, later-row-before-joining error, skip-existing,
  chain written with correct revisions/statuses/`effective_to`,
  `_salary_segments` prices a 2026 period from the imported chain.

### 3.4 Dev-only prerequisite

Delete the dummy 2026-07-01 `SalaryStructure` rows (all 1,508; none real; no
`PayrollRun` exists). Prod is already empty.

---

## 4. Upload 3 — AL calculation as a Service-Book record (`/leave/al-history/upload/`, new)

### 4.1 Model — `leave.LegacyAnnualLeaveRecord`

A read-only HR record of the pre-Orbis annual-leave position, stored exactly
as HR calculated it. **Not** a `LeaveBalance`, never read by
`live_available`/`describe_balance_impact`/the balance page.

| Field | From sheet | Notes |
|---|---|---|
| `employee` | EID | FK to `CustomUser`, `unique_together (employee, as_of)` |
| `as_of` | — | `DateField`, 2025-12-31 for this batch (uploader form field) |
| `accrued_before_year` | Till 2024 | Decimal(6,2) |
| `taken_in_year` | 2025 AL Taken | Decimal(6,2) |
| `balance_in_year` | 2025 AL Balance | Decimal(6,2) |
| `capped_balance` | 60 Cap Till 2025 | Decimal(6,2), ≤ 60 |
| `note` | sheet note column | e.g. Nawshad's "15 taken in 2025, 3 deducted from previous balance" |
| `source_file` | — | CharField, the upload's filename |
| `added_by/added_at/updated_by/updated_at` | — | `_Auditable` |

**Migration must include the collation CONVERT step and `atomic = False`**
(`CustomUser` pk is a `char(32)` UUID — copy `_match_existing_collation` from
`employees/migrations/0008_designationhistory.py`, per CLAUDE.md).

Admin: register with list/search; money-free so no `PayrollMoneyAdminMixin`.

### 4.2 Prepared file `al_history_60.xlsx`

| Employee ID | Till 2024 | 2025 AL Taken | 2025 AL Balance | 60 Cap Till 2025 | Note | Employee Name (ref) |
|---|---|---|---|---|---|---|
| 104 | 204 | 0 | 12 | 60 | | Kazi Md. Shahid Uddin |

All 60 rows go in the file; the parser applies the eligibility rule and
reports what it skipped, so HR sees the outcome in the report rather than in
a hand-edited file.

### 4.3 Code — `leave/upload_al_history.py` + view

Same parse/apply/template shape as `leave/upload_bank.py` (which stays as it
is, unused for this batch).

- Form: file + **As of** date (Flatpickr via `core.forms.use_date_picker`),
  required.
- Parser: employee must exist (active or not — a record is still history);
  all four numbers ≥ 0, `capped_balance ≤ 60`; soft warning when
  `capped_balance ≠ min(60, accrued_before_year + balance_in_year)` (HR's own
  arithmetic check, never blocks). **Eligibility**: `joining_date` missing →
  error; `joining_date + 12 months > as_of` → row **skipped** with
  "under 12 months' service at <as_of> — no AL allocated per policy". For
  2025-12-31 that excludes the 8 employees who joined in 2025 and the 4 who
  joined in 2026 → **48 records written**.
- Apply: **upsert** on `(employee, as_of)` — this is a plain record, not a
  ledger, so a corrected re-upload simply replaces the row (audit via
  `updated_by/at`). One `transaction.atomic()` for the batch.
- View `UploadAnnualLeaveHistoryView` at `/leave/al-history/upload/`, gated
  like the bank uploader (`can_edit_all` — HR/Admins); template download
  prefilled with ID/name/joining date; link from the Upload Center.
- Tests: eligibility skip at the boundary (joined exactly 12 months before
  as-of is eligible), cap > 60 error, arithmetic warning, upsert replaces,
  inactive employee accepted.

### 4.4 Service Book — the only place the record appears

`employees/services.py::build_service_book_context` gains
`sb_al_legacy` = the employee's `LegacyAnnualLeaveRecord` rows ordered by
`as_of` (normally one). Part 4 renders, **above** the Orbis availed-periods
table, a short "Annual leave record before Orbis (HR records, as at
2025-12-31)" block with the four figures and the note; the Orbis periods
table and its running balance stay exactly as they are (they describe the
Orbis ledger, which by decision 3 does not include these days). Both renderers
change together: `templates/employees/service_book.html` and the DB-stored
PDF design in `documents/management/commands/seed_service_book_template.py`
(re-run `seed_service_book_template` after editing, on dev and prod).

### 4.5 Service Book visibility — HR + superuser only

`employees/views.py::_can_view_service_book` drops the `user.pk == employee.pk`
clause and becomes `can_view_salary_history(user)` (HR group or superuser).
Both the HTML view and the PDF view already go through it. The profile card
(`templates/employees/sections/_service_book.html`) is already wrapped in
`{% if can_view_service_book %}`, so it disappears for the employee
automatically. Update the docstring and the CLAUDE.md "Service Book" sentence
("Visible only to the employee themselves or the HR group") in the same
change. Test: employee self-view → 403 on page and PDF; HR → 200.

---

## 5. Run order — prod checklist (dev already done)

1. Merge/deploy the branch (`update_orbis.sh`), which runs `migrate`
   (`employees/0014`, `leave/0013` — the latter has the collation step).
2. `manage.py seed_orbis --menus-only` (new ProtectedView rows:
   `payroll_salary_upload_history*` → Payroll Admins,
   `leave_al_history_upload*` → HR + User Admins) and
   `manage.py seed_service_book_template` (Part 4 block, relaxed Part 1
   lines).
3. **Upload 1 — profile** (`profile_60.xlsx`, then `address_60.xlsx`, both
   HR/Admin at `/employees/bulk-upload/`). Expect 0 errors and 0 warnings
   on each. Then the 52756 follow-up: delete his empty AL
   `LeaveBalance` row (admin) so the next balance read recreates it on the
   corrected 2026-04-01 anniversary, and move his INITIAL
   `DesignationHistory` row to 2025-04-01. **Must run before Upload 2** (the
   salary clamp reads the joining date).
4. **Upload 2 — salary history** (`salary_history_60.xlsx`, Payroll Admins
   at `/payroll/salary/upload/history/`). Preview first: 60 employees, 420
   rows, 59 clamp warnings, 0 skipped (prod's table is empty).
5. **Upload 3 — AL record** (`al_history_60.xlsx`, HR at
   `/leave/al-history/upload/`, as of **2025-12-31**). Expect 48 written /
   12 skipped.
6. Open the Service Book for 104 as HR; confirm an employee gets 403 on
   their own.

## 6. Remaining items for HR

1. **Salary start dates** are Jan 1 of the joining year for 59 of 60 — the
   importer clamps the first row to the joining date. Confirm nobody was paid
   before their recorded joining date.
2. **Addresses** come from the hand-cleaned `address_60.xlsx`; its "Review
   Notes" sheet lists the 39 rows where a spelling was corrected or a union
   was kept as text. 21 union/ward texts have no `UnionWard` row (e.g.
   "Mirpur", "Pirojpur", "Ward No - 07" under Rupnagar) and stay as plain
   text — fine for the record, HR can pick a row from the drawer later.
3. **S. M. Rashel (539)**: the dummy NID in Orbis was replaced by the
   sheet's 2694259598029 (dev); the same will happen on prod.
4. **FYI, not imported now**: the `60` tab differs from Orbis for 35 of 60
   designations (22 have none in Orbis; 12 sheet titles don't exist as
   `Designation` rows) and three departments (656, 539 → Software
   Development; 4704 → Financial Information Systems). Separate exercise.

Resolved 2026-10-05: eligibility cut-off at the as-of date (decision 6);
Abir Hossain's joining date and NID (decision 7); `Sheet3 (2)` extras are a
later batch (decision 8); education/certifications imported partial
(decision 5) with "Below Secondary School Certificate" → JSC; the
live-ledger consequence of decision 3 accepted.
