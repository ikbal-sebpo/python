# Salary Certificate PDF Redesign & Dynamic Salary Data Integration

## Task Objective

The existing certificate generation system, approval workflow, PDF generation process, and base implementation are already available.

**Do not rebuild the system from scratch.**

The objective of this task is to:

1. Redesign the existing Salary Certificate PDF template according to the provided sample image.
2. Keep all existing certificate functionality and workflow intact.
3. Retrieve salary-related information from the existing Extra Field / Dynamic Field models.
4. Dynamically display the latest three months of salary information.
5. Generate the final Salary Certificate as a properly formatted A4 PDF.

---

# 1. Existing System

The following functionality already exists in the project and must be inspected before making changes:

- Certificate creation
- Certificate approval workflow
- Employee data retrieval
- Employee designation and department retrieval
- Existing Extra Field / Dynamic Field system
- Signatory selection
- Signature image support
- Reference number generation
- Existing HTML template rendering
- Existing PDF generation
- Access control and authorization

## Important

Do not unnecessarily rewrite or refactor unrelated functionality.

The required implementation should focus on:

```text
Existing Certificate System
        +
Dynamic Salary Data
        +
New PDF Layout
```

---

# 2. Design Reference

The provided Salary Certificate sample image is the primary visual reference.

The new PDF should follow a similar professional layout and structure.

Expected layout:

```text
--------------------------------------------------

Reference Number                         Date

                 SALARY CERTIFICATE


Employee Introduction


Salary Information Introduction


              THREE MONTH SALARY TABLE


Payment Summary


Declaration


Thanking You,

Signature

Signatory Information


--------------------------------------------------
Company Footer
--------------------------------------------------
```

The design does not need to be pixel-perfect, but it should closely resemble the provided sample in terms of:

- Layout
- Alignment
- Table structure
- Typography hierarchy
- Section spacing
- Professional appearance

---

# 3. Existing Template Variables

The existing implementation already uses variables similar to the following:

```django
{{ ref_no }}

{{ letter_date }}

{{ employee.get_full_name }}

{{ employee.employee_id }}

{{ company_name }}

{{ designation }}

{{ department }}

{{ joining_date }}

{{ signatory_signature }}

{{ signatory_name }}

{{ signatory_title }}

{{ signatory_office_phone }}

{{ signatory_mobile_phone }}

{{ signatory_email }}
```

These variables must continue to work.

Do not remove existing functionality unless absolutely necessary.

---

# 4. Header Section

The top section should contain:

## Left Side

```django
{{ ref_no }}
```

Example:

```text
SEBPO-F&A/SC/2026/539/SL-13/8-17
```

## Center

```text
SALARY CERTIFICATE
```

The title should be bold, centered, and visually prominent.

## Right Side

```django
{{ letter_date }}
```

Recommended format:

```text
August 13, 2026
```

Use the existing date logic where possible.

---

# 5. Employee Introduction Section

The certificate should include employee information similar to:

```text
This is to certify that [Employee Name], Employee ID: [Employee ID],
has been working as [Designation] in the [Department] department
at [Company Name].

He/She is a permanent employee of the company since [Joining Date].
```

Use dynamic values from the existing system.

Suggested Django template logic:

```django
This is to certify that {{ employee.get_full_name }}
{% if employee.employee_id %}
(Employee ID: {{ employee.employee_id }})
{% endif %}
has been working as {{ designation }}
in the {{ department }} department at {{ company_name }}
{% if joining_date %}
since {{ joining_date|date:"F j, Y" }}
{% endif %}.
```

### Optional Fields

If Employee ID does not exist:

- Do not display Employee ID.

If Joining Date does not exist:

- Do not display the joining date sentence.

---

# 6. Salary Information Section

Add a section similar to:

```text
His/Her salary has been paid in the last three months as follows:
```

The salary information must be dynamic.

Do not hardcode salary values.

---

# 7. Main Requirement: Dynamic Three-Month Salary Data

The certificate must display the latest three months of salary information.

Example:

```text
Jul-26 | Jun-26 | May-26
```

The order should always be:

```text
Most Recent Month
        ↓
Previous Month
        ↓
Older Month
```

For example:

```text
July 2026
June 2026
May 2026
```

---

# 8. Salary Table Layout

The salary table should follow this structure:

| Particulars | Month 1 | Month 2 | Month 3 |
|---|---:|---:|---:|
| Gross Salary | | | |
| Incentive / LWP | | | |
| Festival Bonus | | | |
| **Gross Payment** | | | |
| | | | |
| **Add/(Less):** | | | |
| Salary Due / Adjustment | | | |
| TA/DA & Others | | | |
| Lunch Allowance | | | |
| Tax | | | |
| **Net Take-home Salary** | | | |

The exact salary components should be mapped from the existing Extra Field / Dynamic Field structure.

---

# 9. Salary Data Source

## IMPORTANT

Salary values must come from the existing project database structure.

The project already has an Extra Field / Dynamic Field system.

Before implementing anything, inspect:

```text
1. Extra Field Model
2. Extra Field Type Model
3. Dynamic Field Model
4. Employee-related field values
5. Certificate-related field values
6. Approval workflow
7. Existing certificate data storage
```

### Do Not Assume Field Names

Do not hardcode database field names without inspecting the actual models.

The agent must determine how the current project stores:

- Field Name
- Field Type
- Field Value
- Employee Relationship
- Certificate Relationship
- Approval Relationship
- Date / Month Information

Use the existing architecture.

---

# 10. Expected Salary Components

The existing Extra Field system may contain fields equivalent to:

```text
Gross Salary

Incentive

LWP

Festival Bonus

Gross Payment

Salary Due

Salary Adjustment

TA/DA

Other Allowance

Lunch Allowance

Tax

Other Deduction

Net Take-home Salary
```

These are examples only.

The actual field names must be identified from the database/models.

---

# 11. Recommended Backend Data Preparation

Before sending data to the Django template, prepare the salary information in a clean structure.

Example:

```python
salary_months = [
    {
        "month": "Jul-26",

        "gross_salary": 92500,
        "incentive_lwp": None,
        "festival_bonus": None,
        "gross_payment": 92500,

        "salary_adjustment": None,
        "ta_da_others": None,
        "lunch_allowance": -500,
        "tax": -2400,

        "net_take_home": 89600,
    },

    {
        "month": "Jun-26",

        "gross_salary": 92500,
        "incentive_lwp": None,
        "festival_bonus": None,
        "gross_payment": 92500,

        "salary_adjustment": None,
        "ta_da_others": None,
        "lunch_allowance": -300,
        "tax": None,

        "net_take_home": 92200,
    },

    {
        "month": "May-26",

        "gross_salary": 92500,
        "incentive_lwp": None,
        "festival_bonus": 46250,
        "gross_payment": 138750,

        "salary_adjustment": None,
        "ta_da_others": None,
        "lunch_allowance": -550,
        "tax": -3100,

        "net_take_home": 135100,
    }
]
```

This structure is only an example.

The actual data must come dynamically from the existing database.

---

# 12. Recommended Processing Flow

The preferred implementation flow is:

```text
Extra Field / Dynamic Field Data
                ↓
Retrieve Employee Salary Records
                ↓
Identify Latest 3 Months
                ↓
Map Fields to Salary Components
                ↓
Prepare salary_months Structure
                ↓
Add Data to Template Context
                ↓
Render Django HTML Template
                ↓
Generate PDF
```

---

# 13. Template Rendering

The salary table should be generated dynamically.

Recommended concept:

```django
<table>
    <thead>
        <tr>
            <th>Particulars</th>

            {% for month in salary_months %}
                <th>{{ month.month }}</th>
            {% endfor %}
        </tr>
    </thead>

    <tbody>

        <tr>
            <td>Gross Salary</td>

            {% for month in salary_months %}
                <td>
                    {{ month.gross_salary }}
                </td>
            {% endfor %}
        </tr>

    </tbody>
</table>
```

The final implementation may use a different structure depending on the current template engine and codebase.

---

# 14. Number Formatting

All salary values should be formatted properly.

Example:

```text
92500
```

Should display as:

```text
92,500
```

Use the project's existing formatting utilities if available.

---

# 15. Deduction Formatting

Negative or deduction amounts should display using parentheses.

Example:

```text
-500
```

Should display as:

```text
(500)
```

Example:

```text
-2400
```

Should display as:

```text
(2,400)
```

Do not display:

```text
-2,400
```

unless the existing business requirement specifically requires that format.

---

# 16. Missing Salary Values

If an optional salary field is:

- Null
- Empty
- Missing

The PDF should display:

```text
-
```

Example:

```text
Festival Bonus: -
TA/DA: -
Incentive: -
```

The PDF generation must not fail because an optional field is missing.

---

# 17. Gross Payment

The Gross Payment row should be displayed prominently.

Example:

```text
Gross Salary
+
Incentive
+
Festival Bonus
+
Other Earnings
=
Gross Payment
```

Before adding calculation logic:

1. Check whether Gross Payment is already stored.
2. If stored, use the existing value.
3. Only calculate it if the current business logic requires calculation.

Do not introduce duplicate or conflicting calculation logic.

---

# 18. Net Take-home Salary

The Net Take-home Salary should be prominently displayed.

Example:

```text
Gross Payment
+
Additional Earnings
-
Deductions
=
Net Take-home Salary
```

Again:

- Prefer existing stored values.
- Follow existing business rules.
- Do not override existing salary calculation logic.

---

# 19. Payment Summary

Below the salary table, add:

```text
Payment Summary:
```

Then:

```text
The most recent payment was transferred through bank account no.: {{ bank_account_number }}
```

The bank account number should be retrieved from the existing:

- Employee Profile
- Employee Bank Information
- Extra Field
- Existing related model

depending on the current project structure.

Do not create a duplicate bank information model.

### If Bank Information Is Missing

Follow the existing business rule.

Possible behavior:

- Hide the sentence, or
- Display only if the account number exists.

---

# 20. Declaration Section

Add a declaration section similar to:

```text
Declaration

I/We declare that the above information regarding the said employee
cited by me/us is true, complete and accurate. Any loss or liability
arising from the issuance of this letter will not bind the company
or any of its officers.
```

If the current certificate system supports configurable content, use the existing configuration instead of hardcoding this text unnecessarily.

---

# 21. Signature Section

Keep the existing signatory functionality.

Existing dynamic fields include:

```django
{{ signatory_signature }}

{{ signatory_name }}

{{ signatory_title }}

{{ signatory_office_phone }}

{{ signatory_mobile_phone }}

{{ signatory_email }}

{{ company_name }}
```

The layout should resemble:

```text
Thanking You,
For [Company Name]


[SIGNATURE IMAGE]


_________________________

Signature

Name: [Signatory Name]

Designation: [Signatory Title]

Phone: [Office Phone]

Mobile: [Mobile Phone]

Email: [Email]
```

---

# 22. Signature Logic

If:

```django
{{ signatory_signature }}
```

exists:

Render the signature image.

If it does not exist:

Use the existing fallback behavior.

Do not break the current signature functionality.

---

# 23. Logo

If the existing certificate/PDF system already supports a company logo:

- Use the existing logo.
- Do not create another logo system.
- Place the logo near the top-right area similar to the sample.

---

# 24. Footer

If company footer information already exists in the project, continue using it.

Possible information:

```text
Company Website

Corporate Office Address

Branch Office Address

Phone Number
```

Do not hardcode company information if dynamic configuration already exists.

---

# 25. PDF Requirements

The generated PDF should be:

```text
Page Size: A4

Professional Layout

Readable Typography

Proper Margins

Stable Table Layout

No Overflow

Clean Section Spacing
```

---

# 26. PDF CSS Compatibility

Inspect the existing PDF generation engine.

If the project uses WeasyPrint:

Use CSS compatible with WeasyPrint.

Recommended starting point:

```css
@page {
    size: A4;
    margin: 20mm 15mm 20mm 15mm;
}
```

The final values should be adjusted according to the existing project design.

---

# 27. Table Styling Requirements

The salary table should:

- Fit within A4 page width.
- Have a wider first column for Particulars.
- Have equal width salary month columns.
- Align salary values to the right.
- Keep month headers centered.
- Use bold formatting for:
  - Gross Payment
  - Add/(Less)
  - Net Take-home Salary
- Use borders/dividers similar to the provided sample.
- Avoid table overflow.

Suggested concept:

```text
Particulars Column → Wide

Month 1 → Equal Width
Month 2 → Equal Width
Month 3 → Equal Width
```

---

# 28. Suggested Template Context

The final backend context may approximately look like:

```python
context = {
    "ref_no": ref_no,
    "letter_date": letter_date,

    "employee": employee,

    "company_name": company_name,

    "designation": designation,

    "department": department,

    "joining_date": joining_date,

    "salary_months": salary_months,

    "bank_account_number": bank_account_number,

    "signatory_signature": signatory_signature,

    "signatory_name": signatory_name,

    "signatory_title": signatory_title,

    "signatory_office_phone": signatory_office_phone,

    "signatory_mobile_phone": signatory_mobile_phone,

    "signatory_email": signatory_email,
}
```

This is a conceptual example.

The actual implementation should follow the existing project structure.

---

# 29. Approval Workflow

The current approval workflow must remain intact.

The desired flow is:

```text
User / HR Provides Required Extra Field Data
                    ↓
Certificate Goes for Approval
                    ↓
Certificate Approved
                    ↓
System Retrieves Required Salary Information
                    ↓
Latest 3 Months Identified
                    ↓
Salary Data Prepared
                    ↓
Certificate Context Generated
                    ↓
HTML Rendered
                    ↓
PDF Generated
```

Do not create a completely separate workflow.

---

# 30. Historical Data / Snapshot Consideration

This is important.

Before implementing, inspect how the current certificate system handles data after approval.

Example question:

```text
If employee salary information changes next month,
should an already approved/generated certificate change?
```

If the existing system supports certificate data snapshots:

Use the existing snapshot mechanism.

If the existing system stores approved certificate data:

Follow that structure.

The implementation should preserve historical accuracy.

Do not introduce unnecessary new snapshot architecture without inspecting the existing system.

---

# 31. Error Handling

The PDF generation should not fail if:

- Festival Bonus is missing.
- Incentive is missing.
- TA/DA is missing.
- Lunch Allowance is missing.
- Tax is missing.
- Bank account number is unavailable.

Optional data should be handled gracefully.

---

# 32. Development Process

Before making changes, inspect the following:

## Step 1

Locate the existing:

```text
Salary Certificate Model
```

## Step 2

Locate:

```text
Certificate Approval Logic
```

## Step 3

Locate:

```text
Extra Field Models
```

## Step 4

Identify:

```text
How Extra Fields are related to:

Employee
Certificate
Approval
Month / Date
```

## Step 5

Locate:

```text
Current PDF Generation View / Service
```

## Step 6

Locate:

```text
Current Salary Certificate HTML Template
```

## Step 7

Implement:

```text
Latest Three Month Salary Retrieval
```

## Step 8

Implement:

```text
Dynamic Salary Data Mapping
```

## Step 9

Update:

```text
Salary Certificate HTML / CSS Layout
```

## Step 10

Test PDF generation.

---

# 33. Testing Requirements

Test the following scenarios.

## Test 1: Complete Salary Data

All fields available.

Expected:

```text
All values display correctly.
```

---

## Test 2: Missing Festival Bonus

Expected:

```text
Festival Bonus displays "-"
PDF generates successfully.
```

---

## Test 3: Missing Optional Allowances

Expected:

```text
Missing fields display "-"
PDF generates successfully.
```

---

## Test 4: Salary Deductions

Example:

```text
Lunch Allowance: -500
Tax: -2400
```

Expected PDF:

```text
Lunch Allowance: (500)
Tax: (2,400)
```

---

## Test 5: Signature Available

Expected:

```text
Signature image renders correctly.
```

---

## Test 6: Signature Not Available

Expected:

```text
PDF still generates correctly using existing fallback behavior.
```

---

## Test 7: Only Three Months Available

Expected:

```text
Latest three months display correctly.
```

---

# 34. Important Restrictions

## DO NOT

```text
❌ Rebuild the certificate system from scratch.

❌ Create duplicate models without checking existing models.

❌ Hardcode employee salary values.

❌ Hardcode database field names without inspecting the models.

❌ Break existing approval functionality.

❌ Break existing PDF generation.

❌ Break existing certificate types.

❌ Modify unrelated systems unnecessarily.
```

---

# 35. Implementation Priority

Priority order:

```text
1. Inspect Existing Architecture

2. Understand Extra Field Structure

3. Understand Approval Workflow

4. Retrieve Salary Data

5. Prepare Latest 3 Months

6. Map Salary Components

7. Update Template Context

8. Redesign PDF Template

9. Apply PDF CSS

10. Test PDF Generation
```

---

# 36. Expected Final Result

The final Salary Certificate should:

```text
✓ Follow the provided sample design.

✓ Keep the existing certificate system working.

✓ Display employee information dynamically.

✓ Display the latest three months dynamically.

✓ Retrieve salary data from the existing Extra Field / Dynamic Field system.

✓ Handle optional salary fields.

✓ Format numbers with commas.

✓ Display deductions with parentheses.

✓ Display Gross Payment.

✓ Display Net Take-home Salary.

✓ Display Payment Summary.

✓ Display Declaration.

✓ Support existing signatory information.

✓ Support signature image.

✓ Use existing company information.

✓ Generate a professional A4 PDF.

✓ Preserve existing approval workflow.
```

---

# 37. Final Deliverables

After completing the task, provide:

## 1. Updated Template

Provide the updated Salary Certificate template file.

Example:

```text
salary_certificate.html
```

or modify the existing relevant template.

---

## 2. Backend Changes

Document changes related to:

```text
Extra Field Retrieval

Salary Data Processing

Latest Three Month Selection

Template Context Preparation
```

---

## 3. PDF CSS

Provide the updated CSS responsible for:

```text
A4 Layout

Header

Salary Table

Declaration

Signature

Footer
```

---

## 4. Field Mapping Explanation

Clearly explain:

```text
Which existing Extra Field maps to which Salary Certificate field.
```

Example format:

```text
Existing Field Name
        ↓
Salary Certificate Row
```

---

## 5. Testing Result

Confirm:

```text
PDF successfully generated.
```

Also mention:

```text
Test cases completed.
```

---

# FINAL IMPORTANT INSTRUCTION

**Do not start by rewriting the system.**

First inspect the existing codebase, models, approval workflow, Extra Field / Dynamic Field structure, template rendering process, and PDF generation logic.

The base certificate system already exists.

The task is primarily:

```text
Existing Certificate System
        +
Dynamic Salary Data Integration
        +
Latest Three Months Processing
        +
New Salary Certificate PDF Design
```

Make the minimum necessary changes while keeping all existing functionality stable.
