from django.contrib import admin

from dynamic_menu.models import MainMenu, ProtectedView, SubMenu


class SubMenuInline(admin.TabularInline):
    model = SubMenu
    extra = 0
    fields = ("title", "url", "rank", "is_active", "groups")
    filter_horizontal = ("groups",)
    ordering = ("rank", "title")


@admin.register(MainMenu)
class MainMenuAdmin(admin.ModelAdmin):
    list_display = ("title", "icon_class", "url", "rank", "is_active")
    list_filter = ("is_active",)
    search_fields = ("title", "url")
    ordering = ("rank", "title")
    filter_horizontal = ("groups",)
    inlines = [SubMenuInline]

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(SubMenu)
class SubMenuAdmin(admin.ModelAdmin):
    list_display = ("main_menu", "title", "url", "rank", "is_active")
    list_filter = ("is_active", "main_menu")
    search_fields = ("title", "url", "main_menu__title")
    filter_horizontal = ("groups",)


@admin.register(ProtectedView)
class ProtectedViewAdmin(admin.ModelAdmin):
    list_display = ("registered_app", "view_name", "is_active")
    list_filter = ("is_active", "registered_app")
    search_fields = ("view_name", "registered_app", "view_description")
    filter_horizontal = ("allowed_groups",)
