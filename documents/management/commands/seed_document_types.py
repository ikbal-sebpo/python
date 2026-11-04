"""Seed starter rows for the documents app.

Idempotent: re-runs touch nothing once the starter rows exist.

Creates:
* the CompanyBranding singleton
* reusable template designs
* reusable message templates
* starter document types
* document review/signatory configs where available
* sample extra-field definitions for the NOC flow

Depends on ``seed_orbis``-created groups: HR and Senior Leadership must exist
for the review/signatory configs. The core templates and document types still
seed even when those groups are missing.
"""

from __future__ import annotations

from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction

from documents.models import (
    CompanyBranding,
    DocumentExtraFieldType,
    DocumentReviewConfig,
    DocumentSignatoryConfig,
    DocumentType,
    MessageTemplate,
    TemplateDesign,
)


DESIGN_KEY = "design_default_a4"
SHORT_KEY = "design_short_letter"
MESSAGE_KEY = "msg_noc_body"
SALARY_KEY = "msg_salary_cert_body"
EMP_VER_KEY = "msg_emp_verification_body"
DOC_TYPE_CODE = "NOC"

SALARY_AUTO_FIELD_KEYS = ("ref_no",)

SALARY_EXTRA_FIELDS = (
    {"field_key": "letter_date", "field_name": "Letter Date",
     "kind": DocumentExtraFieldType.FieldKind.DATE,
     "is_required": False, "sort_order": 2,
     "help_text": "Override the default (today) — useful for backdating."},
    {"field_key": "salary_month_1_name", "field_name": "Month 1 Name",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": True, "sort_order": 10},
    {"field_key": "salary_month_2_name", "field_name": "Month 2 Name",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": True, "sort_order": 11},
    {"field_key": "salary_month_3_name", "field_name": "Month 3 Name",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": True, "sort_order": 12},
    {"field_key": "salary_month_1_gross_salary", "field_name": "Month 1 Gross Salary",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": True, "sort_order": 20},
    {"field_key": "salary_month_2_gross_salary", "field_name": "Month 2 Gross Salary",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": True, "sort_order": 21},
    {"field_key": "salary_month_3_gross_salary", "field_name": "Month 3 Gross Salary",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": True, "sort_order": 22},
    {"field_key": "salary_month_1_incentive_lwp", "field_name": "Month 1 Incentive (LWP)",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 30},
    {"field_key": "salary_month_2_incentive_lwp", "field_name": "Month 2 Incentive (LWP)",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 31},
    {"field_key": "salary_month_3_incentive_lwp", "field_name": "Month 3 Incentive (LWP)",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 32},
    {"field_key": "salary_month_1_festival_bonus", "field_name": "Month 1 Festival Bonus",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 40},
    {"field_key": "salary_month_2_festival_bonus", "field_name": "Month 2 Festival Bonus",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 41},
    {"field_key": "salary_month_3_festival_bonus", "field_name": "Month 3 Festival Bonus",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 42},
    {"field_key": "salary_month_1_gross_payment", "field_name": "Month 1 Gross Payment",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": True, "sort_order": 50},
    {"field_key": "salary_month_2_gross_payment", "field_name": "Month 2 Gross Payment",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": True, "sort_order": 51},
    {"field_key": "salary_month_3_gross_payment", "field_name": "Month 3 Gross Payment",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": True, "sort_order": 52},
    {"field_key": "salary_month_1_salary_due_adj", "field_name": "Month 1 Salary Due/ Adj.",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 60},
    {"field_key": "salary_month_2_salary_due_adj", "field_name": "Month 2 Salary Due/ Adj.",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 61},
    {"field_key": "salary_month_3_salary_due_adj", "field_name": "Month 3 Salary Due/ Adj.",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 62},
    {"field_key": "salary_month_1_tada_others", "field_name": "Month 1 TA/DA & others",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 70},
    {"field_key": "salary_month_2_tada_others", "field_name": "Month 2 TA/DA & others",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 71},
    {"field_key": "salary_month_3_tada_others", "field_name": "Month 3 TA/DA & others",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 72},
    {"field_key": "salary_month_1_lunch_allowance", "field_name": "Month 1 Lunch Allowance",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 80},
    {"field_key": "salary_month_2_lunch_allowance", "field_name": "Month 2 Lunch Allowance",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 81},
    {"field_key": "salary_month_3_lunch_allowance", "field_name": "Month 3 Lunch Allowance",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 82},
    {"field_key": "salary_month_1_tax", "field_name": "Month 1 Tax",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 90},
    {"field_key": "salary_month_2_tax", "field_name": "Month 2 Tax",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 91},
    {"field_key": "salary_month_3_tax", "field_name": "Month 3 Tax",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 92},
    {"field_key": "salary_month_1_net_take_home", "field_name": "Month 1 Net Take-home Salary",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": True, "sort_order": 100},
    {"field_key": "salary_month_2_net_take_home", "field_name": "Month 2 Net Take-home Salary",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": True, "sort_order": 101},
    {"field_key": "salary_month_3_net_take_home", "field_name": "Month 3 Net Take-home Salary",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": True, "sort_order": 102},
    {"field_key": "bank_account_no", "field_name": "Bank Account No.",
     "kind": DocumentExtraFieldType.FieldKind.TEXT,
     "is_required": False, "sort_order": 110},
)


_DESIGN_HTML = """\
<!doctype html>
<html>
<head>
  <meta charset="utf-8">

  <style>
    :root{
      --page-side: 18mm;
      --page-top: 18mm;
      --footer-height: 24mm;
      --footer-bg: #066F98;
      --body-gap: 6mm;
    }

    @page{
      size: A4;
      margin: var(--page-top) 0 var(--footer-height) 0;

      @bottom-center{
        content: element(page-footer);
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
      padding-bottom: 30mm;
      box-sizing: border-box;
      overflow-wrap: break-word;
      word-break: break-word;
    }

    .header{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 14px;
    }

    .qr-wrap{ text-align: left; }

    .qr{
      width: 82px;
      height: 82px;
      border: 1px solid #ddd;
      padding: 4px;
      background: #fff;
      object-fit: contain;
    }

    .qr-label{
      font-size: 9pt;
      color: #444;
      margin-top: 2px;
    }

    .logo-wrap{ text-align: right; }

    .logo{
      height: 46px;
      width: auto;
      object-fit: contain;
    }

    .body{
      margin-top: 6px;
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

    #page-footer{
      position: running(page-footer);
      height: var(--footer-height);
      width: 100%;
      background: var(--footer-bg);
      color: #fff;
      padding: 5mm var(--page-side);
      box-sizing: border-box;
      font-size: 8.5pt;
      line-height: 1.25;
      overflow: hidden;
    }

    .footer-site{
      font-weight: 800;
      letter-spacing: 0.3px;
      margin-bottom: 2mm;
      text-transform: uppercase;
    }

    .footer-line{ margin-top: 1mm; }
    .footer-label{ font-weight: 700; }
  </style>
</head>

<body>
  <div class="page-content">
    <div class="header">
      <div class="qr-wrap">
        <img class="qr" src="{{ qr_data_uri }}" alt="QR">
        <div class="qr-label">Scan to verify</div>
      </div>

      <div class="logo-wrap">
        {% if logo_url %}
          <img class="logo" src="{{ logo_url }}" alt="Logo">
        {% else %}
          <strong>{{ company_name }}</strong>
        {% endif %}
      </div>
    </div>

    <div class="body">
      {{ body_html|safe }}
    </div>
  </div>

  <div id="page-footer">
    <div class="footer-site">{{ website|default:"sebpo.com" }}</div>

    <div class="footer-line">
      <span class="footer-label">Corporate Head Office:</span>
      {{ head_office_address|default:"Monem Business District, Level 7, 111 Bir Uttam C.R. Dutta Road (Sonargaon Road), Dhaka 1205, Bangladesh. Phone: +88 (096) 0622-1176" }}
    </div>

    <div class="footer-line">
      <span class="footer-label">Branch Office:</span>
      {{ branch_office_address|default:"8 AbbasGarden, DOHS Mohakhali, Dhaka 1206, Bangladesh. Phone: +88 (096) 06221100" }}
    </div>
  </div>
</body>
</html>
"""

_SHORT_DESIGN_HTML = _DESIGN_HTML

_TOP_ROW_HTML = """\
<table width="100%" style="border-collapse:collapse; margin-bottom:14px;">
  <tr>
    <td style="text-align:left;">
      <strong>{{ ref_no }}</strong>
    </td>
    <td style="text-align:right;">
      <strong>DATE:</strong>
      {{ letter_date|default:today|date:"m/d/Y" }}
    </td>
  </tr>
</table>
"""

_SALARY_TOP_ROW_HTML = """\
<table width="100%" style="border-collapse:collapse; margin-bottom:18px;">
  <tr>
    <td style="text-align:left; width:55%;">
      {{ ref_no }}
    </td>
    <td style="text-align:right; width:45%;">
      <u>{{ letter_date|default:today|date:"F d, Y" }}</u>
    </td>
  </tr>
</table>
"""

_SIGNATURE_HTML = """\
<p style="margin-top:18px;">
  {% if signatory_signature %}
    <img src="{{ signatory_signature }}" alt="Signature"
         style="height:65px; width:auto; display:block; margin-bottom:6px;">
  {% else %}
    <span style="display:block; height:55px;"></span>
  {% endif %}

  _______________________________<br>
  <strong>{{ signatory_name|default:"ABUBAKAR SIDDIQUE, SHRM-SCP" }}</strong><br>
  {{ signatory_title|default:"SENIOR MANAGER - HUMAN RESOURCES" }}<br>
  {{ company_name }}<br>
  Office: {{ signatory_office_phone|default:"+880 9606221814" }}<br>
  Mobile: {{ signatory_mobile_phone|default:"+880 1912609923" }}<br>
  Email: {{ signatory_email|default:"abubakar.siddique@sebpo.com" }}
</p>
"""

_SALARY_SIGNATURE_HTML = """\
<p style="margin-top:10px; line-height:1.18;">
  {% if signatory_signature %}
    <img src="{{ signatory_signature }}" alt="Signature"
         style="height:48px; width:auto; display:block; margin-bottom:4px;">
  {% else %}
    <span style="display:block; height:38px;"></span>
  {% endif %}

  _______________________________<br>
  Signature<br>
  <strong>Name: {{ signatory_name|default:"Mohammad Arif Karim Chowdhury" }}</strong><br>
  Designation: {{ signatory_title|default:"Manager" }}<br>
  Phone: {{ signatory_office_phone|default:"09606221820/ +880-2-9842396 (Ext: 113)" }}
  Cell: {{ signatory_mobile_phone|default:"+88 01717674802" }}<br>
  Email: {{ signatory_email|default:"arif.karim@sebpo.com" }}
</p>
"""


_NOC_BODY_HTML = _TOP_ROW_HTML + """\
<p style="text-align:center; margin:18px 0;">
  <strong><u>NO OBJECTION CERTIFICATE</u></strong>
</p>

<p>This is to certify that <strong>{{ employee.get_full_name }}</strong>
{% if employee.employee_id %}(Employee ID: {{ employee.employee_id }}){% endif %}
is employed with {{ company_name }} as <strong>{{ designation }}</strong>
in the <strong>{{ department }}</strong> department
{% if joining_date %}since {{ joining_date|date:"F j, Y" }}{% endif %}.</p>

<p>The company has <strong>no objection</strong> to
{{ employee.get_full_name }}'s travel{% if destination_country %} to <strong>{{ destination_country }}</strong>{% endif %}
{% if purpose %}for the purpose of {{ purpose }}{% endif %}.</p>

<p>This certificate is issued upon the request of the employee for whatever
purpose it may serve.</p>
""" + _SIGNATURE_HTML


_EXP_LETTER_BODY_HTML = _TOP_ROW_HTML + """\
<p style="text-align:center; margin:18px 0;">
  <strong><u>EXPERIENCE CERTIFICATE</u></strong>
</p>

<p>This is to certify that <strong>{{ employee.get_full_name }}</strong>
{% if employee.employee_id %}(Employee ID: {{ employee.employee_id }}){% endif %}
was employed with {{ company_name }} as <strong>{{ designation }}</strong>
in the <strong>{{ department }}</strong> department
{% if joining_date %}from {{ joining_date|date:"F j, Y" }}{% endif %}
{% if last_working_date %}to {{ last_working_date }}{% endif %}.</p>

<p>During the tenure of service, {{ employee.get_full_name }} demonstrated
professionalism, sincerity, and dedication to assigned responsibilities.
We wish them every success in their future endeavours.</p>

<p>This certificate is issued upon the request of the employee.</p>
""" + _SIGNATURE_HTML

_SALARY_CERT_BODY_HTML = """\
<div style="font-size:10pt; line-height:1.24;">
""" + _SALARY_TOP_ROW_HTML + """\
<p style="text-align:center; margin:10px 0 12px 0;">
  <strong><u>SALARY CERTIFICATE</u></strong>
</p>

<p style="margin:0 0 8px 0;">This is to certify that {{ employee.get_full_name }}
{% if employee.employee_id %}(Employee ID: {{ employee.employee_id }}){% endif %}
has been working as {{ designation|default:"" }} in the {{ department|default:"" }}
department at {{ company_name }}.</p>

{% if joining_date %}
<p style="margin:0 0 8px 0;">He/She is a permanent employee of the company since {{ joining_date|date:"F j, Y" }}.</p>
{% endif %}

<p style="margin:12px 0 5px 0;"><strong>His/Her salary has been paid in the last three months as follows:</strong></p>

<table style="width:100%; border-collapse:collapse; margin:4px 0 12px 0; font-size:9.2pt; line-height:1.14;">
  <thead>
    <tr>
      <th style="text-align:left; width:34%; padding:2px 4px;">Particulars</th>
      <th style="text-align:center; width:22%; padding:2px 4px;">{{ salary_display_month_1_month }}</th>
      <th style="text-align:center; width:22%; padding:2px 4px;">{{ salary_display_month_2_month }}</th>
      <th style="text-align:center; width:22%; padding:2px 4px;">{{ salary_display_month_3_month }}</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td style="padding:1px 4px;">Gross Salary</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_1_gross_salary }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_2_gross_salary }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_3_gross_salary }}</td>
    </tr>
    <tr>
      <td style="padding:1px 4px;">Incentive / LWP</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_1_incentive_lwp }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_2_incentive_lwp }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_3_incentive_lwp }}</td>
    </tr>
    <tr>
      <td style="padding:1px 4px;">Festival Bonus</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_1_festival_bonus }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_2_festival_bonus }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_3_festival_bonus }}</td>
    </tr>
    <tr>
      <td style="padding:2px 4px;"><strong>Gross Payment</strong></td>
      <td style="text-align:right; padding:2px 4px; border-top:1px solid #111;"><strong>{{ salary_display_month_1_gross_payment }}</strong></td>
      <td style="text-align:right; padding:2px 4px; border-top:1px solid #111;"><strong>{{ salary_display_month_2_gross_payment }}</strong></td>
      <td style="text-align:right; padding:2px 4px; border-top:1px solid #111;"><strong>{{ salary_display_month_3_gross_payment }}</strong></td>
    </tr>
    <tr><td colspan="4" style="height:6px;"></td></tr>
    <tr>
      <td style="padding:1px 4px;"><strong><u>Add/( Less):</u></strong></td>
      <td></td><td></td><td></td>
    </tr>
    <tr>
      <td style="padding:1px 4px;">Salary Due / Adjustment</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_1_salary_adjustment }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_2_salary_adjustment }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_3_salary_adjustment }}</td>
    </tr>
    <tr>
      <td style="padding:1px 4px;">TA/DA &amp; others</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_1_ta_da_others }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_2_ta_da_others }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_3_ta_da_others }}</td>
    </tr>
    <tr>
      <td style="padding:1px 4px;">Lunch Allowance</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_1_lunch_allowance }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_2_lunch_allowance }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_3_lunch_allowance }}</td>
    </tr>
    <tr>
      <td style="padding:1px 4px;">Tax</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_1_tax }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_2_tax }}</td>
      <td style="text-align:right; padding:1px 4px;">{{ salary_display_month_3_tax }}</td>
    </tr>
    <tr>
      <td style="padding:2px 4px;"><strong>Net Take-home Salary</strong></td>
      <td style="text-align:right; padding:2px 4px; border-top:1px solid #111; border-bottom:3px double #111;"><strong>{{ salary_display_month_1_net_take_home }}</strong></td>
      <td style="text-align:right; padding:2px 4px; border-top:1px solid #111; border-bottom:3px double #111;"><strong>{{ salary_display_month_2_net_take_home }}</strong></td>
      <td style="text-align:right; padding:2px 4px; border-top:1px solid #111; border-bottom:3px double #111;"><strong>{{ salary_display_month_3_net_take_home }}</strong></td>
    </tr>
  </tbody>
</table>

{% if bank_account_number %}
<p style="margin:0 0 8px 0;"><strong>Payment Summary:</strong><br>
<em>The most recent payment was transferred through bank account no.: {{ bank_account_number }}</em></p>
{% endif %}

<p style="margin:10px 0 5px 0;"><strong><u>Declaration</u></strong></p>
<p style="margin:0 0 8px 0;">I/ we declare that the above information regarding the said employee cited
by me/us is true, complete and accurate. Any loss or liability arising from the
issuance of this letter will not bind the company or any of its officers.</p>

<p style="margin:0 0 8px 0;">Thanking You,<br>
For {{ company_name }}.</p>
""" + _SALARY_SIGNATURE_HTML + """
</div>
"""

_EMP_VERIFICATION_BODY_HTML = _TOP_ROW_HTML + """\
<p style="text-align:center; margin:18px 0;">
  <strong><u>EMPLOYMENT VERIFICATION</u></strong>
</p>

<p>This is to verify that <strong>{{ employee.get_full_name }}</strong>
{% if employee.employee_id %}(Employee ID: {{ employee.employee_id }}){% endif %}
is employed by {{ company_name }} as <strong>{{ designation }}</strong>
in the <strong>{{ department }}</strong> department.</p>

<p>Please contact HR for any additional verification needs.</p>
""" + _SIGNATURE_HTML


_RELEASE_LETTER_BODY_HTML = _TOP_ROW_HTML + """\
<p style="text-align:center; margin:18px 0;">
  <strong><u>RELEASE LETTER</u></strong>
</p>

<p>This is to confirm that <strong>{{ employee.get_full_name }}</strong>
{% if employee.employee_id %}(Employee ID: {{ employee.employee_id }}){% endif %},
who served {{ company_name }} as <strong>{{ designation }}</strong>
in the <strong>{{ department }}</strong> department, has been formally
released from all employment obligations
{% if last_working_date %}as of {{ last_working_date }}{% endif %}.</p>

<p>All clearance formalities (asset return, finance settlement, exit
interview) have been completed satisfactorily. {{ company_name }} has no
outstanding claims against {{ employee.get_full_name }} as of the release date.</p>

<p>We wish them every success in their future endeavours.</p>
""" + _SIGNATURE_HTML


class Command(BaseCommand):
    help = "Seed CompanyBranding + the NOC starter DocumentType (idempotent)."

    def _seed_template_design(self, *, key, name, html):
        updates = []
        obj, made = TemplateDesign.objects.get_or_create(
            key=key,
            defaults={
                "name": name,
                "html": html,
                "is_active": True,
            },
        )
        if not made:
            for field, value in (("name", name), ("html", html), ("is_active", True)):
                if getattr(obj, field) != value:
                    setattr(obj, field, value)
                    updates.append(field)
            if updates:
                updates.append("updated_at")
                obj.save(update_fields=updates)
        self.stdout.write(f"design: {key} {'created' if made else 'updated' if updates else 'exists'} (v{obj.version})")
        return obj

    def _seed_message_template(self, *, key, name, body_html):
        updates = []
        obj, made = MessageTemplate.objects.get_or_create(
            key=key,
            defaults={
                "name": name,
                "body_html": body_html,
                "is_active": True,
            },
        )
        if not made:
            for field, value in (("name", name), ("body_html", body_html), ("is_active", True)):
                if getattr(obj, field) != value:
                    setattr(obj, field, value)
                    updates.append(field)
            if updates:
                updates.append("updated_at")
                obj.save(update_fields=updates)
        self.stdout.write(f"message: {key} {'created' if made else 'updated' if updates else 'exists'} (v{obj.version})")
        return obj

    def _seed_document_type(self, *, code, name, display_name, description,
                            design, body, requires_approval=True,
                            auto_approve=False, auto_approve_reason=""):
        defaults = {
            "name": name,
            "display_name": display_name,
            "description": description,
            "template_design": design,
            "message_template": body,
            "requires_approval": requires_approval,
            "auto_approve": auto_approve,
            "auto_approve_reason": auto_approve_reason,
            "is_active": True,
        }
        dt, made = DocumentType.objects.get_or_create(code=code, defaults=defaults)
        if not made:
            updates = []
            for field, value in (
                ("name", name),
                ("display_name", display_name),
                ("description", description),
                ("template_design", design),
                ("message_template", body),
                ("requires_approval", requires_approval),
                ("auto_approve", auto_approve),
                ("auto_approve_reason", auto_approve_reason),
                ("is_active", True),
            ):
                if getattr(dt, field) != value:
                    setattr(dt, field, value)
                    updates.append(field)
            if updates:
                updates.append("updated_at")
                dt.save(update_fields=updates)
        self.stdout.write(f"doctype: {code} {'created' if made else 'exists'}")
        return dt

    @transaction.atomic
    def handle(self, *args, **opts):
        # 1) Singleton branding row
        branding = CompanyBranding.load()
        branding.company_name = "SEBPO"
        branding.footer_company = "ServicEngine Ltd."
        branding.website = "sebpo.com"
        branding.default_ref_no_prefix = "SE/HRD"
        branding.head_office_address = (
            "Monem Business District, Level 7, 111 Bir Uttam C.R. Dutta Road "
            "(Sonargaon Road), Dhaka 1205, Bangladesh. Phone: +88 (096) 0622-1176"
        )
        branding.branch_office_address = (
            "8 AbbasGarden, DOHS Mohakhali, Dhaka 1206, Bangladesh. "
            "Phone: +88 (096) 06221100"
        )
        branding.jashore_office_address = (
            "Sheikh Hasina Software Technology Park, 9th Floor, Nazir "
            "Songkorpur Road, Adjacent to Jashore Passport Office, "
            "Jashore-7400, Khulna."
        )
        branding.save(update_fields=[
            "company_name", "footer_company", "website",
            "default_ref_no_prefix", "head_office_address",
            "branch_office_address", "jashore_office_address", "updated_at",
        ])
        self.stdout.write(f"branding: ensured (company={branding.company_name})")

        # 2) Reusable template designs
        design = self._seed_template_design(
            key=DESIGN_KEY, name="Default A4 Letter", html=_DESIGN_HTML,
        )
        short_design = self._seed_template_design(
            key=SHORT_KEY, name="Short Letter", html=_SHORT_DESIGN_HTML,
        )

        # 3) Reusable message templates
        noc_body = self._seed_message_template(
            key=MESSAGE_KEY, name="NOC Body", body_html=_NOC_BODY_HTML,
        )
        salary_body = self._seed_message_template(
            key=SALARY_KEY, name="Salary Certificate Body",
            body_html=_SALARY_CERT_BODY_HTML,
        )
        emp_ver_body = self._seed_message_template(
            key=EMP_VER_KEY, name="Employment Verification Body",
            body_html=_EMP_VERIFICATION_BODY_HTML,
        )

        # 4) Starter document types
        starter_types = [
            (
                DOC_TYPE_CODE,
                "noc",
                "No Objection Certificate",
                "Issued for visa applications, bank loans, etc.",
                design,
                noc_body,
                True,
                False,
                "",
            ),
            (
                "EXP_LETTER",
                "experience_letter",
                "Experience Certificate",
                "Issued on completion of employment.",
                short_design,
                self._seed_message_template(
                    key="msg_experience_letter_body",
                    name="Experience Certificate Body",
                    body_html=_EXP_LETTER_BODY_HTML,
                ),
                True,
                False,
                "",
            ),
            (
                "RELEASE_LETTER",
                "release_letter",
                "Release Letter",
                "Confirms all clearance formalities are settled.",
                short_design,
                self._seed_message_template(
                    key="msg_release_letter_body",
                    name="Release Letter Body",
                    body_html=_RELEASE_LETTER_BODY_HTML,
                ),
                True,
                False,
                "",
            ),
            (
                "SALARY_CERT",
                "salary_certificate",
                "Salary Certificate",
                "Confirms the employee's current salary for external use.",
                short_design,
                salary_body,
                True,
                False,
                "",
            ),
            (
                "EMP_VERIFICATION",
                "employment_verification",
                "Employment Verification",
                "Confirms employment details for banks, embassies, and other institutions.",
                short_design,
                emp_ver_body,
                True,
                False,
                "",
            ),
        ]

        for code, name, display_name, description, design_obj, body_obj, requires_approval, auto_approve, auto_approve_reason in starter_types:
            doc_type = self._seed_document_type(
                code=code,
                name=name,
                display_name=display_name,
                description=description,
                design=design_obj,
                body=body_obj,
                requires_approval=requires_approval,
                auto_approve=auto_approve,
                auto_approve_reason=auto_approve_reason,
            )

        # 5) Review + signatory configs (require seed_orbis to have created groups)
        try:
            hr = Group.objects.get(name="HR")
        except Group.DoesNotExist:
            self.stdout.write(self.style.WARNING(
                "skip review config: 'HR' group missing — run seed_orbis first."
            ))
        else:
            for code, *_ in starter_types:
                dt = DocumentType.objects.get(code=code)
                DocumentReviewConfig.objects.update_or_create(
                    document_type=dt, reviewer_group=hr,
                    defaults={"review_order": 1},
                )
            self.stdout.write("review config: HR (order 1) for starter document types")

        try:
            senior = Group.objects.get(name="Senior Leadership")
        except Group.DoesNotExist:
            self.stdout.write(self.style.WARNING(
                "skip signatory config: 'Senior Leadership' group missing — run seed_orbis first."
            ))
        else:
            for code, *_ in starter_types:
                dt = DocumentType.objects.get(code=code)
                DocumentSignatoryConfig.objects.update_or_create(
                    document_type=dt, signatory_group=senior,
                    defaults={"signature_order": 1},
                )
            self.stdout.write("signatory config: Senior Leadership (order 1) for starter document types")

        # 6) Sample extra fields (NOC)
        noc_type = DocumentType.objects.get(code=DOC_TYPE_CODE)
        for spec in (
            {"field_key": "destination_country", "field_name": "Destination Country",
             "kind": DocumentExtraFieldType.FieldKind.TEXT,
             "is_required": True, "sort_order": 1},
            {"field_key": "purpose", "field_name": "Purpose",
             "kind": DocumentExtraFieldType.FieldKind.TEXTAREA,
             "is_required": True, "sort_order": 2},
            {"field_key": "letter_date", "field_name": "Letter Date",
             "kind": DocumentExtraFieldType.FieldKind.DATE,
             "is_required": False, "sort_order": 3,
             "help_text": "Override the default (today) — useful for backdating."},
        ):
            DocumentExtraFieldType.objects.update_or_create(
                document_type=noc_type, field_key=spec["field_key"],
                defaults={**spec, "is_active": True},
            )
        self.stdout.write("extra fields: destination_country, purpose, letter_date")

        salary_type = DocumentType.objects.get(code="SALARY_CERT")
        DocumentExtraFieldType.objects.filter(
            document_type=salary_type,
            field_key__in=SALARY_AUTO_FIELD_KEYS,
        ).update(is_active=False)
        for spec in SALARY_EXTRA_FIELDS:
            DocumentExtraFieldType.objects.update_or_create(
                document_type=salary_type, field_key=spec["field_key"],
                defaults={
                    **spec,
                    "fillable_by": DocumentExtraFieldType.FillableBy.APPROVER,
                    "is_active": True,
                },
            )
        self.stdout.write("extra fields: salary certificate salary table")

        self.stdout.write(self.style.SUCCESS("✅ seed_document_types complete"))
