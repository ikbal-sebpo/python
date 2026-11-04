"""PDF rendering pipeline for the documents app.

Ports veridoc's ``pdf_generator.py`` with three changes:

* hardcoded paths (logo, office addresses) → ``CompanyBranding`` singleton
* an explicit ``PDF_CONTEXT_SCHEMA`` documents every available template variable
  (the validator + admin "test render" action use it as the source of truth)
* ``bleach`` sanitisation happens at template-save time, not at render time —
  the renderer reads pre-sanitised content from the DB

The public API used by signals + views:

    render_request_to_pdf_bytes(req)        → bytes
    generate_and_attach_pdf(request_id)     → None  (saves on DocumentRequest.generated_file)
    test_render_pdf(doc_type, sample={})    → bytes (admin "test render" action)
    validate_template_against_context(html, schema) → list[str] warnings
"""

from __future__ import annotations

import base64
import io
import logging
import mimetypes
import os
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from core.money import format_taka
from pathlib import Path
from typing import Any

import qrcode
from django.conf import settings
from django.template import Context
from django.template.base import Lexer, TokenType
from django.template.engine import Engine
from django.urls import reverse
from django.utils import timezone
from weasyprint import HTML

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context schema (source of truth for the variable validator + test renders)
# ---------------------------------------------------------------------------

PDF_CONTEXT_SCHEMA: dict[str, dict[str, str]] = {
    # The DocumentRequest itself
    "request":              {"type": "DocumentRequest", "source": "req"},
    "employee":             {"type": "CustomUser", "source": "req.employee"},
    "document_type":        {"type": "DocumentType", "source": "req.document_type"},
    "input_data":           {"type": "dict", "source": "req.input_data (raw)"},
    "extra":                {"type": "dict", "source": "DocumentExtraFieldValue map"},

    # Date helpers
    "today":                {"type": "date", "source": "timezone.localdate()"},
    "today_mmddyyyy":       {"type": "str",  "source": "today as MM/DD/YYYY"},
    "letter_date":          {"type": "date", "source": "input_data.letter_date OR today"},
    "letter_date_mmddyyyy": {"type": "str",  "source": "letter_date as MM/DD/YYYY"},

    # Verification surface
    "public_url":           {"type": "str", "source": "abs URL to /verify/<id>/"},
    "qr_data_uri":          {"type": "str", "source": "data: URI PNG of public_url QR"},

    # Branding (from CompanyBranding, overridable via input_data)
    "logo_url":             {"type": "str", "source": "CompanyBranding.logo (data URI)"},
    "company_name":         {"type": "str", "source": "CompanyBranding.company_name"},
    "footer_company":       {"type": "str", "source": "CompanyBranding.footer_company"},
    "head_office_address":  {"type": "str", "source": "CompanyBranding.head_office_address"},
    "branch_office_address":{"type": "str", "source": "CompanyBranding.branch_office_address"},
    "jashore_office_address":{"type": "str","source": "CompanyBranding.jashore_office_address"},
    "website":              {"type": "str", "source": "CompanyBranding.website"},
    "ref_no":               {"type": "str", "source": "input_data.ref_no OR default prefix"},
    "subject":              {"type": "str", "source": "input_data.subject OR document_type.display_name"},

    # Employee-derived
    "department":           {"type": "str",  "source": "employee.department.name"},
    "designation":          {"type": "str",  "source": "employee.designation.name"},
    "work_location":        {"type": "str",  "source": "employee.work_location.name"},
    "joining_date":         {"type": "date", "source": "employee.joining_date"},

    # Signatory (latest APPROVED RequestApproval, with CompanyBranding fallback)
    "signatory_user":       {"type": "CustomUser", "source": "latest approved approval signatory"},
    "signatory_name":       {"type": "str", "source": "signatory.get_full_name()"},
    "signatory_title":      {"type": "str", "source": "signatory.designation.name"},
    "signatory_email":      {"type": "str", "source": "signatory.email"},
    "signatory_office_phone":{"type": "str","source": "signatory.office_phone (fallback empty)"},
    "signatory_mobile_phone":{"type": "str","source": "signatory.contact_number"},
    "signatory_signature":  {"type": "str", "source": "data URI of signatory.signature"},

    # Salary certificate dynamic fields
    "salary_month_1_name": {"type": "str", "source": "input_data/extra"},
    "salary_month_2_name": {"type": "str", "source": "input_data/extra"},
    "salary_month_3_name": {"type": "str", "source": "input_data/extra"},
    "salary_month_1_gross_salary": {"type": "str", "source": "input_data/extra"},
    "salary_month_2_gross_salary": {"type": "str", "source": "input_data/extra"},
    "salary_month_3_gross_salary": {"type": "str", "source": "input_data/extra"},
    "salary_month_1_incentive_lwp": {"type": "str", "source": "input_data/extra"},
    "salary_month_2_incentive_lwp": {"type": "str", "source": "input_data/extra"},
    "salary_month_3_incentive_lwp": {"type": "str", "source": "input_data/extra"},
    "salary_month_1_festival_bonus": {"type": "str", "source": "input_data/extra"},
    "salary_month_2_festival_bonus": {"type": "str", "source": "input_data/extra"},
    "salary_month_3_festival_bonus": {"type": "str", "source": "input_data/extra"},
    "salary_month_1_gross_payment": {"type": "str", "source": "input_data/extra"},
    "salary_month_2_gross_payment": {"type": "str", "source": "input_data/extra"},
    "salary_month_3_gross_payment": {"type": "str", "source": "input_data/extra"},
    "salary_month_1_salary_due_adj": {"type": "str", "source": "input_data/extra"},
    "salary_month_2_salary_due_adj": {"type": "str", "source": "input_data/extra"},
    "salary_month_3_salary_due_adj": {"type": "str", "source": "input_data/extra"},
    "salary_month_1_tada_others": {"type": "str", "source": "input_data/extra"},
    "salary_month_2_tada_others": {"type": "str", "source": "input_data/extra"},
    "salary_month_3_tada_others": {"type": "str", "source": "input_data/extra"},
    "salary_month_1_lunch_allowance": {"type": "str", "source": "input_data/extra"},
    "salary_month_2_lunch_allowance": {"type": "str", "source": "input_data/extra"},
    "salary_month_3_lunch_allowance": {"type": "str", "source": "input_data/extra"},
    "salary_month_1_tax": {"type": "str", "source": "input_data/extra"},
    "salary_month_2_tax": {"type": "str", "source": "input_data/extra"},
    "salary_month_3_tax": {"type": "str", "source": "input_data/extra"},
    "salary_month_1_net_take_home": {"type": "str", "source": "input_data/extra"},
    "salary_month_2_net_take_home": {"type": "str", "source": "input_data/extra"},
    "salary_month_3_net_take_home": {"type": "str", "source": "input_data/extra"},
    "bank_account_no": {"type": "str", "source": "input_data/extra"},
    "salary_months": {"type": "list[dict]", "source": "prepared salary extra fields"},
    "month": {"type": "dict", "source": "salary_months loop variable"},
    "bank_account_number": {"type": "str", "source": "input_data/extra or primary EmployeeBankAccount"},

    # `body_html` is only available inside the design template (injected after
    # the message template renders). Listed here so the validator doesn't warn
    # when designers reference it.
    "body_html":            {"type": "str", "source": "rendered MessageTemplate (design only)"},
}

for _idx in (1, 2, 3):
    for _key in (
        "month",
        "gross_salary",
        "incentive_lwp",
        "festival_bonus",
        "gross_payment",
        "salary_adjustment",
        "ta_da_others",
        "lunch_allowance",
        "tax",
        "net_take_home",
    ):
        PDF_CONTEXT_SCHEMA[f"salary_display_month_{_idx}_{_key}"] = {
            "type": "str",
            "source": "prepared salary display field",
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_public_base_url() -> str:
    return getattr(settings, "PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _fmt_mmddyyyy(d) -> str:
    if not d:
        return ""
    if hasattr(d, "date"):
        d = d.date()
    return d.strftime("%m/%d/%Y")


def _parse_salary_month(value: Any) -> tuple[date | None, str]:
    raw = str(value or "").strip()
    if not raw:
        return None, "-"
    for fmt in ("%b-%y", "%B-%y", "%b %y", "%B %y", "%b %Y", "%B %Y", "%Y-%m", "%m/%Y"):
        try:
            parsed = datetime.strptime(raw, fmt).date().replace(day=1)
            return parsed, parsed.strftime("%b-%y")
        except ValueError:
            continue
    return None, raw


def _fmt_salary_amount(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw == "-":
        return "-"

    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = raw.strip("()").replace(",", "").strip()
    if cleaned.startswith("-"):
        negative = True
        cleaned = cleaned[1:].strip()

    try:
        amount = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return raw

    if amount == 0:
        return "-"
    # Whole taka with thousands grouping — the app-wide rule (core/money.py,
    # docs/MONEY_FORMAT_PLAN.md); letters keep their accounting parentheses
    # for negatives.
    formatted = format_taka(abs(amount))
    return f"({formatted})" if negative or amount < 0 else formatted


def _salary_value(source: dict[str, Any], idx: int, component: str) -> str:
    return _fmt_salary_amount(source.get(f"salary_month_{idx}_{component}"))


def _build_salary_months(input_data: dict[str, Any], extra: dict[str, Any]) -> list[dict[str, str]]:
    source = {**input_data, **extra}
    months: list[dict[str, Any]] = []
    for idx in (1, 2, 3):
        sort_date, label = _parse_salary_month(source.get(f"salary_month_{idx}_name"))
        month = {
            "month": label,
            "gross_salary": _salary_value(source, idx, "gross_salary"),
            "incentive_lwp": _salary_value(source, idx, "incentive_lwp"),
            "festival_bonus": _salary_value(source, idx, "festival_bonus"),
            "gross_payment": _salary_value(source, idx, "gross_payment"),
            "salary_adjustment": _salary_value(source, idx, "salary_due_adj"),
            "ta_da_others": _salary_value(source, idx, "tada_others"),
            "lunch_allowance": _salary_value(source, idx, "lunch_allowance"),
            "tax": _salary_value(source, idx, "tax"),
            "net_take_home": _salary_value(source, idx, "net_take_home"),
            "_sort_date": sort_date,
            "_sort_order": idx,
        }
        if label != "-" or any(v != "-" for k, v in month.items() if not k.startswith("_") and k != "month"):
            months.append(month)

    months.sort(
        key=lambda item: (
            item["_sort_date"] is not None,
            item["_sort_date"] or date.min,
            -item["_sort_order"],
        ),
        reverse=True,
    )
    months = months[:3]
    while len(months) < 3:
        months.append({
            "month": "-",
            "gross_salary": "-",
            "incentive_lwp": "-",
            "festival_bonus": "-",
            "gross_payment": "-",
            "salary_adjustment": "-",
            "ta_da_others": "-",
            "lunch_allowance": "-",
            "tax": "-",
            "net_take_home": "-",
        })
    for month in months:
        month.pop("_sort_date", None)
        month.pop("_sort_order", None)
    return months


def _resolve_bank_account_number(employee, input_data: dict[str, Any], extra: dict[str, Any]) -> str:
    explicit = (
        input_data.get("bank_account_number")
        or input_data.get("bank_account_no")
        or extra.get("bank_account_number")
        or extra.get("bank_account_no")
    )
    if explicit:
        return str(explicit)
    try:
        account = employee.bank_accounts.filter(is_primary=True).first()
        if not account:
            account = employee.bank_accounts.order_by("-is_verified", "-updated_at").first()
        return account.account_number if account else ""
    except Exception:
        return ""


def _flatten_salary_months(salary_months: list[dict[str, str]]) -> dict[str, str]:
    flattened: dict[str, str] = {}
    keys = (
        "month",
        "gross_salary",
        "incentive_lwp",
        "festival_bonus",
        "gross_payment",
        "salary_adjustment",
        "ta_da_others",
        "lunch_allowance",
        "tax",
        "net_take_home",
    )
    for idx, month in enumerate(salary_months[:3], start=1):
        for key in keys:
            flattened[f"salary_display_month_{idx}_{key}"] = month.get(key) or "-"
    for idx in range(len(salary_months) + 1, 4):
        for key in keys:
            flattened[f"salary_display_month_{idx}_{key}"] = "-"
    return flattened


def _field_to_data_uri(file_field) -> str:
    """Read an ImageField/FileField and return a data: URI string. Empty
    string if the field is empty or unreadable.

    Goes through the field's storage (``open()``), never ``.path`` — the
    Google Drive backend (media_store) has no filesystem path.
    """
    if not file_field:
        return ""
    try:
        mime, _ = mimetypes.guess_type(file_field.name or "")
        mime = mime or "application/octet-stream"
        with file_field.open("rb") as f:
            data = f.read()
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except (FileNotFoundError, OSError, ValueError):
        return ""


def _absolute_file_data_uri(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    mime, _ = mimetypes.guess_type(path)
    mime = mime or "application/octet-stream"
    with open(path, "rb") as f:
        data = f.read()
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _make_qr_data_uri(url: str) -> str:
    """Generate a small PNG QR encoding the given URL; return as data: URI."""
    qr = qrcode.QRCode(version=1, box_size=4, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"


def _resolve_signatory(req):
    """Pick the user whose name + signature appear on the letter.

    Order: most-recent APPROVED ``RequestApproval.signatory`` → CompanyBranding
    default signatory. Returns ``None`` if neither resolves. For unsaved
    requests (test_render_pdf) the approval lookup is skipped.
    """
    from documents.models import RequestApproval, CompanyBranding

    if not req._state.adding:
        latest = (req.approvals
                  .filter(status=RequestApproval.Status.APPROVED,
                          signatory__isnull=False)
                  .order_by("-signed_at")
                  .select_related("signatory")
                  .first())
        if latest and latest.signatory:
            return latest.signatory
    return CompanyBranding.load().default_signatory


def _resolve_logo_data_uri() -> str:
    from documents.models import CompanyBranding

    branding = CompanyBranding.load()
    if branding.logo:
        uri = _field_to_data_uri(branding.logo)
        if uri:
            return uri
    fallback = getattr(settings, "DOCUMENTS_FALLBACK_LOGO", "")
    if fallback:
        return _absolute_file_data_uri(fallback)
    return ""


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def _build_context(req) -> dict[str, Any]:
    from documents.models import CompanyBranding, DocumentExtraFieldValue

    branding = CompanyBranding.load()
    employee = req.employee
    input_data = dict(req.input_data or {})

    today = timezone.localdate()
    letter_date_raw = input_data.get("letter_date") or today
    if isinstance(letter_date_raw, str):
        try:
            letter_date = date.fromisoformat(letter_date_raw)
        except ValueError:
            letter_date = today
    else:
        letter_date = letter_date_raw

    public_url = f"{_get_public_base_url()}/verify/{req.id}/"

    signatory = _resolve_signatory(req)

    # Skip the DB lookup for unsaved requests (test_render_pdf path) — they
    # carry their sample values in input_data and the form is bypassed.
    if req._state.adding:
        extra: dict[str, str] = {}
    else:
        extras_qs = (DocumentExtraFieldValue.objects
                     .filter(request=req, is_active=True)
                     .select_related("field_type"))
        extra = {efv.field_type.field_key: efv.value for efv in extras_qs}

    salary_months = _build_salary_months(input_data, extra)
    bank_account_number = _resolve_bank_account_number(employee, input_data, extra)

    ctx: dict[str, Any] = {
        "request": req,
        "employee": employee,
        "document_type": req.document_type,
        "input_data": input_data,
        "extra": extra,

        "today": today,
        "today_mmddyyyy": _fmt_mmddyyyy(today),
        "letter_date": letter_date,
        "letter_date_mmddyyyy": _fmt_mmddyyyy(letter_date),

        "public_url": public_url,
        "qr_data_uri": _make_qr_data_uri(public_url),

        "logo_url": _resolve_logo_data_uri(),
        "company_name": branding.company_name,
        "footer_company": branding.footer_company,
        "head_office_address": input_data.get("head_office_address") or branding.head_office_address,
        "branch_office_address": input_data.get("branch_office_address") or branding.branch_office_address,
        "jashore_office_address": input_data.get("jashore_office_address") or branding.jashore_office_address,
        "website": branding.website,
        "ref_no": input_data.get("ref_no") or f"{branding.default_ref_no_prefix}/{str(req.id)[:8]}",
        "subject": input_data.get("subject") or req.document_type.display_name,
        "salary_months": salary_months,
        "bank_account_number": bank_account_number,

        "department": getattr(getattr(employee, "department", None), "name", "") or "",
        "designation": getattr(getattr(employee, "designation", None), "name", "") or "",
        "work_location": getattr(getattr(employee, "work_location", None), "name", "") or "",
        "joining_date": getattr(employee, "joining_date", None),

        "signatory_user": signatory,
        "signatory_name": signatory.get_full_name() if signatory else "",
        "signatory_title": (
            getattr(getattr(signatory, "designation", None), "name", "")
            if signatory else ""
        ),
        "signatory_email": getattr(signatory, "email", "") if signatory else "",
        "signatory_office_phone": getattr(signatory, "office_phone", "") if signatory else "",
        "signatory_mobile_phone": getattr(signatory, "contact_number", "") if signatory else "",
        "signatory_signature": (
            _field_to_data_uri(getattr(signatory, "signature", None)) if signatory else ""
        ),
    }

    # Service Book (Bangladesh Labour Rules 2015/2022, Form-7) — only
    # computed for that one DocumentType, since (unlike salary_months above)
    # it runs several real queries across employees/leave/payroll/offboarding
    # rather than just reshaping input_data already in hand.
    if req.document_type.code == "SERVICE_BOOK":
        from employees.services import build_service_book_context
        ctx.update(build_service_book_context(employee))

    # Flatten input_data keys so templates can use {{ destination_country }}
    # directly. Existing keys (e.g. an `employee` form field) won't clobber
    # the structured context.
    for k, v in input_data.items():
        if k not in ctx:
            ctx[k] = v
    # Same for extra field values.
    for k, v in extra.items():
        if k not in ctx:
            ctx[k] = v

    for k, v in _flatten_salary_months(salary_months).items():
        ctx[k] = v

    return ctx


# ---------------------------------------------------------------------------
# Validation (parses {{ var }} tokens without rendering)
# ---------------------------------------------------------------------------

def validate_template_against_context(template_html: str, schema: dict) -> list[str]:
    """Return a list of warning strings for any ``{{ var }}`` token whose
    root identifier isn't in the schema. Non-blocking — HR templates may
    legitimately reference dynamic ``input_data`` / ``extra`` keys that
    can't be predicted statically."""
    if not template_html:
        return []
    warnings: list[str] = []
    try:
        lexer = Lexer(template_html)
        for token in lexer.tokenize():
            if token.token_type != TokenType.VAR:
                continue
            expr = token.contents.strip()
            # 'employee.get_full_name|upper' → 'employee'
            root = expr.split("|", 1)[0].split(".", 1)[0].strip()
            if not root or root in schema:
                continue
            warnings.append(f"Unknown template variable: {{{{ {expr} }}}}")
    except Exception as exc:  # malformed template — surface but don't crash
        warnings.append(f"Template parse error: {exc}")
    # Stable order, dedup
    return sorted(set(warnings))


# ---------------------------------------------------------------------------
# Render + attach
# ---------------------------------------------------------------------------

def render_request_to_pdf_bytes(req) -> bytes:
    """Render a DocumentRequest to PDF bytes. Raises if the DocumentType
    has no template_design or no message_template."""
    if not req.document_type.template_design:
        raise ValueError(f"DocumentType {req.document_type.code!r} has no template_design.")
    if not req.document_type.message_template:
        raise ValueError(f"DocumentType {req.document_type.code!r} has no message_template.")

    ctx = _build_context(req)
    engine = Engine.get_default()

    body_html = engine.from_string(req.document_type.message_template.body_html).render(
        Context(ctx)
    )

    design_ctx = {**ctx, "body_html": body_html}
    final_html = engine.from_string(req.document_type.template_design.html).render(
        Context(design_ctx)
    )

    base_url = _get_public_base_url()
    return HTML(string=final_html, base_url=base_url).write_pdf()


def generate_and_attach_pdf(request_id) -> None:
    """Render + save to ``DocumentRequest.generated_file``. Called by the
    signal handler after a request transitions to APPROVED."""
    from django.core.files.base import ContentFile
    from documents.models import DocumentRequest

    if isinstance(request_id, str):
        request_id = uuid.UUID(request_id)

    req = (DocumentRequest.objects
           .select_related("employee", "document_type",
                           "document_type__template_design",
                           "document_type__message_template")
           .get(pk=request_id))

    pdf_bytes = render_request_to_pdf_bytes(req)
    filename = f"{req.document_type.code}_{req.id}.pdf"
    req.generated_file.save(filename, ContentFile(pdf_bytes), save=True)
    logger.info("Generated PDF for DocumentRequest %s → %s", req.id, req.generated_file.name)


def test_render_pdf(doc_type, *, sample_input_data: dict | None = None,
                    employee=None) -> bytes:
    """Render a sample PDF without persisting a request. Used by the admin
    "Test render" action on DocumentTypeAdmin."""
    from documents.models import DocumentRequest, DocumentExtraFieldType

    sample_input_data = dict(sample_input_data or {})
    # Pre-fill any required extra fields with plausible defaults so the
    # render doesn't break on KeyError.
    for ft in DocumentExtraFieldType.objects.filter(document_type=doc_type, is_active=True):
        sample_input_data.setdefault(ft.field_key, _sample_value_for(ft))

    # In-memory unsaved request — never written to DB. ``_state.adding``
    # stays True, which both _resolve_signatory and _build_context use to
    # short-circuit DB lookups against the reverse managers.
    req = DocumentRequest(
        id=uuid.uuid4(),
        document_type=doc_type,
        employee=employee,
        status=DocumentRequest.Status.APPROVED,
        input_data=sample_input_data,
    )
    return render_request_to_pdf_bytes(req)


def _sample_value_for(field_type) -> str:
    """Plausible string for the 'Test render' admin action — never persisted."""
    from documents.models import DocumentExtraFieldType
    kind = field_type.kind
    if kind == DocumentExtraFieldType.FieldKind.BOOLEAN:
        return "true"
    if kind == DocumentExtraFieldType.FieldKind.NUMBER:
        return "0"
    if kind == DocumentExtraFieldType.FieldKind.DATE:
        return date.today().isoformat()
    if kind == DocumentExtraFieldType.FieldKind.CHOICE and field_type.choices:
        return str(field_type.choices[0])
    return f"<sample {field_type.field_key}>"
