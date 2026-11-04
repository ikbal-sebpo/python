# Media files on Google Drive — plan

**Status:** accepted 2026-10-10 (all five decisions in §6 closed). **Phase 0 done for dev
(`srv_meals`) and Phase 1 built and verified the same day** — a browser upload
of a profile picture with `MEDIA_BACKEND=drive` on a dev box showed up in the
`srv_meals` Drive as one opaque `.bin`. See "As built" at the end for the
deviations from the text below. **Production: done 2026-10-16** (slipped from
the planned 10-13 until IT handed over `srv_orbis@sebpo.com`) — Phase 0 as
`srv_orbis`, Phase 2 migration + flip in one sitting, now on
`MEDIA_BACKEND=drive`. Only step 2.5 (delete `~/orbis/media` + the tarball on
**2026-10-30**) remains. See "Production rollout (2026-10-16)" at the end.
**Why:** media (uploads + generated PDFs) must leave the VPS. IT cannot buy S3 or
Google Cloud Storage under current policy, but the sebpo.com Google Workspace tier
has ample Google Drive storage. This plan makes Google Drive the storage backend
for every `FileField`/`ImageField` in Orbis.

---

## 1. What we have today

Thirteen file columns across seven apps, all on `FileSystemStorage` under
`MEDIA_ROOT = BASE_DIR / "media"` (`core/settings.py` `STORAGES["default"]`):

| App | Column | `upload_to` | How it is read today |
|---|---|---|---|
| accounts | `CustomUser.profile_picture` | `profile_pictures/` | `.url` in topbar, employee list, employee detail (every page view) |
| accounts | `CustomUser.signature` | `signatures/` | server-side only, letter PDFs via `documents/pdf.py::_field_to_data_uri` (**uses `.path`**) |
| employees | `Image.image` | `employees/` | `.url` in profile section; Service Book PDF via `_field_to_data_uri` |
| employees | `LegalIssue.case_document` | `legal/` | `.url` in `_legal.html` |
| leave | `LeaveApplication.attachment` | `leave/` | `.url` in `leave/detail.html` |
| offboarding | `StepAttachment.file` | `offboarding/step_attachments/` | `.url` in `case_detail.html` |
| documents | `Branding.logo` | `documents/branding/` | `_field_to_data_uri` (**`.path`**) |
| documents | `DocumentRequest.generated_file` | `documents/generated/` | streamed by `DownloadPdfView` + public verify view via `.open("rb")` |
| policies | `PolicyVersion.pdf` | `policies/<policy>/v<n>.pdf` | streamed by `policies:pdf` via `.open("rb")` (pdf.js canvas) |
| payroll | `PayrollInputBatch.uploaded_file` | `payroll/batches/` | never served (audit copy) |
| payroll | `VariablePayEntry.attachment` | `payroll/variable_pay/` | never served |
| payroll | `EmployeeTaxDeclaration.ait_document` | `payroll/tax_declarations/<emp>/<uuid>` | streamed by `payroll_tax_declaration_document` via `.open("rb")` |
| payroll | `SalaryLoan.approval_document` | `payroll/salary_loans/<emp>/<uuid>` | streamed by `payroll_salary_loan_document` via `.open("rb")` |

Plus CKEditor 5 image uploads for letter templates (`django_ckeditor_5`, staff
only) which also land in the default storage.

Two facts shape the design:

1. **Five views already stream through `.open("rb")`** — those work unchanged on
   any storage backend. **Six templates link through `.url`** — those keep working
   if the new storage's `url()` returns an app URL. Only `_field_to_data_uri` and
   `_absolute_file_data_uri` in `documents/pdf.py` touch `.path`, which a
   non-filesystem storage does not have.
2. **Production nginx serves `/media/` to anyone, unauthenticated** — the open
   finding from 2026-08-29 in `deploy/README.md` ("nginx /media/ — NOT APPLIED").
   Moving media off the box means every file must be served by Django anyway, so
   this plan closes that finding as a side effect.

Local dev `media/` is 5.5 MB; the production size is unmeasured (step 0.1).

---

## 2. Google Drive constraints that drive the design

- **Which Drive the files live in — decided 2026-10-10: the My Drive of the
  `srv_orbis@sebpo.com` service mailbox** (production; the existing outbound-mail
  sender, `EMAIL_HOST_USER`), which only the Orbis maintainer can log into.
  **Development and testing use `srv_meals@sebpo.com`'s Drive** — a separate
  account, so a dev box can never write into or read the production folder; each
  environment carries its own credential file and `GOOGLE_DRIVE_ROOT_FOLDER_ID`
  in its own `.env`. Orbis authenticates *as that account* with an OAuth refresh token
  (the same mechanism the `gdrive` CLI backup uses), scope `drive.file`, so the
  app sees only files it created and nothing else in that mailbox's Drive. No
  Shared Drive, no folder shared with anyone; the files are visible in the Drive
  UI only to whoever can sign in as `srv_orbis`. The service-account alternative
  was dropped: service accounts have no Drive storage of their own since
  2025-04-15 and would have forced a Shared Drive, which more people can see.
- **Hard requirement (restated by the owner 2026-10-10): nobody may read a
  media file from the Drive UI, ever — not the `srv_orbis` account holder, not a
  Workspace super-admin. Orbis, with its own authentication and authorisation,
  is the only way to see or manage a file.** The account boundary cannot deliver
  that on its own: a super-admin can reset `srv_orbis`'s password, use Google
  Vault, or transfer its Drive to another user. So the design treats Drive as an
  **untrusted blob store** (§4.4): every byte is encrypted before it leaves the
  VPS, filenames and folder names are random, and the key never exists in Drive.
  Someone in the Drive UI sees a list of `<32 hex>.bin` files of type "binary",
  no previews, no thumbnails, no real names — and can learn nothing from them.
- **A free Google Cloud project is still needed**, for the OAuth client ID the
  consent flow uses. No billing account. The nightly MySQL backup
  (`deploy/mysql-backup.sh`) already runs `gdrive` v3, which needs exactly such
  a client, so one almost certainly exists — reuse it. The OAuth consent screen
  must be **User type: Internal** (available to Workspace orgs); an "External /
  Testing" app's refresh tokens expire after 7 days. Internal-app refresh tokens
  do not expire; they are revoked only if the account is suspended, the user
  revokes them, or the token goes unused for 6 months.
- **Drive is not a filesystem.** Files have IDs; names are not unique; "paths" are
  parent chains that cost one API call per segment to resolve. We therefore keep
  our own **name → file-ID index in MySQL** and never walk folders at request time.
- **Rate limits** are 12,000 queries/min per project — orders of magnitude above
  our load, but 403/429 and 5xx still happen and must be retried with backoff.
- **Drive-side sharing links are not an option** for serving: a `webContentLink`
  needs "anyone with the link", which is worse than today's open nginx alias.
  All reads go through Django.
- **What encryption does and does not give.** It makes every file *unreadable*
  outside Orbis. It cannot stop someone with the `srv_orbis` login or admin
  tooling from *deleting* or trashing blobs — availability is covered by Drive's
  30-day trash, the weekly `verify_media_drive` job (§7) and the index being in
  the MySQL backup. And the key (`MEDIA_ENCRYPTION_KEYS` in the VPS `.env`) is
  the whole secret: Drive access plus the key equals plaintext, so the key is
  handled exactly like `PAYROLL_FIELD_KEYS` and is never stored in Drive, the
  repo, or the backup folder.

---

## 3. Options considered

| Option | Verdict |
|---|---|
| **A. Custom Django storage backend over the Drive API + MySQL index + local read cache** | **Recommended.** ~400 lines, full control over permissions, caching, encryption, retries. Detailed below. |
| B. `django-googledrive-storage` (PyPI) | Resolves paths by folder-walking (several API calls per `exists()`/`save()`), service-account-on-My-Drive design that the 2025 quota change broke, Shared Drive support unconfirmed, low maintenance activity. Verify before dismissing, but expect to write our own. |
| C. `rclone mount` of the Drive folder at `MEDIA_ROOT` (FUSE, zero code) | Rejected. A FUSE stall blocks a gunicorn worker; Drive listing is eventually consistent; the mount dies on reboot/network blips; it keeps the open-nginx problem; and rclone's VFS cache is still a copy of the media on the VPS. |
| D. `rclone sync media/ → Drive` nightly (backup only) | Does **not** meet the ask (media stays on the VPS), but it is a one-hour stopgap worth running from day one while IT does step 0. Dropped once A is live. |
| E. `webContentLink` / public share URLs | Rejected — see §2. |

---

## 4. Design (option A)

### 4.1 New app: `media_store`

A small first-party app (needs a model and a migration, so not `core`):

```
media_store/
  storage.py        GoogleDriveStorage (django.core.files.storage.Storage)
  drive.py          thin Drive API client: credentials, upload, download, trash, list; retries
  cache.py          bounded on-disk read cache (MEDIA_CACHE_ROOT), write-through
  models.py         DriveObject — the name → file-id index
  views.py          MediaServeView (/media/<path:name>)
  rules.py          prefix → authorisation rule table (see 4.3)
  management/commands/
    migrate_media_to_drive.py   copy MEDIA_ROOT tree into Drive, fill the index (idempotent)
    verify_media_drive.py       every FileField value in the DB resolves in the index & on Drive
    rebuild_drive_index.py      regenerate DriveObject from Drive (decrypts appProperties.orbis_ref)
    prune_media_cache.py        LRU-trim the cache to MEDIA_CACHE_MAX_BYTES (nightly beat job)
    rotate_media_keys.py        re-encrypt every blob under the newest key (Phase 3, like rotate_payroll_keys)
    media_drive_authorize.py    one-time OAuth consent flow as srv_orbis → writes the refresh-token file
    media_drive_probe.py        connectivity/credential check, like timely_probe (also reports Drive quota used)
```

New dependencies: none as built (see "As built": the Drive client is
`requests`-only). **This is still a plan-drift event** (new app + new pattern) —
`docs/PLAN.md` has the "Media storage" section.

### 4.2 `DriveObject` index

```
name          varchar(500) unique   # the FileField value, e.g. "profile_pictures/1234_Jane.jpg" — exists ONLY here, never in Drive
file_id       varchar(64)           # Drive file id
drive_name    char(36)              # the random "<32 hex>.bin" the blob is stored under
size          bigint                # plaintext size (what Django's size()/Content-Length report)
md5           char(32)              # Drive's md5Checksum of the *ciphertext*, for verify_media_drive
mime          varchar(120)          # real content type, served by Orbis; Drive only ever sees application/octet-stream
key_id        smallint              # which MEDIA_ENCRYPTION_KEYS entry encrypted it (rotation bookkeeping)
created_at    datetime
```

No `CustomUser` FK (deliberately — avoids the collation CONVERT step and keeps
the table a pure index). `exists()`, `size()`, `url()` and the serve view's
authorisation read only this table: **zero Drive calls on a page render unless a
file is actually opened and is not in the cache.**

Each Drive file carries `appProperties = {"orbis_ref": <Fernet token of
"name|mime|size">}` so `rebuild_drive_index` can regenerate the table from Drive
alone (disaster recovery when a DB restore predates recent uploads) — but only
with the key. Without it the property is as opaque as the blob. Nothing
human-readable about the file exists on Drive: not its name, not its folder, not
its type, not its content.

### 4.3 Storage semantics

- `_save(name, content)` — encrypt the bytes (§4.4), then resumable-upload the
  ciphertext into the `Orbis Media` root folder of the `srv_orbis` My Drive
  (`GOOGLE_DRIVE_ROOT_FOLDER_ID`) as `<uuid4 hex>.bin`,
  `mimeType=application/octet-stream`. **No per-prefix folders**: a folder
  called `payroll` would itself leak. The only sub-structure is two levels of
  shard folders named by the first hex characters of the random name
  (`3f/9a/3f9a….bin`), purely to keep Drive listings fast — they carry no
  meaning. Insert `DriveObject`. Also write the *ciphertext* to the local cache
  (write-through) so the file is servable immediately.
  Upload is **synchronous in the request** (0.5–2 s for our file sizes; the only
  large uploads are policy PDFs and payroll workbooks by staff). The
  alternative — write locally, upload later — reintroduces a VPS copy and a
  window where `open()` must know about two places, so it is rejected.
- `_open(name)` — cache hit → local ciphertext file; miss → `files().get_media(
  file_id)` streamed into the cache. Decrypt in memory and return a
  `django.core.files.File` over the plaintext. **Plaintext exists only inside
  the Django/Celery process that passed authorisation**; the cache on the VPS
  holds ciphertext, so the VPS disk is no more exposed than Drive is.
- `delete(name)` — move to Drive trash (`trashed=True`), never a hard delete,
  then remove the index row. Trash keeps the file 30 days — an undo window.
- `exists`/`size` — index only. `url(name)` → `/media/<name>` via `reverse()`.
  `path()` → `NotImplementedError` (Django's default), which is why 4.6 fixes
  `documents/pdf.py`.
- `get_available_name` keeps Django's default (suffix on collision via `exists()`).
- All Drive calls: `num_retries=5` exponential backoff on 403 rate-limit, 429, 5xx.
  A hard failure on save surfaces as a form error ("file storage unavailable,
  try again") and an `orbis.log` ERROR; a failure on read → 503 from the serve view.

### 4.4 Encryption: everything, always

**Every file is encrypted before upload; there is no plaintext prefix list.**
Decided 2026-10-10 after the owner restated the requirement (§2). Profile
pictures and company-wide policy PDFs are not secret in themselves, but one rule
with no exceptions is what makes "nothing in Drive is readable" a property you
can check rather than a list you have to maintain.

- Cipher: Fernet (AES-128-CBC + HMAC, same primitive as `core/fields.py`),
  whole-file, via `core/crypto.py`. Our files are small (letters < 1 MB, policy
  PDFs and payroll workbooks a few MB), so in-memory encryption is fine; a
  `MEDIA_MAX_UPLOAD_BYTES` guard (default 50 MiB) rejects anything that would
  not be.
- Key: a **separate** `MEDIA_ENCRYPTION_KEYS` in `.env`, same multi-key format
  and loader as `PAYROLL_FIELD_KEYS` (first key encrypts, the rest decrypt-only).
  Separate because rotating the payroll key must not force a re-upload of every
  media blob, and vice versa. Required at settings load only when
  `MEDIA_BACKEND=drive` (revised 2026-10-11 — the code shipped to production
  on the filesystem backend first, and a plain deploy must not depend on a
  secret it doesn't use; `media_store.crypto` raises on first use otherwise); `.env.example` ships a throwaway dev key; `settings_test.py` pins its
  own. Generated with the existing `manage.py generate_payroll_key`.
- Metadata: real name, MIME type and plaintext size live in `DriveObject` and,
  for recovery, encrypted inside `appProperties.orbis_ref` (§4.2). Drive sees
  `application/octet-stream`, a random name, and the ciphertext length.
- Rotation: `manage.py rotate_media_keys` downloads, re-encrypts under the first
  key and re-uploads each blob whose `key_id` is stale, resumable, idempotent.
  Phase 3.
- Dev/test: the same code path runs on the `FileSystemStorage` fallback? **No** —
  `MEDIA_BACKEND=filesystem` stays plain files on disk exactly as today, so
  existing tests and `override_settings(MEDIA_ROOT=…)` fixtures are untouched.
  Encryption is a property of the Drive backend, which is the only place bytes
  leave the VPS. The `FakeDriveClient` tests (§4.7) assert the fake receives
  only valid Fernet tokens.

What someone with Drive UI access can and cannot do (§2 restates the limits):
read a file — **no**; learn what a file is — **no**; see how many files and how
large — yes; trash or delete a file — yes (recoverable 30 days from trash; the
weekly verify job reports anything missing). The VPS `.env` key is the one
secret that turns Drive access into readable files, and it never leaves the
server.

### 4.5 Serving: `MediaServeView` at `/media/<path:name>`

Login required (the public letter-verify page keeps its own view). Authorisation
by top-level prefix, resolved against the owning row — the same checks the
owning page already applies:

| Prefix | Rule |
|---|---|
| `profile_pictures/`, `employees/`, `documents/branding/`, CKEditor uploads | any authenticated user |
| `signatures/` | owner, or `can_see_all` |
| `legal/` | `LegalIssue` by `case_document=name` → `can_view_employee(user, issue.employee)` |
| `leave/` | `LeaveApplication` by `attachment=name` → same rule `LeaveDetailView.get_object` applies |
| `offboarding/` | `StepAttachment` by `file=name` → `offboarding.can_view_case` |
| `payroll/`, `documents/generated/`, `policies/` | **404 here, always.** The dedicated permission-checked views stay the only path. |

Responses: `FileResponse` over the decrypted stream (§4.3), `ETag` = index md5,
`Cache-Control: private, max-age=86400`, correct `Content-Type`, 304 on
`If-None-Match`. Every hit calls `audit.capture.note()`; `audit/registry.py` gets
a `media_serve` rule classifying by prefix (PROFILE / LEAVE / OFFBOARDING /
DOCUMENT) and `/media/` leaves `EXCLUDED_PATH_PREFIXES`.

Because every byte is decrypted in Python after the authorisation check, there
is no `X-Accel-Redirect`/sendfile shortcut and nginx never sees a media file.
That is the price of the §2 requirement and it is small: our files are small,
the cache makes the hot set a local read, and browsers hold a day's `ETag`.

### 4.6 Code changes outside the new app

1. `documents/pdf.py::_field_to_data_uri` — read via `file_field.open("rb")`,
   guess MIME from `file_field.name`; `_absolute_file_data_uri` stays (it is for
   static fallbacks). This also fixes the Service Book photo path.
2. `core/settings.py` — `MEDIA_BACKEND=filesystem|drive` env switch selecting
   `STORAGES["default"]`; `MEDIA_CACHE_ROOT`, `MEDIA_CACHE_MAX_BYTES` (default
   2 GiB), `GOOGLE_DRIVE_CREDENTIALS_FILE` (OAuth client id/secret + refresh
   token, written once by `manage.py media_drive_authorize`),
   `GOOGLE_DRIVE_ROOT_FOLDER_ID`, `MEDIA_ENCRYPTION_KEYS`, `MEDIA_MAX_UPLOAD_BYTES`. Dev and `settings_test.py` stay on filesystem, so
   every existing `override_settings(MEDIA_ROOT=…)` test is untouched.
3. `core/urls.py` — `/media/<path:name>` → `MediaServeView` in all modes (replaces
   the DEBUG-only `static()` line; dev then exercises the same authorisation).
4. `payroll/models.py` docstrings on `tax_declaration_document_path` /
   `salary_loan_document_path` and `deploy/README.md` "nginx /media/" — rewrite
   now that nothing under `/media/` is served without a session.
5. `scheduler/schedule.py::JOBS` — add `prune_media_cache` (03:30, after the
   access-log prune) and run `seed_beat_schedule`.
6. `deploy/README.md` — Drive folder + OAuth setup, credential file placement (`0600`, outside
   the repo), nginx change, migration runbook, restore procedure.

### 4.7 Tests

- A `FakeDriveClient` (in-memory dict) injected into `GoogleDriveStorage` so the
  storage, cache, encryption and index are tested without network: save/open
  round-trip, every blob handed to the fake is a valid Fernet token and no
  real name/MIME/prefix string appears anywhere in the fake's metadata,
  `orbis_ref` round-trip, trash-on-delete, collision suffixing,
  cache miss → fill → hit, cache prune ordering.
- `MediaServeView`: each prefix rule's allow and deny case; the three denied
  prefixes 404 even for superusers; ETag/304; `audit` row written.
- `_field_to_data_uri` on a storage with no `path()`.
- `migrate_media_to_drive` idempotency (second run uploads nothing).
- Registry guard test continues to fail on an unclassified route.

---

## 5. Rollout

### Phase 0 — IT and prerequisites (no code; can start now)

0.1 Measure production media: `ssh orbis-prod` → `du -sh ~/orbis/media && find
    ~/orbis/media -type f | wc -l` (VPN required). Confirms Drive quota headroom
    and the migration window.
0.2 **Do not create the folder by hand.** Under the `drive.file` scope the app
    sees only files and folders it created itself, so a folder made in the Drive
    UI is invisible to it (404 when used as a parent). The root folder
    **"Orbis Media"** is created *by the app* in step 0.4 (`media_drive_authorize
    --mkroot`), once in `srv_orbis@sebpo.com`'s My Drive (production) and once in
    `srv_meals@sebpo.com`'s (dev/test); the command prints the folder id for
    `GOOGLE_DRIVE_ROOT_FOLDER_ID`. Nothing is shared with anyone. Confirm the
    account's Drive quota in Drive → Storage (the per-user figure for the
    Workspace tier; this account is the only consumer).
0.3 In the GCP project `gdrive` already uses for backups — or a new free one,
    no billing — enable the **Google Drive API**; set the OAuth consent screen
    to **Internal**; create an OAuth client of type **Desktop app**; download
    its JSON. No IT involvement beyond confirming a GCP project is allowed, and
    the backup's existing client proves it is.
0.4 On the VPS: place the client JSON at `/home/ubuntu/orbis-secrets/`, run
    `manage.py media_drive_authorize` — prints a URL, you open it **as
    `srv_orbis`** (as `srv_meals` on a dev box), approve the `drive.file`
    scope, paste the code back; the
    command writes `drive-credentials.json` (client id/secret + refresh token,
    `0600`, owner `ubuntu`), creates the "Orbis Media" root folder as the app
    (`--mkroot`, see 0.2) and verifies an upload/read-back/trash round-trip
    inside it. Set
    `GOOGLE_DRIVE_CREDENTIALS_FILE` and `GOOGLE_DRIVE_ROOT_FOLDER_ID` in prod
    `.env`. `manage.py media_drive_probe` → folder reachable, quota printed.
    Credential lifecycle: tokens die if `srv_orbis` is suspended or its password
    is reset by an admin — put the account on IT's "do not touch" list the same
    way the mailbox's SMTP App Password already is.
0.5 **Stopgap now:** nightly `rclone sync` (or the existing `gdrive`) of
    `~/orbis/media` to a separate Drive folder from the backup crontab, so media
    has an off-box copy today. Removed at the end of Phase 2.

### Phase 1 — code (estimate: 3–4 working days including tests)

Everything in §4, `MEDIA_BACKEND=filesystem` default, so merging and deploying is
behaviour-neutral until the flag flips. The serve view and the `.path` fix ship
and take effect immediately, which already closes the open-nginx finding once
the nginx alias is removed (step 2.4 can therefore be done at Phase 1 deploy).

### Phase 2 — migration (half a day, plus a maintenance window of minutes)

2.1 Deploy Phase 1. nginx: delete the `location /media/ { alias … }` block so
    `/media/` proxies to gunicorn like everything else; `nginx -t && reload`.
    Confirm from outside: an unauthenticated fetch of a known
    `/media/profile_pictures/…` URL now redirects to login.
2.2 `manage.py migrate_media_to_drive --dry-run`, then for real, **while the
    filesystem backend is still active** (so the site keeps working and the
    command is pure copy). Idempotent: re-run picks up files uploaded since.
2.3 `manage.py verify_media_drive` → zero missing. Re-run 2.2 once more right
    before the flip to catch the last uploads.
2.4 Flip `MEDIA_BACKEND=drive`, restart gunicorn + celery worker (standard
    `update_orbis.sh`). Smoke test: topbar avatar, employee list photos, open a
    letter PDF, open a policy PDF, download a tax-declaration document, upload
    a profile picture, generate a letter (Celery path), Service Book PDF.
2.5 Keep `~/orbis/media` for 14 days as the rollback (flip the flag back and
    restart). Then `rm -rf ~/orbis/media` and the Phase 0.5 stopgap job; the
    cache dir `~/orbis/media_cache` is the only media-related disk use left and
    is bounded by `MEDIA_CACHE_MAX_BYTES`.

Rollback at any point before 2.5: `MEDIA_BACKEND=filesystem`, restart. Files
uploaded while on Drive are then unreachable until `migrate_media_to_drive
--reverse` (pull Drive → disk, same index) — build this flag; it is 30 lines.

### Phase 3 — hardening (optional, after a quiet month)

- Admin read-only view of `DriveObject` with a "re-verify" action.
- `rotate_media_keys` (§4.4).

---

## 6. Decisions (all closed 2026-10-10)

1. **A bounded local read cache stays on the VPS** (≤ 2 GiB, disposable,
   rebuilt on demand). Without it every profile picture on every page is a Drive
   round-trip. **Decided 2026-10-10: yes** — "no media on the VPS" means no
   *authoritative* media; the cache holds ciphertext only (§4.3) and is
   disposable.
2. ~~Encrypt sensitive prefixes~~ **Decided 2026-10-10: encrypt every file,
   no exceptions** (§4.4), after the owner restated that nobody may read media
   from the Drive UI. Costs the nginx sendfile shortcut; buys a checkable
   property instead of a list.
3. ~~Shared Drive + service account~~ **Decided 2026-10-10: the `srv_orbis`
   mailbox's own My Drive via an OAuth refresh token.** Only that account's
   owner can even *list* the blobs in a browser, and with decision 2 nobody
   can read one. Residual: Workspace super-admins can reach the account through
   admin tooling and could delete blobs — see §7.
4. **Uploads are synchronous** in the request (§4.3). Acceptable form latency of
   0.5–2 s vs. keeping a transient local copy. **Decided 2026-10-10:
   synchronous.**
5. **`delete()` trashes rather than hard-deletes** (30-day Drive undo). Trash
   counts toward quota for those 30 days. **Decided 2026-10-10: trash.**

## 7. Risks

- **Drive outage** → reads of uncached files fail (503), uploads fail with a
  form error. Mitigation: cache absorbs the hot set; nothing is lost, users
  retry. Letter generation runs in Celery and can be re-triggered.
- **Index/Drive drift** (a DB restore that predates uploads, or a human moving
  files in the Drive UI). Mitigation: `rebuild_drive_index` from
  `appProperties.orbis_ref`, `verify_media_drive` as a weekly beat job with a WARNING in
  `ScheduledTaskRun` output. Nobody should touch the "Orbis Media" folder in the Drive UI; the `srv_orbis` login is for recovery only.
- **Refresh-token compromise** gives `drive.file` access as `srv_orbis`: the
  app-created ciphertext blobs only (unreadable without `MEDIA_ENCRYPTION_KEYS`,
  which lives in the VPS `.env` and nowhere near Drive), and nothing else in
  that mailbox. The token file lives outside the repo; revoke
  from the account's Security → Third-party access page and re-run
  `media_drive_authorize`.
- **The account is a single point of failure.** Suspension, deletion or an
  admin password reset of `srv_orbis` stops all media reads and writes
  (cache keeps serving the hot set). Deletion also deletes the files unless an
  admin transfers Drive ownership first. Mitigation: the account is already a
  production dependency for outbound mail, so it is on the same protection
  footing; document the dependency in `deploy/README.md`; `verify_media_drive`
  weekly beat job raises a WARNING the first morning the token fails.
- **Workspace storage policy changes** (per-user caps tightened by IT) hit this
  account directly, where a Shared Drive would have drawn on the Workspace pool. The probe
  command prints quota used/limit so it can be watched.
- **Quota exhaustion** of the Workspace pool is IT's to monitor; the probe
  command reports the account's Drive usage so it can be surfaced on the
  dashboard later.

---

## As built (Phase 1, 2026-10-10)

Deviations from the sections above, each deliberate:

- **No Google client libraries.** `media_store/drive.py` is ~150 lines over
  `requests` (token refresh, resumable upload, download, trash, list, about).
  Six calls did not justify four packages and an httplib2 transport.
- **Flat layout, no shard folders.** Every blob sits directly in the root
  folder. Nobody browses it and the API lists by query, so shards bought
  nothing and cost a folder lookup per upload.
- **The recovery reference lives in the blob's Drive `description`**, not in
  `appProperties.orbis_ref`: Drive caps each app property at 124 bytes, which
  a Fernet token of `{name, mime, size, fp}` exceeds. `appProperties` carries
  only a constant marker (`orbis=1`) so `rebuild_drive_index` can list exactly
  our files. The description is a token — opaque in the UI.
- **Key bookkeeping is a fingerprint, not an index**: `DriveObject.key_fingerprint`
  is the first 16 hex of SHA-256 of the key, so a rotation still knows which
  blobs are stale after the key list is reordered.
- **`verify_media_drive` is a no-op success while `MEDIA_BACKEND=filesystem`**
  (`--force` to run anyway), so the weekly beat job can be seeded enabled and
  the scheduler parity tests stay simple.
- **CKEditor root-level uploads** (`django_ckeditor_5` saves at the storage
  root with no prefix) are served to staff only — the same gate as its upload
  endpoint. Unknown prefixes are 404 for everyone, which is the safe default
  for any future `upload_to`.
- **`rotate_media_keys` is not built** (Phase 3 as planned); `generate_payroll_key`
  prints a key of the right format for `MEDIA_ENCRYPTION_KEYS`.
- Tests: 27 in `media_store/tests.py` (fake Drive injected through STORAGES
  OPTIONS), plus the scheduler parity entries. `manage.py media_drive_probe`
  against the real `srv_meals` Drive: encrypt → upload → download → decrypt →
  trash OK.

## Production rollout (2026-10-16)

- **Pre-flight found the nginx alias still live**: the Phase 1 deploy had left
  `location /media/ { alias … }` in place, plus three `deny all` blocks
  (`payroll/`, `documents/`, `legal/`) the earlier notes said were never
  applied. All four removed; an anonymous `/media/…` fetch now 302s to login.
- Media before migration: **965 MB, 1,479 files**. Backups: manual DB dump to
  Drive + `/home/ubuntu/media-pre-drive-2026-10-16.tgz` (937 MB).
- **Bug 1 — case-insensitive index.** The first run stopped at 114 files on a
  duplicate key: `DriveObject.name` inherited `utf8mb4_0900_ai_ci`, and two real
  profile pictures differ only in case. Fixed by `media_store/0002` (column →
  `utf8mb4_0900_bin`); `_save` now trashes the uploaded blob if the index
  write fails. Commit `02248fa`.
- **Bug 2 — blind re-PUT after a 5xx.** Drive answered one completed upload with
  500 "The operation was successful, but there was an error preparing the
  response"; the retry re-sent to the finished session and got 410.
  `drive.py::_put_session` now queries the session (`Content-Range: bytes */N`)
  after a 5xx and resends only what is missing. Commit `e74b5db`. Each of the
  two failures left one encrypted, unindexed blob in "Orbis Media" — harmless.
- Result: 1,479 indexed; `verify_media_drive --force` → 1,177 referenced, 0
  missing, 0 md5 drift, 302 orphans (files on disk no row references — old
  replaced uploads; copied anyway). Flipped, browser smoke test clean.
- **Left:** on 2026-10-30, `rm -rf ~/orbis/media` and the tarball (step 2.5).
  Same day: `media_store/rules.py` resolved a file's owner with a
  case-insensitive match against the owning table's column, so two
  case-variant names could be authorised against the wrong row. Every rule now
  goes through `rules._exact()`, which re-compares the name in Python.
