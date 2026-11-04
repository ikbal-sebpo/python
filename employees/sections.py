"""Section registry — the single source of truth for CRUD-able profile sections.

URL dispatch, form selection, detail-page layout, and partial template lookup
all read from here. Add a section = 1 entry here + 1 form + 1 partial.
"""

from dataclasses import dataclass
from typing import Type

from django.db import models as dj_models
from django.forms import ModelForm

from employees import forms as employee_forms
from employees import models as employee_models
from payroll import forms as payroll_forms
from payroll import models as payroll_models


@dataclass(frozen=True)
class SectionSpec:
    slug: str
    model: Type[dj_models.Model]
    form: Type[ModelForm]
    is_singleton: bool
    icon: str              # lucide icon name
    label: str
    order: int
    partial: str           # filename within templates/employees/sections/
    singular: str = ""     # explicit singular; leave blank to derive from label

    @property
    def dom_id(self) -> str:
        """Card id on the detail page: replace with hx-swap-oob target."""
        return f"section-card-{self.slug}"

    @property
    def singular_label(self) -> str:
        """Singular form for drawer/page titles ("Edit address").

        The old `label.rstrip('s')` / `|cut:"s"` hacks strip every trailing
        (or embedded) "s" — "Addresses" became "Adree" in the UI.
        """
        if self.singular:
            return self.singular
        if self.label.endswith("ses"):
            return self.label[:-2]
        if self.label.endswith("s"):
            return self.label[:-1]
        return self.label


SECTIONS: dict[str, SectionSpec] = {
    spec.slug: spec for spec in [
        SectionSpec("jobinfo",       employee_models.JobInfo,                 employee_forms.JobInfoForm,                 True,  "briefcase",       "Job info",              10,  "_jobinfo.html"),
        SectionSpec("personal",      employee_models.PersonalInfo,            employee_forms.PersonalInfoForm,            True,  "user",            "Personal info",         20,  "_personal.html"),
        SectionSpec("family",        employee_models.FamilyInfo,              employee_forms.FamilyInfoForm,              True,  "heart",           "Family info",           30,  "_family.html"),
        SectionSpec("address",       employee_models.Address,                 employee_forms.AddressForm,                 False, "map-pin",         "Addresses",             40,  "_address.html"),
        SectionSpec("education",     employee_models.Education,               employee_forms.EducationForm,               False, "graduation-cap",  "Education",             50,  "_education.html"),
        SectionSpec("contact",       employee_models.Contact,                 employee_forms.ContactForm,                 False, "phone",           "Contacts",              60,  "_contact.html"),
        SectionSpec("nominee",       employee_models.Nominee,                 employee_forms.NomineeForm,                 False, "users",           "Nominees",              70,  "_nominee.html"),
        SectionSpec("email",         employee_models.Email,                   employee_forms.EmailForm,                   False, "mail",            "Emails",                80,  "_email.html"),
        SectionSpec("id-number",     employee_models.IdentificationNumber,    employee_forms.IdentificationNumberForm,    False, "badge-check",     "ID numbers",            90,  "_id_number.html"),
        SectionSpec("reference",     employee_models.Reference,               employee_forms.ReferenceForm,               False, "bookmark",        "References",            100, "_reference.html"),
        SectionSpec("certification", employee_models.ProfessionalCertification, employee_forms.ProfessionalCertificationForm, False, "award",    "Certifications",        110, "_certification.html"),
        SectionSpec("vaccination",   employee_models.Vaccination,             employee_forms.VaccinationForm,             False, "syringe",         "Vaccinations",          120, "_vaccination.html"),
        SectionSpec("legal",         employee_models.LegalIssue,              employee_forms.LegalIssueForm,              True,  "scale",           "Legal",                 130, "_legal.html"),
        SectionSpec("image",         employee_models.Image,                   employee_forms.ImageForm,                   False, "image",           "Photos & documents",    140, "_image.html", singular="Photo / document"),
        SectionSpec("designation-history", employee_models.DesignationHistory, employee_forms.DesignationHistoryForm,     False, "trending-up",     "Designation history",   145, "_designation_history.html", singular="Designation history entry"),
        # Cross-app: the model lives in `payroll` (gated independently of
        # general profile access), not `employees` — see payroll/models.py.
        SectionSpec("bank-account",  payroll_models.EmployeeBankAccount,      payroll_forms.EmployeeBankAccountForm,      False, "landmark",        "Bank accounts",         150, "_bank_account.html"),
        SectionSpec("tax-declaration", payroll_models.EmployeeTaxDeclaration, payroll_forms.EmployeeTaxDeclarationForm,   False, "percent",         "Tax declarations",      160, "_tax_declaration.html"),
    ]
}


def ordered_sections() -> list[SectionSpec]:
    return sorted(SECTIONS.values(), key=lambda s: s.order)
