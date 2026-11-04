from datetime import time

from django.contrib import admin
from django.test import RequestFactory, TestCase, override_settings

from roster.admin import ShiftAttributeAdmin
from roster.models import ShiftAttribute, ShiftType


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    },
)
class ShiftAttributeAdminActionTests(TestCase):
    def setUp(self):
        self.shift_type = ShiftType.objects.create(
            name="EVE",
            category=ShiftType.Category.WORK,
            shift_start_time=time(14, 0),
            shift_end_time=time(23, 0),
        )
        self.attr = ShiftAttribute.objects.create(
            shift_type=self.shift_type,
            name="EVE 1-10",
            start_time=time(13, 0),
            end_time=time(22, 0),
            is_active=False,
        )
        self.model_admin = ShiftAttributeAdmin(ShiftAttribute, admin.site)
        self.model_admin.message_user = lambda *args, **kwargs: None
        self.request = RequestFactory().post("/")

    def test_activate_shift_attributes_action(self):
        queryset = ShiftAttribute.objects.filter(pk=self.attr.pk)

        self.model_admin.activate_shift_attributes(self.request, queryset)

        self.attr.refresh_from_db()
        self.assertTrue(self.attr.is_active)

    def test_deactivate_shift_attributes_action(self):
        self.attr.is_active = True
        self.attr.save(update_fields=["is_active"])
        queryset = ShiftAttribute.objects.filter(pk=self.attr.pk)

        self.model_admin.deactivate_shift_attributes(self.request, queryset)

        self.attr.refresh_from_db()
        self.assertFalse(self.attr.is_active)
