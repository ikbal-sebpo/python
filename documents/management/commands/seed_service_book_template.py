"""Seed the Service Book DocumentType (Bangladesh Labour Rules 2015/2022,
Form-7) — idempotent, same update_or_create shape as seed_document_types.py.

This DocumentType is deliberately never used to create a DocumentRequest —
it exists purely as the template container the Service Book PDF view
(employees/views.py::ServiceBookPDFView) renders against, via the same
in-memory-unsaved-DocumentRequest technique documents.pdf.test_render_pdf()
uses for admin "test render". See
/home/smrashel/.claude/plans/vivid-honking-pillow.md for the full design.

Usage:
    python manage.py seed_service_book_template
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from documents.models import DocumentType, MessageTemplate, TemplateDesign

DESIGN_KEY = "design_service_book_a4"
MESSAGE_KEY = "msg_service_book_body"
DOC_TYPE_CODE = "SERVICE_BOOK"

# Started from the @page / body rules every other document uses
# (documents/management/commands/seed_document_types.py::_DESIGN_HTML) but
# deliberately WITHOUT that letterhead: no logo header, no blue footer band
# with the office addresses (2026-10-07). The Service Book is a statutory
# worker record (Form-7) — it gets printed and submitted to the Labour
# Department, and the employee carries the copy to their next employer — so
# the page is data only. The only running element is a "Page X of Y"
# counter in the bottom margin. The `.body` content is Service Book's own
# Part 1-5 markup, plus the dense table/heading/definition-list rules.
_DESIGN_HTML = """\
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Service Book · {{ employee.get_full_name }} · Orbis</title>

  <style>
    :root{
      --page-side: 18mm;
      --page-top: 14mm;
      --page-bottom: 16mm;
    }

    @page{
      size: A4;
      margin: var(--page-top) 0 var(--page-bottom) 0;

      @bottom-center{
        content: "Page " counter(page) " of " counter(pages);
        font-family: Arial, Helvetica, sans-serif;
        font-size: 8pt;
        color: #666;
      }
    }

    html, body{
      margin: 0;
      padding: 0;
      font-family: Arial, Helvetica, sans-serif;
      font-size: 12pt;
      line-height: 1.45;
      color: #111;
    }

    .page-content{
      padding: 0 var(--page-side);
      box-sizing: border-box;
      overflow-wrap: break-word;
      word-break: break-word;
    }

    .body{
      margin-top: 0;
    }

    .body *{
      max-width: 100%;
      box-sizing: border-box;
    }

    .body img{
      max-width: 100%;
      height: auto;
    }

    .body table{
      width: 100%;
      border-collapse: collapse;
    }

    /* Service Book-specific: dense report tables, not letter prose. */
    .body{ font-size: 9.5pt; line-height: 1.35; }
    .body table{ margin: 4px 0 14px 0; }
    /* Part 3 wage table has 14 columns: fixed layout + proportional widths so
       the signature columns never run past the page edge (2026-10-05). */
    .body table.wage{ table-layout: fixed; font-size: 8pt; }
    .body table.wage th, .body table.wage td{ padding: 2px 2px; overflow-wrap: break-word; }
    .body table.wage th{ font-size: 7.5pt; white-space: nowrap; }
    .body table.wage td:first-child, .body table.wage td:nth-child(2){ white-space: nowrap; }
    .body th{ text-align:left; padding:2px 4px; border-bottom:1px solid #999; }
    .body td{ padding:2px 4px; border-top:1px solid #eee; }
    .body h2{ font-size:11pt; text-transform:uppercase; letter-spacing:.03em; margin: 14px 0 6px 0; border-bottom: 2px solid #066F98; padding-bottom: 2px; }
    .body h2:first-child{ margin-top:0; }
    dl.sb-grid{ display:grid; grid-template-columns: 1fr 1fr 1fr; gap: 2px 16px; margin: 4px 0 10px 0; }
    dl.sb-grid dt{ font-size: 7.5pt; color:#666; text-transform:uppercase; }
    dl.sb-grid dd{ margin: 0 0 4px 0; }
  </style>
</head>

<body>
  <div class="page-content">
    <div class="body">
<p style="text-align:center; margin: 0 0 14px 0;">
  <strong style="font-size:13pt;"><u>SERVICE BOOK</u></strong><br>
  <span style="font-size:8pt; color:#666;">Bangladesh Labour Rules 2015, Form-7 (Section 7 / Rule 20), as amended 2022</span>
</p>

<h2>Part 1 — Identifying information</h2>
<div style="display:flex; align-items:flex-start; gap:12px;">
  <dl class="sb-grid" style="flex:1; margin:4px 0 10px 0;">
    <div><dt>Name</dt><dd>{{ employee.get_full_name }}</dd></div>
    <div><dt>Father's name</dt><dd>{{ sb_personal.father_name|default:"—" }}</dd></div>
    <div><dt>Mother's name</dt><dd>{{ sb_personal.mother_name|default:"—" }}</dd></div>
    <div><dt>Spouse's name</dt><dd>{{ sb_family.spouse_name|default:"—" }}</dd></div>
    <div><dt>Date of birth / Age</dt><dd>{{ sb_personal.date_of_birth|date:"Y-m-d"|default:"—" }}{% if sb_age %} (Age {{ sb_age }}){% endif %}</dd></div>
    {% for id_row in sb_identifications %}
    <div><dt>{{ id_row.get_id_type_display }}</dt><dd>{{ id_row.id_number }}</dd></div>
    {% empty %}
    <div><dt>National ID No.</dt><dd>—</dd></div>
    {% endfor %}
    <div><dt>Educational qualification</dt><dd>{% if sb_education %}{{ sb_education.get_degree_level_display }}{% if sb_education.degree_name and sb_education.degree_name != sb_education.get_degree_level_display %} ({{ sb_education.degree_name }}){% endif %}{% if sb_education.academic_institution %}, {{ sb_education.academic_institution }}{% endif %}{% if sb_education.passing_year %} ({{ sb_education.passing_year }}){% endif %}{% else %}—{% endif %}</dd></div>
    <div><dt>Training / special skill</dt><dd>{% for c in sb_certifications %}{{ c.certificate_name }}{% if c.certificate_year %} ({{ c.certificate_year }}){% endif %}{% if not forloop.last %}; {% endif %}{% empty %}—{% endfor %}</dd></div>
    <div><dt>Height</dt><dd>{% if sb_height_cm %}{{ sb_height_cm }} cm{% else %}—{% endif %}</dd></div>
    <div><dt>Blood group</dt><dd>{{ sb_personal.blood_group|default:"—" }}</dd></div>
    <div><dt>Identification mark</dt><dd>{{ sb_personal.identification_mark|default:"—" }}</dd></div>
    <div><dt>Date service book opened</dt><dd>{{ sb_service_book_opened|date:"Y-m-d"|default:"—" }}</dd></div>
  </dl>
  <div style="width:70px; text-align:center; flex-shrink:0;">
    {% if sb_photo_data_uri %}
    <img src="{{ sb_photo_data_uri }}" style="width:70px; height:90px; object-fit:cover; border:1px solid #999;" alt="">
    {% else %}
    <div style="width:70px; height:90px; border:1px dashed #999; font-size:7pt; color:#999; text-align:center; line-height:90px;">photo</div>
    {% endif %}
  </div>
</div>
<p style="font-size:8.5pt;">
  <strong>Permanent address:</strong> {{ sb_address_permanent|default:"—" }}<br>
  <strong>Present address:</strong> {{ sb_address_present|default:"—" }}
</p>
<p style="font-size:8.5pt; margin-top:14px;">
  <strong>Left thumb impression:</strong>
  <span style="display:inline-block; width:70px; height:50px; border:1px solid #999; vertical-align:middle; margin-left:6px;"></span>
  <em style="color:#888;">(to be taken on the printed copy)</em>
</p>
<table style="margin-top:10px;">
  <tbody>
    <tr>
      <td style="border-top:none; height:22px;"></td>
      <td style="border-top:none; height:22px;"></td>
    </tr>
    <tr>
      <td style="border-top:1px solid #999; font-size:7.5pt; color:#666;">Worker's signature</td>
      <td style="border-top:1px solid #999; font-size:7.5pt; color:#666;">Employer / management authority's signature</td>
    </tr>
  </tbody>
</table>

<h2>Part 2 — Employer &amp; employment</h2>
<table>
  <thead><tr><th>Establishment name &amp; address</th><th>Employer / mgmt. authority</th><th>Joining date</th><th>Job category</th><th>Leaving date</th><th>Type / reason of leaving</th><th>Employer sig.</th><th>Worker sig.</th></tr></thead>
  <tbody>
    <tr>
      <td>{{ sb_company.company_name }}{% if sb_company.head_office_address %}<br><span style="font-size:7.5pt; color:#666;">{{ sb_company.head_office_address }}</span>{% endif %}</td>
      <td>{{ sb_authorized_signatory.get_full_name|default:"—" }}</td>
      <td>{{ sb_employment.joining_date|date:"Y-m-d"|default:"—" }}</td>
      <td>N/A</td>
      <td>{{ sb_employment.last_working_date|date:"Y-m-d"|default:"—" }}</td>
      <td>{% if sb_employment.resignation %}{{ sb_employment.resignation.get_separation_type_display }} — {{ sb_employment.resignation.reason }}{% else %}—{% endif %}</td>
      <td></td>
      <td></td>
    </tr>
  </tbody>
</table>

<h2>Part 3 — Service record, wages &amp; allowances</h2>
<p style="font-size:8pt; margin:0 0 2px 0;"><strong>Position / post history</strong></p>
<table>
  <thead><tr><th>From</th><th>To</th><th>Job post</th><th>Card / register no.</th><th>Reason</th><th>Employer sig.</th><th>Worker sig.</th></tr></thead>
  <tbody>
    {% for row in sb_designation_history %}
    <tr><td>{{ row.effective_from|date:"Y-m-d" }}</td><td>{{ row.effective_to|date:"Y-m-d"|default:"present" }}</td><td>{{ row.designation }}</td><td>{{ sb_register_serial_no|default:"—" }}</td><td>{{ row.get_reason_display }}</td><td></td><td></td></tr>
    {% empty %}
    <tr><td colspan="7">No position history recorded yet.</td></tr>
    {% endfor %}
  </tbody>
</table>
<p style="font-size:8pt; margin:0 0 2px 0;"><strong>Monthly wage rate &amp; allowances</strong></p>
<table class="wage">
  <colgroup>
    <col style="width:10%"><col style="width:10%"><col style="width:5.5%"><col style="width:5.5%"><col style="width:7%"><col style="width:5.5%">
    <col style="width:8.5%"><col style="width:6%"><col style="width:8.5%"><col style="width:6%"><col style="width:5.5%"><col style="width:8.5%">
    <col style="width:6.75%"><col style="width:6.75%">
  </colgroup>
  <thead><tr><th>From</th><th>To</th><th>Basic</th><th>House<br>rent</th><th>Medical</th><th>Bonus</th><th>Total<br>(Tk)</th><th>Other<br>allow.</th><th>Deduction</th><th>Worker<br>PF</th><th>Empl.<br>PF</th><th>Reason</th><th>Empl.<br>sig.</th><th>Worker<br>sig.</th></tr></thead>
  <tbody>
    {% for row in sb_salary_history %}
    <tr><td>{{ row.effective_from|date:"Y-m-d" }}</td><td>{{ row.effective_to|date:"Y-m-d"|default:"present" }}</td><td>N/A</td><td>N/A</td><td>N/A</td><td>N/A</td><td><strong>{{ row.gross_monthly|taka }}</strong></td><td>N/A</td><td>N/A</td><td>N/A</td><td>N/A</td><td>{{ row.get_reason_display }}</td><td></td><td></td></tr>
    {% empty %}
    <tr><td colspan="14">No wage history recorded yet.</td></tr>
    {% endfor %}
  </tbody>
</table>
<p style="font-size:7.5pt; color:#888; margin-top:2px;">Orbis's compensation master tracks gross monthly pay only (no component breakdown) and no Provident Fund scheme — those columns are marked N/A.</p>

<h2>Part 4 — Record of leaves</h2>
{% if sb_al_legacy %}
<p style="font-size:8pt; margin:0 0 2px 0;"><strong>Annual Leave record before Orbis (HR records)</strong></p>
<table>
  <thead><tr><th>Year</th><th>Accrued to prior year-end</th><th>Taken in year</th><th>Year balance</th><th>Balance carried (60-day cap)</th><th>Note</th></tr></thead>
  <tbody>
    {% for rec in sb_al_legacy %}
    <tr><td>{{ rec.year }}</td><td>{{ rec.accrued_prior|floatformat:"-1" }}</td><td>{{ rec.taken|floatformat:"-1" }}</td><td>{{ rec.year_balance|floatformat:"-1" }}</td><td><strong>{{ rec.carried|floatformat:"-1" }}</strong></td><td>{{ rec.note|default:"—" }}</td></tr>
    {% endfor %}
  </tbody>
</table>
<p style="font-size:7.5pt; color:#888; margin-top:2px;">The first year's "Accrued to prior year-end" is the balance HR carried into Orbis; each later year starts from the previous year's carried balance. "Taken in year" and "Year balance" come from Orbis ({{ sb_al_legacy.0.entitlement|floatformat:"-1" }} days' entitlement a year); the carried balance is capped at 60 days.</p>
{% endif %}
{# "Annual Leave availed (Orbis)" table disabled 2026-10-05 at HR's request — see employees/services.py. #}
<p style="font-size:8pt; margin:8px 0 2px 0;"><strong>Annual Leave encashment</strong></p>
<table>
  <thead><tr><th>Total (days)</th><th>Date</th><th>Remaining balance</th><th>Employer sig.</th><th>Worker sig.</th></tr></thead>
  <tbody>
    {% for row in sb_al_encashment %}
    <tr><td>{{ row.total_days }}</td><td>{{ row.date|date:"Y-m-d" }}</td><td><strong>{{ row.remaining }}</strong></td><td></td><td></td></tr>
    {% empty %}
    <tr><td colspan="5">No Annual Leave encashed on file.</td></tr>
    {% endfor %}
  </tbody>
</table>

<h2>Part 5 — Record of conduct</h2>
<table>
  <thead><tr><th>Date</th><th>Description</th><th>Employer / authorised person's signature</th><th>Worker's signature / thumb impression</th></tr></thead>
  <tbody>
    {% for entry in sb_conduct %}
    <tr><td>{{ entry.processed_at|date:"Y-m-d"|default:"—" }}</td><td>{{ entry.component.name }} — {{ entry.reason }}</td><td>{{ entry.processed_by.get_full_name|default:"—" }}</td><td></td></tr>
    {% empty %}
    <tr><td colspan="4">No proven fines or commendations on file.</td></tr>
    {% endfor %}
  </tbody>
</table>

{% if body_html %}<div style="margin-top:10px; font-size:8.5pt;">{{ body_html|safe }}</div>{% endif %}
    </div>
  </div>
</body>
</html>
"""

# Kept deliberately empty: every field lives in _DESIGN_HTML above (unsanitized,
# skips bleach). A `{% for %}`/`{% empty %}` sitting as bare text directly
# inside <table>/<tbody> (i.e. not inside a <td>) gets relocated by html5lib's
# foster-parenting rule during MessageTemplate's bleach-sanitize round-trip —
# duplicating the loop body next to the {% empty %} branch instead of
# choosing one. TemplateDesign skips sanitisation entirely, so all the
# {% for %} table loops belong there, not here. See this command's design
# for the one-off reasoning; a document type needing genuinely free-text HR
# wording (unlike this always-derived record) would keep using body_html.
_BODY_HTML = ""


class Command(BaseCommand):
    help = "Seed the Service Book DocumentType (Form-7) — idempotent."

    def _seed_template_design(self, *, key, name, html):
        obj, made = TemplateDesign.objects.get_or_create(
            key=key, defaults={"name": name, "html": html, "is_active": True},
        )
        if not made and obj.html != html:
            obj.html = html
            obj.save(update_fields=["html", "updated_at"])
        self.stdout.write(f"design: {key} {'created' if made else 'exists'} (v{obj.version})")
        return obj

    def _seed_message_template(self, *, key, name, body_html):
        obj, made = MessageTemplate.objects.get_or_create(
            key=key, defaults={"name": name, "body_html": body_html, "is_active": True},
        )
        if not made and obj.body_html != body_html:
            obj.body_html = body_html
            obj.save(update_fields=["body_html", "updated_at"])
        self.stdout.write(f"message: {key} {'created' if made else 'exists'} (v{obj.version})")
        return obj

    @transaction.atomic
    def handle(self, *args, **opts):
        design = self._seed_template_design(
            key=DESIGN_KEY, name="Service Book (A4)", html=_DESIGN_HTML,
        )
        body = self._seed_message_template(
            key=MESSAGE_KEY, name="Service Book body", body_html=_BODY_HTML,
        )
        dt, made = DocumentType.objects.get_or_create(
            code=DOC_TYPE_CODE,
            defaults={
                "name": "service_book",
                "display_name": "Service Book",
                "description": (
                    "Bangladesh Labour Rules 2015/2022 Form-7. Rendered on demand "
                    "from live HR data — never issued as a DocumentRequest, no "
                    "approval chain (see employees/views.py::ServiceBookPDFView)."
                ),
                "template_design": design,
                "message_template": body,
                "requires_approval": False,
                "auto_approve": False,
                "is_active": True,
            },
        )
        if not made:
            updates = []
            for field, value in (
                ("template_design", design), ("message_template", body), ("is_active", True),
            ):
                if getattr(dt, field) != value:
                    setattr(dt, field, value)
                    updates.append(field)
            if updates:
                dt.save(update_fields=[*updates, "updated_at"])
        self.stdout.write(self.style.SUCCESS(
            f"doctype: {DOC_TYPE_CODE} {'created' if made else 'exists'}"
        ))
