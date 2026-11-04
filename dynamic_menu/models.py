from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models
from django.utils import timezone


class _Auditable(models.Model):
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    added_at = models.DateTimeField(default=timezone.now)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class MainMenu(_Auditable):
    title = models.CharField(max_length=100)
    icon_class = models.CharField(
        max_length=100, blank=True,
        help_text="Lucide icon name, e.g. 'users', 'calendar-days'.",
    )
    url = models.CharField(
        max_length=200, blank=True,
        help_text="Route name. Leave blank to render as a section that only expands submenus.",
    )
    rank = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    groups = models.ManyToManyField(Group, blank=True, related_name="main_menus")

    class Meta:
        ordering = ["rank", "title"]
        verbose_name = "Main menu"
        verbose_name_plural = "Main menus"

    def __str__(self) -> str:
        return self.title


class SubMenu(_Auditable):
    main_menu = models.ForeignKey(
        MainMenu, on_delete=models.CASCADE, related_name="submenus",
    )
    title = models.CharField(max_length=100)
    url = models.CharField(max_length=200, help_text="Route name.")
    rank = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    groups = models.ManyToManyField(Group, blank=True, related_name="sub_menus")

    class Meta:
        ordering = ["main_menu__rank", "rank", "title"]
        verbose_name = "Sub menu"
        verbose_name_plural = "Sub menus"

    def __str__(self) -> str:
        return f"{self.main_menu.title} → {self.title}"


class ProtectedView(_Auditable):
    """Access control for views that aren't in the sidebar."""

    registered_app = models.CharField(max_length=100)
    view_name = models.CharField(max_length=200, unique=True)
    view_description = models.CharField(max_length=300, blank=True)
    allowed_groups = models.ManyToManyField(Group, blank=True, related_name="protected_views")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["registered_app", "view_name"]
        verbose_name = "Protected view"
        verbose_name_plural = "Protected views"

    def __str__(self) -> str:
        return f"{self.registered_app}:{self.view_name}"
