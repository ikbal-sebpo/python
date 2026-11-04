"""Seed the canonical SEBPO offboarding workflow.

Idempotent: re-runs touch nothing once the canonical workflow + steps +
field definitions exist. Creates one ``ClearanceWorkflowTemplate`` with
``code='SEBPO_DEFAULT_2026'``, ``is_default=True``, and 8 steps with
their predefined fields.

Depends on the clearance groups (IT, Finance, HR)
created by ``seed_orbis``.
"""

from __future__ import annotations

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction

from offboarding.models import (
    ClearanceStepFieldDefinition,
    ClearanceStepTemplate,
    ClearanceWorkflowTemplate,
)


WORKFLOW_CODE = "SEBPO_DEFAULT_2026"
WORKFLOW_NAME = "SEBPO default offboarding"


# Each step: dict of model fields PLUS a `fields` list of field defs.
STEPS = [
    {
        "step_code": "MANAGER_ACK",
        "step_name": "Manager Acknowledgement",
        "order": 1,
        "assigned_kind": ClearanceStepTemplate.AssignedKind.MANAGER,
        "assigned_group": None,
        "instruction": "Acknowledge the resignation and record any handover notes.",
        "fields": [
            ("handover_note", "Handover note", ClearanceStepFieldDefinition.FieldType.TEXTAREA, False, 1),
            ("recommended_lwd_change", "Recommended LWD change", ClearanceStepFieldDefinition.FieldType.DATE, False, 2),
        ],
    },
    {
        "step_code": "HR_INIT",
        "step_name": "HR Initial Validation",
        "order": 2,
        "assigned_kind": ClearanceStepTemplate.AssignedKind.GROUP,
        "assigned_group": "HR",
        "instruction": "Verify ID card return + personal contact details + appointment letter on file.",
        "fields": [
            ("id_card_received", "ID card received", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 1),
            ("personal_email", "Personal email", ClearanceStepFieldDefinition.FieldType.TEXT, True, 2),
            ("departmental_contact", "Departmental contact", ClearanceStepFieldDefinition.FieldType.TEXT, False, 3),
        ],
    },
    {
        "step_code": "IT",
        "step_name": "IT Clearance",
        "order": 3,
        "assigned_kind": ClearanceStepTemplate.AssignedKind.GROUP,
        "assigned_group": "IT",
        "instruction": "Disable email, revoke VPN, confirm laptop + access card returned.",
        "fields": [
            ("email_disabled", "Email disabled", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 1),
            ("vpn_revoked", "VPN revoked", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 2),
            ("laptop_returned", "Laptop returned", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 3),
            ("access_card_returned", "Access card returned", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 4),
            ("clearance_issue", "Outstanding issues", ClearanceStepFieldDefinition.FieldType.TEXTAREA, False, 5),
        ],
    },
    {
        "step_code": "FIN",
        "step_name": "Finance Clearance",
        "order": 4,
        "assigned_kind": ClearanceStepTemplate.AssignedKind.GROUP,
        "assigned_group": "Finance",
        "instruction": "Settle loans, advances, salary adjustment, gratuity.",
        "fields": [
            ("salary_adjustment_done", "Salary adjustment done", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 1),
            ("loan_balance_settled", "Loan balance settled", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 2),
            ("advance_settled", "Advance settled", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 3),
            ("gratuity_calculated", "Gratuity calculated", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 4),
            ("clearance_issue", "Outstanding issues", ClearanceStepFieldDefinition.FieldType.TEXTAREA, False, 5),
        ],
    },
    {
        "step_code": "ASSET",
        "step_name": "Asset Return",
        "order": 5,
        "assigned_kind": ClearanceStepTemplate.AssignedKind.GROUP,
        "assigned_group": "IT",
        "instruction": "Use the asset return panel below to log each item handed back.",
        "fields": [
            ("all_assets_returned", "All assets returned", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 1),
        ],
        "opens_asset_return": True,
    },
    {
        "step_code": "EXIT",
        "step_name": "Exit Interview",
        "order": 6,
        "assigned_kind": ClearanceStepTemplate.AssignedKind.GROUP,
        "assigned_group": "HR",
        "instruction": "Open the Exit Interview form below.",
        "fields": [
            ("interview_completed", "Interview completed", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 1),
        ],
        "opens_exit_interview": True,
    },
    {
        "step_code": "ERP",
        "step_name": "ERP Update",
        "order": 7,
        "assigned_kind": ClearanceStepTemplate.AssignedKind.GROUP,
        "assigned_group": "Finance",
        "instruction": "Mark the employee as separated in the master ERP system.",
        "fields": [
            ("erp_marked_separated", "ERP marked separated", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 1),
        ],
    },
    {
        "step_code": "HR_FINAL",
        "step_name": "Final HR Sign-off",
        "order": 8,
        "assigned_kind": ClearanceStepTemplate.AssignedKind.HR_HEAD,
        "assigned_group": "HR",
        "instruction": "Final approval. Marking this step DONE triggers letter issuance.",
        "fields": [
            ("clearance_letter_signed", "Clearance letter signed", ClearanceStepFieldDefinition.FieldType.BOOLEAN, False, 1),
            ("final_remarks", "Final remarks", ClearanceStepFieldDefinition.FieldType.TEXTAREA, False, 2),
        ],
        "triggers_letters": True,
    },
]


class Command(BaseCommand):
    help = "Seed the canonical SEBPO offboarding clearance workflow (idempotent)."

    @transaction.atomic
    def handle(self, *args, **opts):
        workflow, made = ClearanceWorkflowTemplate.objects.get_or_create(
            code=WORKFLOW_CODE,
            defaults={
                "name": WORKFLOW_NAME,
                "description": "Default 8-step clearance workflow for SEBPO employee separations.",
                "is_default": True,
                "is_active": True,
            },
        )
        self.stdout.write(f"workflow: {WORKFLOW_CODE} {'created' if made else 'exists'}")

        for spec in STEPS:
            group_name = spec["assigned_group"]
            group = Group.objects.filter(name=group_name).first() if group_name else None
            if group_name and not group:
                self.stdout.write(self.style.WARNING(
                    f"  step {spec['step_code']} skipped: group {group_name!r} missing — "
                    f"run seed_orbis first."
                ))
                continue

            st_defaults = {
                "step_name": spec["step_name"],
                "order": spec["order"],
                "assigned_kind": spec["assigned_kind"],
                "assigned_group": group,
                "instruction": spec.get("instruction", ""),
                "is_required": True,
                "is_active": True,
                "opens_exit_interview": spec.get("opens_exit_interview", False),
                "opens_asset_return": spec.get("opens_asset_return", False),
                "triggers_letters": spec.get("triggers_letters", False),
            }
            step, made_step = ClearanceStepTemplate.objects.update_or_create(
                workflow=workflow, step_code=spec["step_code"],
                defaults=st_defaults,
            )

            for fkey, flabel, ftype, freq, fsort in spec["fields"]:
                ClearanceStepFieldDefinition.objects.update_or_create(
                    step_template=step, field_key=fkey,
                    defaults={
                        "field_label": flabel,
                        "field_type": ftype,
                        "is_required": freq,
                        "sort_order": fsort,
                        "is_active": True,
                    },
                )

            self.stdout.write(
                f"  step {spec['step_code']} ({spec['order']}) "
                f"{'created' if made_step else 'updated'} "
                f"· {len(spec['fields'])} field(s)"
            )

        self.stdout.write(self.style.SUCCESS("✅ seed_offboarding_workflow complete"))
