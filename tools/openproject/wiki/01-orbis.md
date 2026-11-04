# Orbis

Orbis is SEBPO's workforce-management platform. It consolidates the legacy HR, roster, leave, timesheet, documents, offboarding, policy and payroll tools into one Django 5.2 LTS application, with a UI modelled on TimeLy.

- **Live at:** https://orbis.sebpo.net (Microsoft Entra SSO)
- **Started:** 2026-04-24 · **MVP:** 2026-05-15 (shipped on schedule) · **Now:** post-MVP iteration, driven by HR/Finance requests
- **Active thread:** Payroll. Phases 1–4 are live. Phase 5+ (approval chain, payslips, bank advice) waits on HR/Finance sign-off.

## How this project is tracked

| Where | What |
|---|---|
| Work packages | One **Epic** per module, with **Features** (shipped, closed) and **Tasks** (open) under it, plus **Milestones** |
| Gantt | Feature bars span their first → last commit |
| Spent time | Development hours, logged per day from the roster and TimeLy |
| Wiki | This overview, plus [Architecture](architecture), [Modules](modules), [Deployment and Operations](deployment-and-operations), [Roadmap and Open items](roadmap-and-open-items) and [Docs index](docs-index) |

Every work package is generated from `tools/openproject/inventory.yaml` in the Orbis repository; `docs/OPENPROJECT.md` there explains the workflow. **Change the inventory, not the work package.** Edits made directly in OpenProject are reported as drift on the next sync.
