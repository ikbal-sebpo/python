"""Tests for the documents app.

Coverage focus (pre-refactor safety net for the PDF/email pipeline):

  - Approval pipeline end-to-end: request created → review chain →
    approval chain → post-approval PDF generation + employee email
    (WeasyPrint is mocked — we assert pipeline behaviour, not PDF bytes)
  - PDF-render failure after approval is caught and recorded in
    ``rejection_history`` (stage="pdf") without crashing
  - Rejection flow (review + approval stages, append-only history)
  - DownloadPdfView access control (owner / signer / reviewer / HR / stranger)
  - Public verify + public PDF streaming (APPROVED only)
  - Template sanitisation (bleach allowlist), variable validation,
    version bump + TemplateRevision audit rows

Run with:
  python manage.py test documents --settings=core.settings_test
"""

from __future__ import annotations

import tempfile
from io import StringIO
from unittest import mock

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core import mail
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from documents.models import (
    DocumentExtraFieldType,
    DocumentExtraFieldValue,
    DocumentRequest,
    DocumentReviewConfig,
    DocumentSignatoryConfig,
    DocumentType,
    MessageTemplate,
    RequestApproval,
    RequestReview,
    TemplateDesign,
    TemplateRevision,
)
from documents.pdf import PDF_CONTEXT_SCHEMA, _build_context, validate_template_against_context
from documents.sanitize import clean_template_html
from documents.management.commands.seed_document_types import SALARY_EXTRA_FIELDS, _SALARY_CERT_BODY_HTML
from documents.forms import ApprovalActionForm, DocumentRequestForm
from payroll.models import EmployeeBankAccount

User = get_user_model()

PDF_BYTES = b"%PDF-1.4\n%fake orbis test pdf\n"

# Keep generated files out of the real media/ tree.
_MEDIA_ROOT = tempfile.mkdtemp(prefix="orbis-documents-tests-")


# ---------------------------------------------------------------------------
# Factory helpers (mirror leave/tests.py style)
# ---------------------------------------------------------------------------

def _user(slug, *, groups=(), **extra):
    u = User.objects.create_user(
        email=f"{slug}@sebpo.com",
        username=slug,
        password="test-1234!",
        first_name=slug.title(),
        last_name="Test",
        **extra,
    )
    for g in groups:
        u.groups.add(g)
    return u


def _templates(suffix=""):
    """A minimal design (with the required {{ body_html|safe }} slot) and a
    message body referencing real context variables."""
    design = TemplateDesign.objects.create(
        key=f"design-test{suffix}",
        name=f"Test design{suffix}",
        html=(
            "<html><body><div class='page'>{{ body_html|safe }}"
            "<footer>{{ company_name }}</footer></div></body></html>"
        ),
    )
    message = MessageTemplate.objects.create(
        key=f"msg-test{suffix}",
        name=f"Test message{suffix}",
        body_html="<p>Dear {{ employee.get_full_name }}, ref {{ ref_no }}.</p>",
    )
    return design, message


def _doc_type(code, *, design=None, message=None, review_groups=(),
              signatory_groups=(), requires_approval=True,
              auto_approve=False, auto_approve_reason=""):
    dt = DocumentType.objects.create(
        code=code,
        name=code.lower(),
        display_name=f"{code} letter",
        template_design=design,
        message_template=message,
        requires_approval=requires_approval,
        auto_approve=auto_approve,
        auto_approve_reason=auto_approve_reason,
    )
    for i, g in enumerate(review_groups, start=1):
        DocumentReviewConfig.objects.create(
            document_type=dt, reviewer_group=g, review_order=i,
        )
    for i, g in enumerate(signatory_groups, start=1):
        DocumentSignatoryConfig.objects.create(
            document_type=dt, signatory_group=g, signature_order=i,
        )
    return dt


def _request(employee, doc_type, applied_by=None, **extra):
    """Creating the row fires documents.signals.create_review_chain."""
    return DocumentRequest.objects.create(
        employee=employee,
        document_type=doc_type,
        applied_by=applied_by or employee,
        **extra,
    )


def _approve(approval, user, remarks=""):
    approval.signatory = user
    approval.status = RequestApproval.Status.APPROVED
    approval.remarks = remarks
    approval.signed_at = timezone.now()
    approval.save()


def _mock_weasyprint():
    """Patch WeasyPrint where documents.pdf uses it. The pipeline still
    exercises the context builder + Django template rendering; only the
    CPU-heavy PDF render is replaced."""
    patcher = mock.patch("documents.pdf.HTML")
    html_cls = patcher.start()
    html_cls.return_value.write_pdf.return_value = PDF_BYTES
    return patcher, html_cls


# ---------------------------------------------------------------------------
# 1. Approval pipeline end-to-end
# ---------------------------------------------------------------------------

@override_settings(
    MEDIA_ROOT=_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class ApprovalPipelineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reviewer_group = Group.objects.create(name="Doc Reviewers")
        cls.signer_group = Group.objects.create(name="Doc Signers")
        cls.signer_group2 = Group.objects.create(name="Doc Signers L2")
        cls.employee = _user("emp")
        cls.reviewer = _user("reviewer", groups=[cls.reviewer_group])
        cls.approver = _user("approver", groups=[cls.signer_group])
        cls.approver2 = _user("approver2", groups=[cls.signer_group2])
        cls.design, cls.message = _templates()

    def test_create_with_review_chain_sets_in_review_and_notifies_reviewer(self):
        dt = _doc_type(
            "NOC", design=self.design, message=self.message,
            review_groups=[self.reviewer_group],
            signatory_groups=[self.signer_group],
        )
        with self.captureOnCommitCallbacks(execute=True):
            req = _request(self.employee, dt)

        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.IN_REVIEW)
        review = req.reviews.get()
        self.assertEqual(review.reviewer_group, self.reviewer_group)
        self.assertEqual(review.status, RequestReview.Status.PENDING)
        self.assertEqual(review.order, 1)
        # Approval chain is NOT created until the review chain completes.
        self.assertEqual(req.approvals.count(), 0)
        # Reviewer group got the assignment email (on commit).
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.reviewer.email])

    def test_review_complete_moves_to_pending_and_creates_approval_chain(self):
        dt = _doc_type(
            "EXP", design=self.design, message=self.message,
            review_groups=[self.reviewer_group],
            signatory_groups=[self.signer_group],
        )
        req = _request(self.employee, dt)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            req.reviews.get().mark_reviewed(self.reviewer, "looks fine")

        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.PENDING)
        approval = req.approvals.get()
        self.assertEqual(approval.approver_group, self.signer_group)
        self.assertEqual(approval.status, RequestApproval.Status.PENDING)
        # Next actor (approver group) notified.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.approver.email])

    def test_final_approval_generates_pdf_and_emails_employee(self):
        """End-to-end through the ApprovalActionView: POST approve → status
        APPROVED → on-commit hook renders PDF (mocked WeasyPrint), attaches
        the file, and emails the employee download + verify links."""
        dt = _doc_type(
            "RELEASE", design=self.design, message=self.message,
            review_groups=[self.reviewer_group],
            signatory_groups=[self.signer_group],
        )
        req = _request(self.employee, dt)
        req.reviews.get().mark_reviewed(self.reviewer, "ok")
        approval = req.approvals.get()
        mail.outbox.clear()

        self.client.force_login(self.approver)
        patcher, html_cls = _mock_weasyprint()
        self.addCleanup(patcher.stop)
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                reverse("documents:approval_action", args=[approval.pk]),
                {"status": RequestApproval.Status.APPROVED, "remarks": "Approved"},
            )
        self.assertRedirects(
            resp, reverse("documents:request_detail", args=[req.pk]),
            fetch_redirect_response=False,
        )

        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.APPROVED)
        self.assertEqual(req.processed_by, self.approver)
        self.assertIsNotNone(req.processed_at)
        # The WeasyPrint hook fired exactly once and the file was attached.
        html_cls.assert_called_once()
        self.assertTrue(req.generated_file)
        self.assertIn(dt.code, req.generated_file.name)
        with req.generated_file.open("rb") as fh:
            self.assertEqual(fh.read(), PDF_BYTES)
        # Employee got the "your document is ready" email with both links.
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, [self.employee.email])
        self.assertIn(f"/documents/requests/{req.pk}/download/", msg.body)
        self.assertIn(f"/verify/{req.pk}/", msg.body)

    def test_multi_signatory_partial_then_full_approval(self):
        dt = _doc_type(
            "MULTI", design=self.design, message=self.message,
            signatory_groups=[self.signer_group, self.signer_group2],
        )
        req = _request(self.employee, dt)
        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.PENDING)
        self.assertEqual(req.approvals.count(), 2)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            _approve(req.approvals.get(order=1), self.approver)
        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.PARTIALLY_APPROVED)
        # The next approver group was notified.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.approver2.email])

        mail.outbox.clear()
        patcher, _html = _mock_weasyprint()
        self.addCleanup(patcher.stop)
        with self.captureOnCommitCallbacks(execute=True):
            _approve(req.approvals.get(order=2), self.approver2)
        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.APPROVED)
        self.assertEqual(req.processed_by, self.approver2)
        self.assertTrue(req.generated_file)
        self.assertEqual(mail.outbox[-1].to, [self.employee.email])

    def test_pdf_failure_after_approval_is_caught_and_recorded(self):
        """A render crash after final approval must not blow up the commit:
        the request stays APPROVED, a stage='pdf' entry lands in
        rejection_history, and no 'ready' email goes out."""
        dt = _doc_type(
            "FAIL", design=self.design, message=self.message,
            signatory_groups=[self.signer_group],
        )
        req = _request(self.employee, dt)
        mail.outbox.clear()

        with mock.patch("documents.pdf.HTML", side_effect=RuntimeError("render boom")):
            with self.assertLogs("documents.signals", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    _approve(req.approvals.get(), self.approver)

        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.APPROVED)
        self.assertFalse(req.generated_file)
        entry = req.rejection_history[-1]
        self.assertEqual(entry["stage"], "pdf")
        self.assertEqual(entry["step_order"], 0)
        self.assertIn("PDF generation failed", entry["reason"])
        self.assertIsNone(entry["by_pk"])
        self.assertEqual(len(mail.outbox), 0)

    def test_auto_approve_skips_chain_and_generates_immediately(self):
        dt = _doc_type(
            "AUTO", design=self.design, message=self.message,
            requires_approval=False, auto_approve=True,
            auto_approve_reason="No human review needed",
        )
        patcher, _html = _mock_weasyprint()
        self.addCleanup(patcher.stop)
        with self.captureOnCommitCallbacks(execute=True):
            req = _request(self.employee, dt)

        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.APPROVED)
        self.assertTrue(req.process_note.startswith("Auto-approved:"))
        self.assertIsNotNone(req.processed_at)
        self.assertEqual(req.reviews.count(), 0)
        self.assertEqual(req.approvals.count(), 0)
        self.assertTrue(req.generated_file)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.employee.email])

    def test_misconfigured_type_without_chain_logs_warning_and_stalls(self):
        """requires_approval=False + auto_approve=False + no configs: the
        request has nowhere to go — the signal logs a warning and leaves it
        PENDING forever (documented current behaviour)."""
        dt = _doc_type(
            "STUCK", design=self.design, message=self.message,
            requires_approval=False, auto_approve=False,
        )
        with self.assertLogs("documents.signals", level="WARNING"):
            req = _request(self.employee, dt)
        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.PENDING)
        self.assertEqual(req.reviews.count(), 0)
        self.assertEqual(req.approvals.count(), 0)


# ---------------------------------------------------------------------------
# 2. Rejection flow
# ---------------------------------------------------------------------------

@override_settings(
    MEDIA_ROOT=_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class RejectionFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reviewer_group = Group.objects.create(name="Rej Reviewers")
        cls.signer_group = Group.objects.create(name="Rej Signers")
        cls.employee = _user("rejemp")
        cls.reviewer = _user("rejreviewer", groups=[cls.reviewer_group])
        cls.approver = _user("rejapprover", groups=[cls.signer_group])
        design, message = _templates("-rej")
        cls.doc_type = _doc_type(
            "REJ", design=design, message=message,
            review_groups=[cls.reviewer_group],
            signatory_groups=[cls.signer_group],
        )

    def test_review_rejection_records_history_and_notifies_employee(self):
        req = _request(self.employee, self.doc_type)
        review = req.reviews.get()
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            review.reviewer = self.reviewer
            review.status = RequestReview.Status.REJECTED
            review.remarks = "Missing passport copy"
            review.reviewed_at = timezone.now()
            review.save()

        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.REJECTED)
        self.assertEqual(req.processed_by, self.reviewer)
        self.assertEqual(req.process_note, "Rejected at review step 1")
        self.assertEqual(len(req.rejection_history), 1)
        entry = req.latest_rejection
        self.assertEqual(entry["stage"], "review")
        self.assertEqual(entry["step_order"], 1)
        self.assertEqual(entry["reason"], "Missing passport copy")
        self.assertEqual(entry["by_pk"], str(self.reviewer.pk))
        self.assertEqual(entry["by_email"], self.reviewer.email)
        # Employee notified of the rejection.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.employee.email])

    def test_approval_rejection_records_history(self):
        req = _request(self.employee, self.doc_type)
        req.reviews.get().mark_reviewed(self.reviewer, "ok")
        approval = req.approvals.get()
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            approval.signatory = self.approver
            approval.status = RequestApproval.Status.REJECTED
            approval.remarks = "Not eligible"
            approval.signed_at = timezone.now()
            approval.save()

        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.REJECTED)
        self.assertEqual(req.processed_by, self.approver)
        entry = req.latest_rejection
        self.assertEqual(entry["stage"], "approval")
        self.assertEqual(entry["reason"], "Not eligible")
        self.assertFalse(req.generated_file)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.employee.email])


# ---------------------------------------------------------------------------
# 3. Download access control + public verify
# ---------------------------------------------------------------------------

@override_settings(
    MEDIA_ROOT=_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class DownloadAndPublicVerifyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reviewer_group = Group.objects.create(name="DL Reviewers")
        cls.signer_group = Group.objects.create(name="DL Signers")
        cls.hr_group = Group.objects.create(name="HR")  # can_see_all group
        cls.owner = _user("dlowner")
        cls.reviewer = _user("dlreviewer", groups=[cls.reviewer_group])
        cls.signer = _user("dlsigner", groups=[cls.signer_group])
        cls.hr = _user("dlhr", groups=[cls.hr_group])
        cls.stranger = _user("dlstranger")
        design, message = _templates("-dl")
        cls.doc_type = _doc_type(
            "DL", design=design, message=message,
            review_groups=[cls.reviewer_group],
            signatory_groups=[cls.signer_group],
        )

        # Drive a request through the full chain. on_commit callbacks are
        # never executed here, so no real render happens; we attach the PDF
        # bytes by hand exactly as generate_and_attach_pdf would.
        cls.approved = _request(cls.owner, cls.doc_type)
        cls.approved.reviews.get().mark_reviewed(cls.reviewer, "ok")
        _approve(cls.approved.approvals.get(), cls.signer)
        cls.approved.refresh_from_db()
        cls.approved.generated_file.save(
            f"{cls.doc_type.code}_{cls.approved.id}.pdf",
            ContentFile(PDF_BYTES),
        )

        # A second request still in review (no PDF).
        cls.pending = _request(cls.owner, cls.doc_type)

    def setUp(self):
        self.download_url = reverse(
            "documents:download_pdf", args=[self.approved.pk],
        )

    def test_setup_reached_approved_state(self):
        self.assertEqual(self.approved.status, DocumentRequest.Status.APPROVED)
        self.assertTrue(self.approved.generated_file)

    def test_owner_signer_reviewer_and_hr_can_download(self):
        for user in (self.owner, self.signer, self.reviewer, self.hr):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                resp = self.client.get(self.download_url)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp["Content-Type"], "application/pdf")
                self.assertIn("attachment", resp["Content-Disposition"])
                self.assertEqual(b"".join(resp.streaming_content), PDF_BYTES)

    def test_unrelated_user_cannot_download(self):
        self.client.force_login(self.stranger)
        resp = self.client.get(self.download_url)
        self.assertEqual(resp.status_code, 403)

    def test_download_404_when_pdf_not_generated(self):
        self.client.force_login(self.owner)
        resp = self.client.get(
            reverse("documents:download_pdf", args=[self.pending.pk]),
        )
        self.assertEqual(resp.status_code, 404)

    # ----- public verify (no auth) -----

    def test_public_verify_shows_approved_document(self):
        resp = self.client.get(
            reverse("documents_public:verify_document", args=[self.approved.pk]),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "documents/public_verify_success.html")
        self.assertContains(resp, self.owner.display_name)
        self.assertContains(resp, self.doc_type.display_name)

    def test_public_verify_fails_for_non_approved(self):
        resp = self.client.get(
            reverse("documents_public:verify_document", args=[self.pending.pk]),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "documents/public_verify_fail.html")

    def test_public_verify_unknown_uuid_404(self):
        import uuid
        resp = self.client.get(
            reverse("documents_public:verify_document", args=[uuid.uuid4()]),
        )
        self.assertEqual(resp.status_code, 404)

    def test_public_pdf_streams_approved_only(self):
        resp = self.client.get(
            reverse("documents_public:public_pdf", args=[self.approved.pk]),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("inline", resp["Content-Disposition"])
        self.assertEqual(b"".join(resp.streaming_content), PDF_BYTES)

    def test_public_pdf_404_for_non_approved(self):
        resp = self.client.get(
            reverse("documents_public:public_pdf", args=[self.pending.pk]),
        )
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# 4. Template sanitisation + variable validation + revision audit
# ---------------------------------------------------------------------------

class TemplateSanitisationTests(TestCase):
    def test_script_tags_stripped_from_message_template_on_save(self):
        mt = MessageTemplate(
            key="san-script", name="Sanitise script",
            body_html='<p>Hello</p><script>alert("xss")</script>',
        )
        mt.save()
        mt.refresh_from_db()
        self.assertNotIn("<script", mt.body_html)
        self.assertIn("<p>Hello</p>", mt.body_html)
        # The save surfaced a non-blocking warning for the admin.
        self.assertTrue(
            any("sanitisation" in w for w in mt._template_warnings),
            mt._template_warnings,
        )

    def test_disallowed_css_properties_stripped(self):
        mt = MessageTemplate(
            key="san-css", name="Sanitise css",
            body_html='<p style="color: red; position: fixed;">x</p>',
        )
        mt.save()
        mt.refresh_from_db()
        self.assertIn("color: red", mt.body_html)
        self.assertNotIn("position", mt.body_html)

    def test_template_design_is_not_sanitised(self):
        """Designs need full <html>/<style> for WeasyPrint; _SANITIZE=False."""
        html = "<html><head><style>body { color: red; }</style></head><body>{{ body_html|safe }}</body></html>"
        td = TemplateDesign(key="san-design", name="Design", html=html)
        td.save()
        td.refresh_from_db()
        self.assertEqual(td.html, html)

    def test_clean_template_html_reports_modification_flag(self):
        cleaned, modified = clean_template_html("<p>safe</p>")
        self.assertEqual(cleaned, "<p>safe</p>")
        self.assertFalse(modified)
        _cleaned, modified = clean_template_html("<p onclick='x()'>hi</p>")
        self.assertTrue(modified)

    def test_unknown_variables_warn_known_ones_do_not(self):
        warnings = validate_template_against_context(
            "{{ nope }} {{ employee.get_full_name }} {{ extra.thing|upper }} "
            "{{ body_html|safe }}",
            PDF_CONTEXT_SCHEMA,
        )
        self.assertEqual(warnings, ["Unknown template variable: {{ nope }}"])

    def test_version_bump_and_revision_rows(self):
        mt = MessageTemplate(key="rev-test", name="Rev", body_html="<p>v1</p>")
        mt.save()
        self.assertEqual(mt.version, 1)
        self.assertEqual(
            TemplateRevision.objects.filter(message_template=mt).count(), 1,
        )

        mt.body_html = "<p>v2</p>"
        mt.save()
        self.assertEqual(mt.version, 2)
        revs = TemplateRevision.objects.filter(message_template=mt).order_by("version")
        self.assertEqual(revs.count(), 2)
        self.assertEqual(revs.last().html_snapshot, "<p>v2</p>")

        # Saving without an HTML change must NOT bump or add a revision.
        mt.name = "Renamed"
        mt.save()
        self.assertEqual(mt.version, 2)
        self.assertEqual(
            TemplateRevision.objects.filter(message_template=mt).count(), 2,
        )


# ---------------------------------------------------------------------------
# 5. Starter template management commands
# ---------------------------------------------------------------------------

class StarterTemplateCommandTests(TestCase):
    def test_apply_salary_certificate_template_updates_body_and_fields(self):
        design, message = _templates("-salary-command")
        doc_type = _doc_type("SALARY_CERT", design=design, message=message)

        stdout = StringIO()
        call_command("apply_salary_certificate_template", stdout=stdout)

        doc_type.refresh_from_db()
        message = doc_type.message_template

        self.assertIn("SALARY CERTIFICATE", message.body_html)
        self.assertIn("Payment Summary", message.body_html)
        self.assertIn("salary_display_month_1_month", message.body_html)
        self.assertIn("salary_display_month_3_net_take_home", message.body_html)
        self.assertIn("font-size:10pt", message.body_html)
        self.assertEqual(
            DocumentExtraFieldType.objects.filter(
                document_type=doc_type,
                is_active=True,
            ).count(),
            len(SALARY_EXTRA_FIELDS),
        )
        self.assertFalse(
            DocumentExtraFieldType.objects.filter(
                document_type=doc_type,
                field_key="ref_no",
                is_active=True,
            ).exists(),
        )
        self.assertTrue(
            DocumentExtraFieldType.objects.filter(
                document_type=doc_type,
                field_key="salary_month_1_name",
                is_required=True,
            ).exists(),
        )
        self.assertTrue(
            DocumentExtraFieldType.objects.filter(
                document_type=doc_type,
                field_key="bank_account_no",
                is_required=False,
            ).exists(),
        )
        self.assertIn("Salary certificate template applied.", stdout.getvalue())


class SalaryCertificateContextTests(TestCase):
    def setUp(self):
        design, message = _templates("-salary-context")
        self.doc_type = _doc_type("SALARY_CERT", design=design, message=message)
        self.employee = _user("salaryctx")

    def _salary_request(self, input_data):
        return _request(self.employee, self.doc_type, input_data=input_data)

    def test_salary_months_are_sorted_and_amounts_are_formatted(self):
        req = self._salary_request({
            "salary_month_1_name": "May-26",
            "salary_month_1_gross_salary": "92500",
            "salary_month_1_festival_bonus": "46250",
            "salary_month_1_gross_payment": "138750",
            "salary_month_1_lunch_allowance": "-550",
            "salary_month_1_tax": "-3100",
            "salary_month_1_net_take_home": "135100",
            "salary_month_2_name": "July 2026",
            "salary_month_2_gross_salary": "92500",
            "salary_month_2_gross_payment": "92500",
            "salary_month_2_lunch_allowance": "-500",
            "salary_month_2_tax": "-2400",
            "salary_month_2_net_take_home": "89600",
            "salary_month_3_name": "Jun-26",
            "salary_month_3_gross_salary": "92500",
            "salary_month_3_gross_payment": "92500",
            "salary_month_3_lunch_allowance": "-300",
            "salary_month_3_net_take_home": "92200",
        })

        salary_months = _build_context(req)["salary_months"]

        self.assertEqual([m["month"] for m in salary_months], ["Jul-26", "Jun-26", "May-26"])
        self.assertEqual(salary_months[0]["gross_salary"], "92,500")
        self.assertEqual(salary_months[0]["festival_bonus"], "-")
        self.assertEqual(salary_months[0]["lunch_allowance"], "(500)")
        self.assertEqual(salary_months[0]["tax"], "(2,400)")
        self.assertEqual(salary_months[2]["festival_bonus"], "46,250")
        self.assertEqual(salary_months[2]["net_take_home"], "135,100")

    def test_bank_account_number_falls_back_to_primary_employee_account(self):
        EmployeeBankAccount.objects.create(
            employee=self.employee,
            account_type=EmployeeBankAccount.AccountType.SAVINGS,
            account_name="Salary Context",
            account_number="118273645",
            is_primary=True,
        )
        req = self._salary_request({"salary_month_1_name": "Jul-26"})

        ctx = _build_context(req)

        self.assertEqual(ctx["bank_account_number"], "118273645")

    def test_salary_certificate_template_renders_salary_table(self):
        design = TemplateDesign.objects.create(
            key="salary-render-design",
            name="Salary render design",
            html="<html><body>{{ body_html|safe }}</body></html>",
        )
        message = MessageTemplate.objects.create(
            key="salary-render-message",
            name="Salary render message",
            body_html=_SALARY_CERT_BODY_HTML,
        )
        doc_type = _doc_type("SALARY_RENDER", design=design, message=message)
        req = _request(self.employee, doc_type, input_data={
            "salary_month_1_name": "Jul-26",
            "salary_month_1_gross_salary": "92500",
            "salary_month_1_gross_payment": "92500",
            "salary_month_1_lunch_allowance": "-500",
            "salary_month_1_tax": "-2400",
            "salary_month_1_net_take_home": "89600",
            "bank_account_number": "118273645",
        })
        patcher, html_cls = _mock_weasyprint()
        self.addCleanup(patcher.stop)

        from documents.pdf import render_request_to_pdf_bytes

        pdf = render_request_to_pdf_bytes(req)
        rendered_html = html_cls.call_args.kwargs["string"]

        self.assertEqual(pdf, PDF_BYTES)
        self.assertIn("SALARY CERTIFICATE", rendered_html)
        self.assertIn("Jul-26", rendered_html)
        self.assertIn("92,500", rendered_html)
        self.assertIn("(500)", rendered_html)
        self.assertIn("(2,400)", rendered_html)
        self.assertIn("118273645", rendered_html)

    def test_salary_certificate_ref_no_is_generated_without_manual_field(self):
        req = self._salary_request({})

        ctx = _build_context(req)

        self.assertTrue(ctx["ref_no"].startswith("SE/HRD/"))
        self.assertIn(str(req.id)[:8], ctx["ref_no"])


class ExtraFieldFillResponsibilityTests(TestCase):
    def setUp(self):
        design, message = _templates("-fill-responsibility")
        self.doc_type = _doc_type("FILL_RESP", design=design, message=message)
        self.employee = _user("fillrespemp")
        self.approver = _user("fillrespapprover")
        self.group = Group.objects.create(name="Fill Responsibility Approvers")
        self.requester_field = DocumentExtraFieldType.objects.create(
            document_type=self.doc_type,
            field_key="destination_country",
            field_name="Destination Country",
            kind=DocumentExtraFieldType.FieldKind.TEXT,
            fillable_by=DocumentExtraFieldType.FillableBy.REQUESTER,
            is_required=True,
        )
        self.approver_field = DocumentExtraFieldType.objects.create(
            document_type=self.doc_type,
            field_key="salary_month_1_gross_salary",
            field_name="Month 1 Gross Salary",
            kind=DocumentExtraFieldType.FieldKind.TEXT,
            fillable_by=DocumentExtraFieldType.FillableBy.APPROVER,
            is_required=True,
            sort_order=2,
        )

    def test_request_form_only_shows_requester_fields(self):
        form = DocumentRequestForm(document_type=self.doc_type, user=self.employee)

        self.assertIn("destination_country", form.fields)
        self.assertNotIn("salary_month_1_gross_salary", form.fields)

    def test_approval_form_requires_approver_fields_only_when_approving(self):
        req = _request(self.employee, self.doc_type)
        approval = RequestApproval.objects.create(
            request=req,
            approver_group=self.group,
        )

        approve_form = ApprovalActionForm(
            data={"status": RequestApproval.Status.APPROVED, "remarks": ""},
            instance=approval,
        )
        reject_form = ApprovalActionForm(
            data={"status": RequestApproval.Status.REJECTED, "remarks": "Not ready"},
            instance=approval,
        )

        self.assertFalse(approve_form.is_valid())
        self.assertIn("salary_month_1_gross_salary", approve_form.errors)
        self.assertTrue(reject_form.is_valid())

    def test_approval_form_saves_approver_extra_values_to_request_snapshot(self):
        req = _request(self.employee, self.doc_type)
        approval = RequestApproval.objects.create(
            request=req,
            approver_group=self.group,
        )
        form = ApprovalActionForm(
            data={
                "status": RequestApproval.Status.APPROVED,
                "remarks": "",
                "salary_month_1_gross_salary": "92500",
            },
            instance=approval,
        )

        self.assertTrue(form.is_valid(), form.errors)
        form.save_extra_values(submitted_by=self.approver)
        req.refresh_from_db()

        self.assertEqual(req.input_data["salary_month_1_gross_salary"], "92500")
        value = DocumentExtraFieldValue.objects.get(
            request=req,
            field_type=self.approver_field,
        )
        self.assertEqual(value.value, "92500")
        self.assertEqual(value.submitted_by, self.approver)

    def test_salary_approval_form_groups_month_fields_with_dropdown_months(self):
        design, message = _templates("-salary-wizard")
        doc_type = _doc_type("SALARY_CERT", design=design, message=message)
        for spec in (
            {"field_key": "ref_no", "field_name": "Reference No."},
            {"field_key": "salary_month_1_name", "field_name": "Month 1 Name"},
            {"field_key": "salary_month_1_gross_salary", "field_name": "Month 1 Gross Salary"},
            {"field_key": "salary_month_2_name", "field_name": "Month 2 Name"},
            {"field_key": "salary_month_3_name", "field_name": "Month 3 Name"},
            {"field_key": "bank_account_no", "field_name": "Bank Account No."},
        ):
            DocumentExtraFieldType.objects.create(
                document_type=doc_type,
                kind=DocumentExtraFieldType.FieldKind.TEXT,
                fillable_by=DocumentExtraFieldType.FillableBy.APPROVER,
                is_required=True,
                **spec,
            )
        req = _request(self.employee, doc_type)
        approval = RequestApproval.objects.create(
            request=req,
            approver_group=self.group,
        )

        form = ApprovalActionForm(instance=approval)

        self.assertTrue(form.has_salary_month_wizard)
        self.assertEqual(len(form.salary_month_field_groups), 3)
        self.assertEqual(form.salary_month_field_groups[0]["title"], "Month 1")
        self.assertNotIn("ref_no", form.fields)
        self.assertNotIn("ref_no", [field.name for field in form.salary_common_fields])
        self.assertIn("salary_month_1_name", [field.name for field in form.salary_month_field_groups[0]["fields"]])
        self.assertIsInstance(form.fields["salary_month_1_name"].widget, forms.Select)


# ---------------------------------------------------------------------------
# 6. Request form view (dynamic fields) + cancel
# ---------------------------------------------------------------------------

@override_settings(
    MEDIA_ROOT=_MEDIA_ROOT,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class RequestFormAndCancelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reviewer_group = Group.objects.create(name="Form Reviewers")
        cls.employee = _user("formemp")
        cls.reviewer = _user("formreviewer", groups=[cls.reviewer_group])
        cls.stranger = _user("formstranger")
        design, message = _templates("-form")
        cls.doc_type = _doc_type(
            "FORM", design=design, message=message,
            review_groups=[cls.reviewer_group],
        )
        DocumentExtraFieldType.objects.create(
            document_type=cls.doc_type,
            field_key="destination_country",
            field_name="Destination country",
            kind=DocumentExtraFieldType.FieldKind.TEXT,
            is_required=True,
        )

    def test_post_creates_request_with_input_data_and_extra_values(self):
        self.client.force_login(self.employee)
        resp = self.client.post(
            reverse("documents:request_form", args=[self.doc_type.pk]),
            {"reason": "Visa application", "destination_country": "Japan"},
        )
        req = DocumentRequest.objects.get(employee=self.employee)
        self.assertRedirects(
            resp, reverse("documents:request_detail", args=[req.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(req.status, DocumentRequest.Status.IN_REVIEW)
        self.assertEqual(req.applied_by, self.employee)
        self.assertEqual(req.input_data["reason"], "Visa application")
        self.assertEqual(req.input_data["destination_country"], "Japan")
        efv = DocumentExtraFieldValue.objects.get(request=req)
        self.assertEqual(efv.field_type.field_key, "destination_country")
        self.assertEqual(efv.value, "Japan")

    def test_post_missing_required_extra_field_rerenders_with_error(self):
        self.client.force_login(self.employee)
        resp = self.client.post(
            reverse("documents:request_form", args=[self.doc_type.pk]),
            {"reason": "Visa application"},  # destination_country missing
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "This field is required")
        self.assertFalse(
            DocumentRequest.objects.filter(employee=self.employee).exists(),
        )

    def test_owner_can_cancel_while_in_review(self):
        req = _request(self.employee, self.doc_type)
        self.client.force_login(self.employee)
        resp = self.client.post(
            reverse("documents:request_cancel", args=[req.pk]),
        )
        self.assertEqual(resp.status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.CANCELLED)
        self.assertEqual(req.processed_by, self.employee)

    def test_stranger_cannot_cancel(self):
        req = _request(self.employee, self.doc_type)
        self.client.force_login(self.stranger)
        resp = self.client.post(
            reverse("documents:request_cancel", args=[req.pk]),
        )
        self.assertEqual(resp.status_code, 403)
        req.refresh_from_db()
        self.assertEqual(req.status, DocumentRequest.Status.IN_REVIEW)
