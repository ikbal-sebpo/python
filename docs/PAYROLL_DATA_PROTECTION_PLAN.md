# Payroll Data Protection — field-level encryption + money-visibility tightening

**Companion to [`PAYROLL_PLAN.md`](PAYROLL_PLAN.md)** (technical spec) and [`PAYROLL_PROCESS.md`](PAYROLL_PROCESS.md) (business process).
**Status:** Phase 0 applied on production; Phase 1 **deployed to production 2026-10-05** (migrate with the purge flag, `seed_orbis --menus-only`, `seed_tax_declarations`, token check from the MySQL CLI passed) — see the progress notes under §7. Phase 2's remaining side-channel items are next. Decisions below were confirmed with the product owner on 2026-10-05.
**Date:** 2026-10-05.

---

## 1. Problem and threat model

Every monetary figure Orbis holds — gross salary, run register amounts, TDS, bonuses, tax
declarations, bank account numbers — is stored as plaintext `DECIMAL`/`VARCHAR` columns in
MySQL. Anyone with database credentials can read it with a single `SELECT`, and a `mysqldump`
carries it all out of the building.

**Who we are protecting against (confirmed 2026-10-05):** developers hold *database
credentials only*. They do **not** have SSH access to the production VPS, so they cannot read
`.env`, run `manage.py shell`, or attach to the gunicorn/Celery processes. That makes
application-level field encryption a complete answer for this threat: the database, every
dump, backup and replica of it, and every DBA tool show ciphertext, while the application
(which holds the key) decrypts for the roles allowed to see money.

**What this does not cover, and why that is acceptable today:**

- Anyone with a shell on the VPS as the app user (or root) can read the key from `.env` and
  decrypt. Today that is the product owner and the deploy pipeline only. If developers ever
  get production shell access, that becomes a separate control (key in a KMS/secret store the
  app fetches at boot, not a file), noted in §9.
- MySQL's own tablespace encryption (TDE) protects data *files* from disk theft, not data
  from credential holders. It is an optional defence-in-depth layer (§7 Phase 3), **not** a
  substitute for field encryption.

Two alternatives were considered and rejected:

- **Per-role decryption keys** (a Payroll Admin unlocks a key when acting). The engine runs
  in Celery with no user present and must read every employee's gross; a key the batch can't
  reach is a key the batch can't use.
- **A separate payroll database / Django DB router.** Django can't enforce FKs across
  databases, and the register views join through `employee__department_id`. Blast-radius
  reduction is instead achieved by MySQL grants (§7 Phase 0).

## 2. Inventory — where the money is today

Everything below is plaintext at rest right now.

### 2.1 Per-employee money columns (the core problem)

| Table | Columns | Read in SQL anywhere? |
|---|---|---|
| `payroll_salarystructure` | `gross_monthly` | No (Python only) |
| `payroll_teamsalarypackage` | `gross_monthly` | No (`active_team_salary_packages` resolves in memory) |
| `payroll_payrollrunemployee` | `gross_earnings`, `total_additions`, `total_deductions`, `taxable_gross`, `tax_deducted`, `net_pay`, `bank_account_number` | **Yes** — `payroll/views.py` register: `filter(gross_earnings__gt=0)`, `order_by("-net_pay")`, `aggregate(Sum(...))` |
| `payroll_payrollrunline` | `amount`, `full_amount`, `rate` | **Yes** — `Sum("amount")` grouped by component in `payroll/views.py` (`_deduction_breakdown_totals` & friends) and `payroll/exports.py::_earning_breakdown` |
| `payroll_payrollrun` | `gross_total`, `total_additions`, `total_deductions`, `tax_total`, `net_total` | No |
| `payroll_payrollvariance` | `prev_net`, `curr_net`, `delta`, `delta_pct` | No |
| `payroll_taxcomputation` | `gross_annual_projected`, `exempted_income`, `taxable_income`, `tax_before_rebate`, `rebate_amount`, `net_tax_payable`, `source_tax`, `other_adjustment`, `annual_tds`, `monthly_tds`, `breakdown_json` | No |
| `payroll_employeetaxdeclaration` | `allowable_investment`, `actual_investment`, `source_tax`, `ait_car_advance_tax`, `opening_tds_deducted`, `opening_additional_pay` (the six `investment_*` columns and `other_tds_adjustment` were replaced 2026-10-07; `ait_document` is a file under `media/payroll/tax_declarations/<employee uuid>/<random>.ext`, streamed only by `payroll_tax_declaration_document`) | No |
| `payroll_variablepayentry` | `amount` (bonus / incentive / disciplinary), `attachment` file | No |
| `payroll_tdsentry` | `amount` | No |
| `payroll_employeebankaccount` | `account_number`, `routing_number` | Verify at build time (see §4.2) |

### 2.2 Plaintext copies hiding in text, JSON and files

Field encryption alone misses these. Each one is closed in §7 Phase 2.

- `PayrollControlCheck.message` embeds both figures for `SALARY_PACKAGE_MISMATCH`
  (`payroll/engine.py` ~L437: "Paid 25000 but the … package is 18000.") and net pay for the
  negative-net check (~L684).
- `PayrollRunAudit.detail_json` stores `net_total` on every calculate (`payroll/engine.py` ~L811).
- `__str__` on `SalaryStructure`, `TeamSalaryPackage`, `VariablePayEntry`, `TDSEntry`,
  `PayrollRunLine`, `PayrollVariance`, `TaxComputation`, `EmployeeBankAccount`,
  `MealDeductionTier`, `PayRate` includes the amount. Django writes `str(obj)` into
  `django_admin_log.object_repr` on every admin save, so **that table already holds
  historical figures**.
- `media/payroll/batches/` keeps every uploaded workbook forever (`PayrollInputBatch.uploaded_file`)
  — including `salary_structure_upload.xlsx` and the Test Gross Salary files;
  `media/payroll/variable_pay/` keeps attachments.
- `documents_documentrequest.input_data` holds the salary certificate's three months of
  gross/bonus/net figures (`documents/pdf.py` `salary_month_*` keys); the rendered PDF lives
  under `media/documents/generated/<CODE>_<uuid>.pdf`.
- **Production nginx serves the whole `/media/` tree with no session check** (recorded in
  `deploy/README.md` 2026-08-29, re-confirmed 2026-10-05: `/media/payroll/` and
  `/media/documents/generated/` return an nginx 403 — alias live, autoindex off — so any file
  under them is served to anyone who knows its path). Letter PDFs are behind an unguessable
  UUID (obscurity, not authorisation — a leaked link is enough). **Payroll batch workbooks
  and variable-pay attachments keep the uploader's original filename** (`upload_to=
  "payroll/batches/"`), so `/media/payroll/batches/salary_structure_upload.xlsx`-style URLs
  are guessable. Neither directory is linked from any template: the in-app paths are
  `documents/views.py::DownloadPdfView` (login + owner/signer/reviewer/`can_see_all`) and the
  deliberately public `/verify/<uuid>/` preview, both of which stream the file themselves.
- Free text: `PayrollRunEmployeeNote.note`, `SalaryStructure.note`, `VariablePayEntry.reason`.
  Finance used to type package figures into notes (see `TeamSalaryPackage`'s docstring).
- `scheduler_scheduledtaskrun.output` captures command stdout. No payroll command is known to
  print amounts; `import_placeholder_july_salaries` must be checked before it is ever
  dispatched via beat.
- The production SQL dump recorded in `deploy/README.md` ("Git history note") is still in git
  history. It predates payroll (2026-06-08), so it carries PII but not salaries.

**Confirmed clean:** `PayrollRunEmployee.inputs_json`, `PayrollRun.config_snapshot` and
`PayrollInputBatch.report_json` are never written with money (the first two are never
written at all; `report_json` only ever stores an error string from `payroll/meals.py`).
No `logger` call in `payroll/` writes an amount to `logs/orbis.log`. Exports
(`payroll/exports.py`) are transient HTTP responses, never stored.

### 2.3 Who can see money today — four different answers

| Screen | Gate today | Groups that see gross/net |
|---|---|---|
| Run register money columns, calculated export | `accounts.permissions::can_see_payroll_money` | Payroll Admins (+ superuser) |
| Gross salary list, uploader, pending/decide | `ProtectedView` `payroll_salary` | **Finance** |
| Service Book salary history (`/employees/<uuid>/service-book/`) | `employees/views.py::_can_view_service_book` → `can_edit_all` | **HR + User Admins** |
| Bulk onboarding preview + results workbook (gross column) | `accounts/upload_views.py` `StaffRequiredMixin` → `can_edit_all` | **HR + User Admins** |
| Django admin (`SalaryStructureAdmin`, `PayrollRunEmployeeAdmin`, `TaxComputationAdmin`, `PayrollVarianceAdmin`, `VariablePayEntryAdmin`, `TDSEntryAdmin`, `EmployeeTaxDeclarationAdmin`, `TeamSalaryPackageAdmin`, `EmployeeBankAccountAdmin`) | `is_staff` + model perms | **any staff user** |

## 3. Decisions (2026-10-05)

1. **Encrypt at the application layer**, with a dedicated key separate from `SECRET_KEY`.
   The database never sees a plaintext amount.
2. **No migration of existing data.** There is no real financial data in any Orbis database
   yet (still building/testing), so the conversion migrations **delete the rows of the
   converted tables** instead of backfilling. The purge is guarded (§5.4) so it can never run
   silently against real data later.
3. **One money-visibility matrix** replaces today's four:

   | Screen | New rule |
   |---|---|
   | Run register money columns, calculated export, tax declarations/TDS | Payroll Admins (unchanged) |
   | Gross salary list, uploader, template, pending list, decide | **Payroll Admins only** (was Finance) |
   | Service Book salary history | **HR group only** (+ the employee themselves, + superuser). User Admins removed. |
   | Bulk onboarding upload / preview / results workbook | **HR group only** (+ superuser). User Admins removed, because the sheet and results carry gross. To make that usable, the **Users** sidebar section is opened to HR as well (HR already passes `UserListView`/`UserCreateView`'s `can_edit_all` gate; only the menu hid it). `/users/create/` carries no salary and keeps `can_edit_all`. |
   | Django admin money fields | **superuser + Payroll Admins**. Other staff still see the rows (ops edits like fixing a status keep working) with the money fields removed from list/detail. Today exactly one person holds both roles. |
   | Everything else (rate cards, meal tiers, tax slabs, overtime hours, meal counts) | unchanged — not personal money |

4. **Rate cards stay plaintext.** `PayRate.amount`/`min_gross`/`max_gross`,
   `MealDeductionTier.*` and the `TaxYear`/`TaxSlab`/`TaxExemptionThreshold` tables are
   policy and public law, not personal data, and `payroll/rates.py` + the engine's meal-tier
   lookup filter/sort on them in SQL. Recorded in `PAYROLL_PLAN.md` as an explicit exception.
5. **Key custody:** the product owner holds the live key (production `.env`); the **CTO holds
   the one offline copy, on paper**, handed over in person. Never in the repo, the deploy
   scripts, email, or chat. Rotation (`rotate_payroll_keys`) means a new printed copy to the CTO
   and the old one destroyed.
6. **Uploaded batch workbooks are deleted once a batch reaches `APPLIED`**; kept while
   `DRAFT`/`FAILED` for debugging.
7. **Notes lint warns, never blocks** on currency-sized numbers (payroll's soft-gate posture).
8. **Salary certificate** `input_data` is encrypted; the generated PDF stays on disk because
   the public verify URL must serve it.

## 4. Design

### 4.1 Cryptography

- **Library:** `cryptography` (already in the venv as a transitive dependency of
  `mozilla-django-oidc`/`josepy` — pin it explicitly in `requirements.txt`). No new
  framework; a 3rd-party "django-encrypted-fields" package would add a dependency for ~80
  lines of code we need to own anyway.
- **Primitive:** Fernet (AES-128-CBC + HMAC-SHA256, random IV per write). Randomised
  encryption is correct here because no code needs equality lookups on amounts; the ciphertext
  for the same salary differs row to row, so the database can't even tell who earns the same.
- **Rotation:** `MultiFernet` over an ordered key list. New writes use the first key; reads try
  each. `manage.py rotate_payroll_keys` re-saves every encrypted row under the newest key.
- **Key source:** `PAYROLL_FIELD_KEYS` in `.env` (comma-separated Fernet keys, newest first),
  read via `os.getenv` like every other setting. `settings.py` raises `ImproperlyConfigured`
  when it is missing, exactly as it does for `SECRET_KEY`. `.env.example` ships a
  *dev-only* key with a comment; `manage.py generate_payroll_key` prints a fresh one.
  Both gunicorn and the Celery worker read the same `.env`, so the engine works unchanged.
- **Key separation:** deliberately not derived from `SECRET_KEY`, so rotating either one
  does not force the other, and a leaked `SECRET_KEY` (sessions/CSRF) does not expose pay.

### 4.2 Field types — `core/fields.py`

```
EncryptedTextMixin        get_prep_value → encrypt(str)   from_db_value → decrypt
EncryptedCharField        (mixin + TextField)  for account/routing numbers
EncryptedDecimalField     (mixin + TextField)  max_digits/decimal_places kept for validation;
                          stores str(Decimal) quantised; returns Decimal
EncryptedJSONField        (mixin + TextField)  json.dumps/loads around the cipher
```

- DB column type is `TEXT` (a Fernet token for a 12-digit decimal is ~120 bytes).
- **Lookups other than `isnull` raise `TypeError`.** A stray `.filter(net_pay__gt=0)` or
  `Sum("amount")` must fail loudly in tests, not silently compare ciphertext.
- `EmployeeBankAccount`'s unique constraint must be checked at build time: if it includes
  `account_number`, add a blind index column (`account_number_hash`, HMAC-SHA256 under the
  same key material) and move the constraint onto it. Nothing else in the codebase does an
  equality lookup on a money/bank column.
- Form fields are unaffected: the model field's `formfield()` still returns `DecimalField`/
  `CharField`, so `StyledMixin`, `use_date_picker`, uploaders and the admin keep working.

### 4.3 Encryption scope

| Encrypt | Reason |
|---|---|
| `SalaryStructure.gross_monthly`, `TeamSalaryPackage.gross_monthly` | compensation master |
| `PayrollRunEmployee` six money columns + `bank_account_number` | register |
| `PayrollRunLine.amount`, `full_amount`, `rate` | register detail (`rate` × `quantity` reveals pay) |
| `PayrollRun` five totals | company-wide totals; cheap and consistent |
| `PayrollVariance` four columns | net-pay deltas |
| `TaxComputation` ten columns + `breakdown_json` | tax |
| `EmployeeTaxDeclaration` eight columns | personal investments |
| `VariablePayEntry.amount`, `TDSEntry.amount` | inputs |
| `EmployeeBankAccount.account_number`, `routing_number` | financial PII |
| `documents.DocumentRequest.input_data` | salary certificate figures (one field, every document type — simpler than special-casing) |

| Stay plaintext | Reason |
|---|---|
| `PayRate.*`, `MealDeductionTier.*` | rate cards; SQL band lookups in `payroll/rates.py` / engine |
| `TaxYear`, `TaxSlab`, `TaxExemptionThreshold` | public law |
| `OvertimeEntry.seconds`, `MealCount.total_meals`, every `*_count` | quantities, not money |
| `PayrollRunEmployee.proration_factor`, `payable_days`, … | attendance arithmetic |

### 4.4 Moving money arithmetic out of SQL

Every SQL operation on an encrypted column moves to Python. A run holds a few hundred rows
and a few thousand lines, so this is microseconds, not a performance question.

- `payroll/views.py` register (`PayrollRunDetailView`, ~L495–L585):
  - `filter(gross_earnings__gt=0)` → new persisted `PayrollRunEmployee.has_pay` boolean, set
    by `calculate_run` (the engine already knows which rows carry pay). Same for the
    `page_subtitle` count.
  - `order_by("-net_pay")` → load the filtered rows, `sorted(..., key=net_pay, reverse=True)`,
    then `core.pagination.paginate_list(request, rows)` (already used by other list views).
  - `aggregate(Sum(...))` rollups → `sum()` over the same list.
- `_deduction_breakdown_totals` / `_earning_breakdown` (`payroll/views.py` ~L359–L443,
  `payroll/exports.py::_earning_breakdown`) → one `PayrollRunLine` fetch for the run, grouped
  in Python with `itertools.groupby`/`defaultdict`.
- `payroll/engine.py` already does its arithmetic in memory (`package.gross_monthly !=
  structure.gross_monthly`, variance calc); no change beyond `has_pay`.
- `accounts/upload.py` reads `salary_structures` in Python; no change.

### 4.5 Purge instead of backfill (guarded)

Each conversion migration is `RunPython(purge) → AlterField(...)`. A `DECIMAL → TEXT`
`AlterField` on MySQL would otherwise *cast* existing values, leaving plaintext strings in the
"encrypted" column. The purge:

- deletes every row of the models converted in that migration (children first), printing
  counts;
- **refuses to run when any converted table is non-empty unless
  `PAYROLL_PURGE_ON_MIGRATE=1` is set in the environment**, with a message naming the tables.
  That flag is set once, by hand, for the one-time conversion on dev and prod, and never
  written into `.env`. A future engineer re-running migrations against real data hits the
  refusal, not a wipe.

No new tables are created, so the MySQL collation `CONVERT` step (CLAUDE.md) does not apply
here — except to `PayrollAccessLog` in Phase 3, which has a `CustomUser` FK and needs it.

### 4.6 Django admin

A `PayrollMoneyAdminMixin` on every admin class in §2.3's last row:

- `MONEY_FIELDS` tuple per admin;
- `get_list_display`, `get_fields`/`get_fieldsets`, `get_readonly_fields`, `get_search_fields`
  drop those names unless `can_see_payroll_money(request.user)` (which already returns `True`
  for superusers);
- inline admins (`PayrollRunLine` on `PayrollRunEmployeeAdmin`, if present) get the same
  treatment;
- `PayRateAdmin`'s `django-import-export` export is untouched (rate card, plaintext by decision).

With `__str__` no longer containing amounts, `django_admin_log.object_repr` stops leaking from
the first deploy; the historical rows are scrubbed once in Phase 0.

### 4.7 Permissions

- `accounts/permissions.py`: `can_see_payroll_money` unchanged. New `can_view_salary_history`
  (HR group or superuser) used by the Service Book and the onboarding uploader; new
  `HRRequiredMixin` in `accounts/views.py` beside `StaffRequiredMixin`.
- `employees/views.py::_can_view_service_book` → self or `can_view_salary_history`.
- `accounts/upload_views.py` `OnboardUploadView` / `OnboardUploadTemplateView` /
  `OnboardResultsView` → `HRRequiredMixin`.
- `accounts/management/commands/seed_orbis.py`: `MENU_GROUPS["payroll_salary"] = ["Payroll Admins"]`;
  `MENU_GROUPS["admins"]`-gated **Users** main menu gets `"HR"` added. Prod needs
  `manage.py seed_orbis --menus-only` after deploy (the bare command is broken — see
  `deploy/README.md`). Update the "Finance only" comments in `seed_orbis.py`,
  `payroll/views.py::SalaryStructureListView` and `docs/PLAN.md` in the same change.

## 5. Side channels — the concrete edits

- `payroll/engine.py`: `SALARY_PACKAGE_MISMATCH` message → "Paid gross differs from the
  {package} package." (Payroll Admins see both figures on the register row and in admin.)
  Negative-net message → "Net pay is negative." `PayrollRunAudit.detail_json` drops
  `net_total`.
- `payroll/models.py`: every `__str__` in §2.2 loses its amount (`"{employee} · rev {revision_no} from {effective_from}"` style).
- `payroll/upload_views.py`: after a batch flips to `APPLIED`, `batch.uploaded_file.delete(save=True)`.
  One-off: delete everything under `media/payroll/batches/` on dev and prod.
- `payroll/notes_lint.py`: new `CURRENCY_FIGURE` warning for a note containing a number ≥ 1,000
  (optionally with `,`/`৳`/`BDT`), surfaced the same way existing lint findings are.
- `payroll/management/commands/*`: rule, enforced by a test that greps `self.stdout.write`
  f-strings for money field names — commands never print an amount, because
  `scheduler/monitoring.py` persists their stdout.

## 6. Verification

Tests to add (mirroring the existing `payroll/tests*.py` layout):

1. `core/tests.py`: round-trip for all three field types; ciphertext differs across two saves
   of the same value; `MultiFernet` reads a value written under an older key;
   `.filter(gross_monthly__gt=…)` raises `TypeError`; `rotate_payroll_keys` leaves values
   readable under the new key only.
2. **Raw-SQL assertion:** after saving a `SalaryStructure` with gross `12345.67`, a
   `connection.cursor()` `SELECT gross_monthly FROM payroll_salarystructure` contains neither
   `12345` nor `.67`. Same for one `PayrollRunEmployee` after `calculate_run`.
3. Existing engine tests (`tests_engine.py`, `tests_tax.py`, `tests_variable_pay.py`,
   `tests_test_salary.py`, `tests_team_package.py`) pass unchanged — they assign and compare
   through the ORM, which is transparent. A grep found **zero** tests filtering or aggregating
   on a money column in SQL.
4. Register view: sort order and filtered totals match a Python-computed expectation;
   `has_pay` excludes zero-gross rows.
5. `PayrollControlCheck.message` and every money model's `str()` contain no digit sequence of
   length ≥ 4 for a seeded run.
6. Admin: a staff user outside Payroll Admins loads each money changelist/changeform with the
   money columns absent; a Payroll Admin sees them.
7. Permissions: User Admin → 403 on the Service Book and on `/users/bulk-create/`; HR → 200
   on both and sees the Users menu; Finance → 403 on `/payroll/salary/`; Payroll Admin → 200.
   `seed_orbis` ACL assertions updated.
8. Batch upload: `uploaded_file` is gone after apply.
9. Migrations: the purge refuses on a non-empty table without the env flag, and runs with it.

Manual, before deploy (per the "test locally before deploy" convention): run the full
conversion on a dev DB seeded with fake salaries; open the register, gross salary list,
Service Book, admin and a salary certificate; confirm `mysql -e "SELECT …"` shows tokens.

## 7. Delivery

### Phase 0 — contain (no code, this week)

1. **Split MySQL accounts.** Keep `orbis_user` for the app. Create a developer account granted
   `SELECT` per table on everything *except* the §2.1 tables, `django_admin_log` and
   `documents_documentrequest` (MySQL has no "revoke a subset of a DB-wide grant", so the
   grant list is generated from `information_schema.tables`; script goes in `deploy/README.md`).
   Rotate the app password afterwards.
2. Delete `media/payroll/batches/*` on dev and prod.
3. **nginx: deny `/media/payroll/` and `/media/documents/` outright** — nothing links to them;
   every legitimate read already streams through a Django view. Add to the live config and
   to `deploy/README.md`'s "nginx /media/" section:

   ```nginx
   location /media/payroll/   { deny all; }   # batch workbooks + attachments: app views only
   location /media/documents/ { deny all; }   # letter PDFs: DownloadPdfView / public verify only
   location /media/           { alias /home/ubuntu/orbis/media/; }
   ```

   `offboarding/` attachments are linked directly from `case_detail.html` (`a.file.url`), so
   that directory stays served until it gets its own permission-checked view — out of this
   plan's scope, but the same fix.
4. Scrub `django_admin_log.object_repr` for the money content types.
5. Write down the backup policy in `deploy/README.md` — none exists today, and a plaintext dump
   defeats every later phase. Dumps taken before Phase 1 ships must be treated as sensitive.
6. Audit `is_staff` / `is_superuser` holders.

**Phase 0 progress, 2026-10-05:**

- Done in-repo: `deploy/mysql-dev-grants.sql` (step 1, generated from the model registry —
  127 tables granted, 18 withheld, `accounts_customuser` minus `password`), the dev copies of
  `media/payroll/batches/*` deleted (step 2), nginx deny block written into `deploy/README.md`
  (step 3), `deploy/mysql-backup.sh` + the "MySQL backups" README section (step 5).
- Applied on the VPS 2026-10-05: `orbis_dev@localhost` created from the grants file; nginx
  deny block live (`/media/payroll/batches/x.xlsx` → 403 from outside, profile pictures still
  served); `age` installed. Open question: developers' actual connection path — if they reach
  MySQL over the LAN the account's host part must be their address, not `localhost`.
- Still to run on the VPS, by hand: rotate `orbis_user`'s password, set up the new backup
  script (`orbis_backup` user, `backup.conf`, offline age key pair, crontab), delete
  `media/payroll/batches/*` on prod (30+ files found, mostly overtime templates), delete the
  unencrypted dumps in the Drive folder, one restore drill.
- Step 4 (admin-log scrub) is deferred to the Phase 1 deploy: scrubbing now would be undone by
  the next admin edit, since `__str__` still embeds amounts until the new code is live (the
  `__str__` change ships with Phase 1 — see its progress notes). The statement is in
  `deploy/README.md` "Payroll field encryption".
- **Step 6 finding:** production has **four superusers**, and superusers bypass every gate
  including `can_see_payroll_money`. Two of them are also in "Payroll Admins" — the assumption
  that exactly one person holds both roles is wrong — and the other two carry Finance + HR +
  User Admins. Decide which of the four genuinely need superuser; the admin-visibility rule in
  §3 only means something once superuser is limited to people allowed to see pay.
- Confirmed: production `.env` is `-rw------- ubuntu`, so only the app user and root can read
  the key. The backup script found at `/home/ubuntu/mysql_backup/mysql-backup.sh` embedded the
  `orbis_user` password and uploaded plaintext dumps to Google Drive — see `deploy/README.md`
  "MySQL backups" for the state found and the replacement.

### Phase 1 — field-level encryption

1. `core/crypto.py` (`get_fernet()`, cached), `core/fields.py`, settings + `.env.example`,
   `manage.py generate_payroll_key`, `manage.py rotate_payroll_keys`.
2. Model conversions + guarded purge migrations, one migration per group, in order:
   `SalaryStructure` + `TeamSalaryPackage` → `PayrollRun` + `PayrollRunEmployee` (+ `has_pay`)
   + `PayrollRunLine` + `PayrollVariance` → `TaxComputation` + `EmployeeTaxDeclaration` →
   `VariablePayEntry` + `TDSEntry` → `EmployeeBankAccount` → `documents.DocumentRequest.input_data`.
3. §4.4 Python relocation in `payroll/views.py` and `payroll/exports.py`.
4. §4.7 permission + menu changes, §4.6 admin mixin.
5. Tests from §6. Short maintenance window on prod: set `PAYROLL_FIELD_KEYS`, export
   `PAYROLL_PURGE_ON_MIGRATE=1` for the migrate step only, `seed_orbis --menus-only`, restart
   gunicorn + Celery (both read the new key). Step-by-step in `deploy/README.md`
   "Payroll field encryption".

**Phase 1 progress, 2026-10-05 — built, 1,269-test suite green apart from nine pre-existing
failures (seven `tests_team_package` SQLite partial-unique errors, two roster query-budget
tests), migrations replayed on a fresh MySQL test database:**

- `core/crypto.py` (`get_fernet`, `encrypt`, `decrypt`, `DecryptionError`), `core/fields.py`
  (`EncryptedDecimalField`/`EncryptedCharField`/`EncryptedJSONField` subclassing their plain
  counterparts, so forms, validation and admin display are unchanged; column type `TEXT`),
  `core/migration_utils.py::guarded_purge`, `core/admin_mixins.py::PayrollMoneyAdminMixin`,
  `manage.py generate_payroll_key` / `rotate_payroll_keys` (in `payroll/management/commands/`
  — `core` is not an installed app), settings + `.env.example` + `core/settings_test.py`,
  `cryptography==49.0.0` pinned.
- Migrations `payroll/0021_encrypt_salary_structure` → `0025_encrypt_bank_account` (each a
  guarded purge then the `AlterField`s) and `documents/0006_encrypt_documentrequest_input_data`.
  `0022` purges every run-derived table including `PayrollRunAudit` (its `detail_json`
  carried `net_total`).
- §4.4 relocation done in `payroll/views.py` (`_line_totals` groups `PayrollRunLine` amounts in
  Python; `calculated_rows` is sorted/summed in Python; `has_pay` replaces `gross_earnings > 0`)
  and `payroll/exports.py`; `payroll/services.py::propose_salary_for_assignment` compares the
  pending proposal's gross in Python.
- §4.7 permissions: `can_view_salary_history`, `HRRequiredMixin`, Service Book + onboarding
  gates, `seed_orbis` ACLs (`payroll_salary` → Payroll Admins, Users menu + HR).
- `core/tests_encryption.py` covers §6 items 1, 2, 4, 5, 6, 7, 9 plus the §8 registry guard;
  item 3 holds (existing engine/tax/variable-pay/team-package suites unchanged except two
  tests that queried a money column in SQL); item 8 (batch file deletion) is Phase 2.

Three deviations from the text above, all deliberate:

1. **`documents.DocumentRequest.input_data` is backfilled, not purged.** Letter requests are
   real HR records (NOCs, experience certificates), not test money, so `documents/0006`
   alters the column and then re-writes every existing JSON value as a token via raw SQL.
   Re-run safe.
2. **The no-amount `__str__` change (§5) shipped with Phase 1**, because Django's changelist
   puts `str(obj)` into the action-checkbox label for every row — the admin mixin alone left
   the gross visible to a non-payroll staff user. `PayRate`/`MealDeductionTier.__str__` keep
   their figures (rate cards, plaintext by decision). The admin-log scrub (Phase 0 step 4)
   can therefore happen at this deploy.
3. **Not every SQL misuse can be refused at runtime.** Lookups raise `TypeError` as planned;
   `order_by()` and `F()` are refused when the ordering resolves (`_EncryptedCol`); `Sum`/
   `Max`/`Min` fail loudly when the database hands back a number instead of a token. `Avg`
   would not (its output field is a plain `DecimalField`), so
   `RegistryGuardTests.test_no_sql_ordering_or_aggregate_on_an_encrypted_column` scans the
   source of `payroll`, `documents`, `accounts`, `employees` and `core` for any of these on an
   encrypted column name. `isnull` is implemented on a plain column reference so it still works.

### Phase 2 — close the side channels

§5 in full: control-check messages, audit `detail_json`, `__str__`s, batch-file deletion,
notes lint, the command-output rule. Ships with or immediately after Phase 1.

### Phase 3 — visibility and defence in depth (optional, sequenced after 1–2)

1. ~~`PayrollAccessLog` — who viewed/exported money, when.~~ **Superseded 2026-10-09 by the
   application-wide access log** (`audit.AccessLog`, `docs/ACCESS_LOG_PLAN.md`): salary, tax
   and loan reads/exports — including through the Django admin — are its `SALARY`/`TAX`/`LOAN`
   data classes, so no payroll-only table is needed.
2. MySQL TDE (`keyring_file`, `ENCRYPTION='Y'` on the `payroll_*` tables) + encrypted backups.
3. Purge the historical SQL dump from git history (`deploy/README.md` already recommends it).

## 8. Risks

1. **Key loss = total loss.** There is no recovery without `PAYROLL_FIELD_KEYS`. The offline
   copy is created *before* the first production migrate, and `update_orbis.sh` must never
   regenerate `.env`.
2. **Silent plaintext regressions.** A future `DecimalField` added to a payroll model lands
   plaintext by default. Mitigation: a test that walks `payroll` models and fails on any
   `DecimalField` not in an explicit allow-list (the rate-card exception).
3. **Lookups creeping back.** The `TypeError` on non-`isnull` lookups is the guard; a
   developer who hits it in a test knows to move the work to Python.
4. **Admin bypass via raw `queryset.values()`** — not blocked by the mixin, but nothing in the
   admin does it; covered by code review.
5. **The purge flag misused later.** Mitigated by the refusal message and by the flag never
   living in `.env`.
6. **Developer shell access later** (§1). Reassess key storage if that changes.

## 9. Open items

- ~~Confirm nobody besides the product owner and the deploy user can read production `.env`.~~
  Confirmed 2026-10-05: mode 0600, owner `ubuntu`.
- ~~Reduce the four production superusers to the people allowed to see pay (Phase 0 step 6).~~
  Done 2026-10-05: two superusers remain, the product owner and the CTO — both key holders,
  so the superuser bypass of `can_see_payroll_money` exposes nothing they don't already hold.
- ~~Decide whether the offline key copy is a password manager entry or a sealed physical copy.~~
  Resolved 2026-10-05: printed copy held by the CTO (§3 item 5).
- **What a credential holder sees after Phase 1, confirmed 2026-10-05:** a `SELECT` on any
  encrypted column from the MySQL CLI — with the developer account, `orbis_user`, or root —
  returns a Fernet token (`gAAAAA…`), never a number; decryption happens only in Python with
  the key from `.env`. Human-readable figures exist only on application pages gated by §3's
  matrix. Still plaintext by design: rate cards/tax tables (§4.3), generated PDFs on disk
  under `media/documents/` (reachable only through `DownloadPdfView`, the public verify UUID
  link, or — until Phase 0's nginx deny rule lands — a direct `/media/` URL), and any figure a
  person types into a free-text note (lint warns, never blocks).
- CTO's printed copy of `PAYROLL_FIELD_KEYS` confirmed in hand 2026-10-05 (§3 item 5).
- **Phase 2 not started** (deferred 2026-10-05 for other features). Everything it needs is §5 +
  §7 "Phase 2"; the `__str__` item is already done. Phase 3 timing is open; Phases 0–2 are
  the commitment.
