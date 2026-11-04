"""ISO-format overrides for all English locales.

Django normalises LANGUAGE_CODE and falls back from a specific locale
(e.g. `en_US`) to the language root (`en`). By putting overrides here we
catch every English variant with a single file.

Active via FORMAT_MODULE_PATH in core/settings.py.
"""

DATE_FORMAT = "Y-m-d"
DATETIME_FORMAT = "Y-m-d H:i"
SHORT_DATE_FORMAT = "Y-m-d"
SHORT_DATETIME_FORMAT = "Y-m-d H:i"
YEAR_MONTH_FORMAT = "Y-m"
MONTH_DAY_FORMAT = "m-d"

DATE_INPUT_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
]
DATETIME_INPUT_FORMATS = [
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
]

FIRST_DAY_OF_WEEK = 0
