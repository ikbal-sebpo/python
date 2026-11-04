# VeriDoc PDF Design and Format Handoff

এই ডকটি বর্তমান VeriDoc প্রজেক্টের PDF design/format অন্য প্রজেক্টে একইভাবে implement করার জন্য বানানো হয়েছে। এখানে PDF generate হয় HTML + CSS template render করে WeasyPrint দিয়ে।

## High-Level Architecture

PDF rendering দুই স্তরে ভাগ করা:

1. `TemplateDesign`
   - Full page layout.
   - A4 page size, margins, header, QR, logo, running footer, typography, page-safe CSS.
   - এর ভেতরে `{{ body_html|safe }}` placeholder থাকে।

2. `MessageTemplate`
   - শুধু document body বা letter content.
   - Employee certificate, NOC, experience certificate, resignation letter ইত্যাদি আলাদা body template.
   - আগে এই template render হয়, তারপর rendered body `TemplateDesign`-এর `{{ body_html|safe }}` জায়গায় inject হয়।

Final flow:

```text
DocumentRequest
  -> build context
  -> render MessageTemplate.body_html
  -> inject rendered HTML into TemplateDesign.html
  -> WeasyPrint HTML.write_pdf()
  -> save to DocumentRequest.generated_file
```

Main implementation file:

```text
veridoc/pdf_generator.py
```

## Required Packages

```text
weasyprint==67.0
qrcode[pil]
Pillow
```

Current project also uses:

```text
django-ckeditor
```

because `TemplateDesign.html` and `MessageTemplate.body_html` are `RichTextField`.

## Model Structure

Minimum required models/relations:

```python
class TemplateDesign(models.Model):
    key = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=150)
    html = RichTextField(blank=True)
    is_active = models.BooleanField(default=True)


class MessageTemplate(models.Model):
    key = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=150)
    body_html = RichTextField(blank=True)
    is_active = models.BooleanField(default=True)


class DocumentType(models.Model):
    name = models.CharField(max_length=100, unique=True)
    display_name = models.CharField(max_length=150)
    template_design = models.ForeignKey(TemplateDesign, on_delete=models.PROTECT)
    message_template = models.ForeignKey(MessageTemplate, on_delete=models.PROTECT)
    requires_approval = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)


class DocumentRequest(models.Model):
    id = models.UUIDField(primary_key=True)
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    document_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT)
    generated_file = models.FileField(upload_to="veridoc/generated/", null=True, blank=True)
    input_data = models.JSONField(default=dict, blank=True)
```

If approval signature is needed:

```python
class RequestApproval(models.Model):
    request = models.ForeignKey(DocumentRequest, related_name="approvals", on_delete=models.CASCADE)
    signatory = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=10)  # APPROVED/PENDING/REJECTED
    order = models.PositiveSmallIntegerField(default=1)
    signed_at = models.DateTimeField(null=True, blank=True)
```

## Base TemplateDesign HTML

Use this as the default design. Current key/name:

```text
key: base-design
name: Base Design
```

```html
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
      padding-bottom: calc(var(--footer-height) + var(--body-gap));
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
        <img class="logo" src="{{ logo_url }}" alt="Logo">
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
```

## Visual Specification

Page:

- Paper size: `A4`
- Top margin: `18mm`
- Left/right content padding: `18mm`
- Bottom reserved margin: `24mm` for footer
- Extra body gap above footer: `6mm`
- Body font: `Arial, Helvetica, sans-serif`
- Body font size: `12pt`
- Body line height: `1.45`
- Text color: `#111`

Header:

- Header uses flex layout.
- QR is left aligned.
- Logo is right aligned.
- QR image size: `82px x 82px`
- QR border: `1px solid #ddd`
- QR padding: `4px`
- QR label: `Scan to verify`, `9pt`, color `#444`
- Logo height: `46px`, width auto.
- Header bottom spacing: `14px`

Footer:

- WeasyPrint running footer using `position: running(page-footer)`.
- Injected into every PDF page via `@bottom-center`.
- Footer background: `#066F98`
- Footer text color: white.
- Footer height: `24mm`
- Footer padding: `5mm 18mm`
- Footer font size: `8.5pt`
- Footer line height: `1.25`
- Website text is uppercase, bold `800`, letter spacing `0.3px`.

Overflow safety:

- `.page-content` uses `overflow-wrap: break-word` and `word-break: break-word`.
- `.body *` uses `max-width: 100%` and `box-sizing: border-box`.
- `.body img` is constrained to page width.
- `.body table` is forced to `width: 100%` and `border-collapse: collapse`.

## MessageTemplate Body Pattern

Body templates should not include full `<html>`, `<head>`, or `<body>` unless absolutely needed. Keep only body HTML.

Common top row format:

```html
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
```

Common title format:

```html
<p style="text-align:center; margin:18px 0;">
  <strong><u>EMPLOYMENT CERTIFICATE</u></strong>
</p>
```

Common signature block:

```html
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
```

## Required Context Variables

The PDF context should provide these keys:

```text
request
employee
document_type
input_data
today
today_mmddyyyy
letter_date
letter_date_mmddyyyy
public_url
qr_data_uri
logo_url
company_name
ref_no
subject
department
work_location
joining_date
designation
footer_company
head_office_address
branch_office_address
jashore_office_address
website
signatory_user
signatory_name
signatory_title
signatory_email
signatory_office_phone
signatory_mobile_phone
signatory_signature
extra
```

Also flatten `input_data` into context, so this works:

```django
{{ purpose }}
{{ destination_country }}
{{ travel_from }}
{{ travel_to }}
{{ resignation_letter_date }}
{{ end_date }}
```

If dynamic extra fields exist, flatten them too and let them override `input_data`.

## Context Defaults

Current defaults:

```text
PUBLIC_BASE_URL fallback: http://127.0.0.1:8000
Verification URL: <PUBLIC_BASE_URL>/doc/verify/<request_uuid>/
company_name fallback: SEBPO
ref_no fallback: SE/HRD/XXXX/YYYY/XX
website fallback: sebpo.com
```

Corporate Head Office:

```text
Monem Business District, Level 7, 111 Bir Uttam C.R. Dutta Road (Sonargaon Road), Dhaka 1205, Bangladesh. Phone: +88 (096) 0622-1176
```

Branch Office:

```text
8 AbbasGarden, DOHS Mohakhali, Dhaka 1206, Bangladesh. Phone: +88 (096) 06221100
```

Jashore Office:

```text
Sheikh Hasina Software Technology Park, 9th Floor, Nazir Songkorpur Road, Adjacent to Jashore Passport Office, Jashore-7400, Khulna.
```

## QR Code Rule

QR code points to public verification URL:

```text
<PUBLIC_BASE_URL>/doc/verify/<request_uuid>/
```

QR generation settings:

```python
qr = qrcode.QRCode(
    version=None,
    error_correction=qrcode.constants.ERROR_CORRECT_M,
    box_size=6,
    border=2,
)
```

The QR image is embedded as a data URI:

```text
data:image/png;base64,...
```

This is important because PDF generators often fail to load protected/local media URLs.

## Logo and Signature Image Rule

Preferred order for logo:

1. `input_data["logo_url"]`
2. Absolute file path data URI
3. Company logo FileField data URI
4. Company logo absolute URL fallback
5. Local project file `SEBPO-Logo-2023.png`

Signature:

- Get latest approved `RequestApproval` ordered by highest `order`, then `signed_at`, then `updated_at`.
- Read signatory signature FileField as data URI first.
- If reading from storage fails, fallback to media URL.

## PDF Rendering Function Skeleton

```python
from django.template import engines
from weasyprint import HTML


def render_request_to_pdf_bytes(req):
    django_engine = engines["django"]
    ctx = build_context(req)

    message_template = req.document_type.message_template
    body_src = (message_template.body_html or "").strip()
    if not body_src:
        body_src = "<p>Document body is not configured.</p>"

    rendered_body_html = django_engine.from_string(body_src).render(ctx)

    design_template = req.document_type.template_design
    design_src = (design_template.html or "").strip()
    if not design_src:
        design_src = "<!doctype html><html><head><meta charset='utf-8'></head><body>{{ body_html|safe }}</body></html>"

    ctx["body_html"] = rendered_body_html
    final_html = django_engine.from_string(design_src).render(ctx)

    return HTML(string=final_html, base_url=get_public_base_url()).write_pdf()
```

## Example: Employment Certificate Body

```html
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

<p style="text-align:center; margin:18px 0;">
  <strong><u>EMPLOYMENT CERTIFICATE</u></strong>
</p>

<p>
  This is to inform whomever it may concern and certify that
  <strong>{{ employee.get_full_name }}</strong>
  is an employee of
  <strong>{{ company_name }}</strong>
  as
  <strong>{{ designation|default:"[DESIGNATION]" }}</strong>,
  in the department of
  <strong>{{ department|default:"[DEPARTMENT]" }}</strong>
  from
  <strong>{{ joining_date|default:"[JOINING DATE]" }}</strong>
  to
  <strong>{{ end_date|default:"TILL DATE" }}</strong>.
</p>

<p>
  This certificate is issued at the request of
  <strong>{{ employee.get_full_name }}</strong>
  with no further obligation from the organization and is exclusively
  for
  <strong>{{ purpose|default:"OFFICIAL PURPOSES" }}</strong>.
</p>

<p>
  Concerned authorities may contact us if verification is required.
</p>

<br><br>

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
```

## Important WeasyPrint Notes

- `@page` and `position: running(...)` are WeasyPrint-specific CSS features.
- Browser preview may not show the footer exactly like the PDF.
- Use `HTML(string=final_html, base_url=...)` so relative media/static URLs can resolve.
- Prefer data URI for QR, logo, and signature to avoid network/storage access issues.
- Keep footer height enough for all address lines. Current safe value is `24mm`.
- Keep body bottom padding at `calc(var(--footer-height) + var(--body-gap))` so content does not overlap the footer.

## Migration Checklist for Another Project

1. Install `weasyprint` and `qrcode[pil]`.
2. Add `TemplateDesign`, `MessageTemplate`, `DocumentType`, and `DocumentRequest` models or equivalent tables.
3. Add `template_design` and `message_template` relation to each document type.
4. Store the base design HTML from this document with key `base-design`.
5. Store each letter body as a separate message template.
6. Implement context builder with QR, logo, employee, company, department, designation, signatory, footer, and `input_data`.
7. Render body template first.
8. Inject rendered body into design using `{{ body_html|safe }}`.
9. Generate PDF using WeasyPrint.
10. Save PDF bytes to the request's generated file field.
11. Add secure download and public verification routes.
12. Test with long names, long departments, missing signature, missing logo, and multi-page body content.

