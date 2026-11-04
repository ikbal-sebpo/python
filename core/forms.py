from django import forms


class StyledMixin:
    """Apply the theme's ``.form-input`` class to every widget on a form.

    The class lives in ``theme/static_src/src/styles.css`` and provides the
    bg/border/padding/focus styling for inputs, textareas, dates, AND
    selects. Critically, it also drives a dark-mode rule that fixes the
    native ``<option>`` rendering — without ``form-input`` on a ``<select>``,
    its dropdown options render with light backgrounds in dark mode and
    become unreadable.

    See ``CLAUDE.md`` → "Working conventions" for the project-wide rule.

    Usage:
        class MyForm(forms.ModelForm, StyledMixin):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                # add any dynamic fields here, then:
                self.style_widgets()

    Checkboxes are skipped — they use a different visual (.form-checkbox).
    """

    def style_widgets(self):
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                continue
            existing = widget.attrs.get("class", "")
            widget.attrs["class"] = (existing + " form-input").strip()


# ---------------------------------------------------------------------------
# Date pickers
# ---------------------------------------------------------------------------
#
# Flatpickr is the ONLY date picker in this project. Never use a native
# ``<input type="date">`` / ``type="datetime-local"``: the browser renders its
# own control, so the same form looks different per-OS and clashes with the
# Timely dark theme. Templates use ``type="text" data-flatpickr`` directly
# (see ``templates/roster/week.html``); Python forms use the helpers below.
#
# ``base.html::wireFlatpickr`` binds every ``[data-flatpickr]`` /
# ``[data-flatpickr-datetime]`` input on load and after each htmx swap.

DATE_FORMAT = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%d %H:%M"

_DATE_ATTRS = {
    "type": "text",
    "placeholder": "YYYY-MM-DD",
    "data-flatpickr": "",
    "autocomplete": "off",
    "pattern": r"\d{4}-\d{2}-\d{2}",
}
_DATETIME_ATTRS = {
    "type": "text",
    "placeholder": "YYYY-MM-DD HH:MM",
    "data-flatpickr-datetime": "",
    "autocomplete": "off",
}


def date_widget(css_class: str = "", **extra_attrs) -> forms.DateInput:
    """A flatpickr-backed date input. Pass ``css_class="form-input"`` unless
    the form runs ``StyledMixin.style_widgets()``, which adds it for you."""
    attrs = {**_DATE_ATTRS, **extra_attrs}
    if css_class:
        attrs["class"] = css_class
    return forms.DateInput(attrs=attrs, format=DATE_FORMAT)


def datetime_widget(css_class: str = "", **extra_attrs) -> forms.DateTimeInput:
    """A flatpickr-backed date+time input (``enableTime``)."""
    attrs = {**_DATETIME_ATTRS, **extra_attrs}
    if css_class:
        attrs["class"] = css_class
    return forms.DateTimeInput(attrs=attrs, format=DATETIME_FORMAT)


def use_date_picker(form, *names, css_class: str = "") -> None:
    """Swap the named fields on ``form`` to the flatpickr widget.

    Silently skips names the form doesn't have, so one call can cover a
    create/edit pair that expose different subsets of the same fields.
    ``input_formats`` is pinned alongside the widget — flatpickr always
    submits ISO, and leaving Django's locale-dependent default in place makes
    a round-trip reject its own rendered value.
    """
    for name in names:
        field = form.fields.get(name)
        if field is None:
            continue
        if isinstance(field, forms.DateTimeField):
            field.widget = datetime_widget(css_class)
            field.input_formats = [DATETIME_FORMAT]
        else:
            field.widget = date_widget(css_class)
            field.input_formats = [DATE_FORMAT]


# ---------------------------------------------------------------------------
# Money inputs — whole taka
# ---------------------------------------------------------------------------

class TakaField(forms.DecimalField):
    """A money input in whole taka — no paise anywhere a person types or
    reads an amount (Finance's decision, 2026-10-07; display already went
    whole-taka via ``|taka`` in docs/MONEY_FORMAT_PLAN.md).

    * Renders the bound/initial value as an integer (``306786``, never
      ``306786.00``) and asks the browser for ``step="1"``.
    * Accepts a typed decimal anyway and rounds it half-up to the nearest
      taka instead of rejecting it, so a pasted ``12345.67`` lands as
      ``12346`` rather than an error.
    * ``decimal_places`` is forced to 0 for validation; ``max_digits`` is
      whatever the caller (or the model column) says.

    ``core.fields.EncryptedDecimalField.formfield()`` returns this by default,
    so every ModelForm and admin form over an encrypted money column gets
    it without naming it; plain ``forms.Form`` money inputs use it directly.
    """

    def __init__(self, *, decimal_places=None, **kwargs):
        kwargs.pop("step_size", None)
        super().__init__(decimal_places=0, **kwargs)

    def widget_attrs(self, widget):
        attrs = super().widget_attrs(widget)
        if isinstance(widget, forms.NumberInput):
            attrs["step"] = "1"
        return attrs

    def prepare_value(self, value):
        from core.money import to_whole_taka

        whole = to_whole_taka(value)
        return whole if whole is not None else value

    def to_python(self, value):
        from decimal import ROUND_HALF_UP, Decimal

        value = super().to_python(value)
        if value is None:
            return None
        return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
