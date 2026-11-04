"""Invalidate the cached menu tree whenever menus or their ACLs change."""

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from dynamic_menu.context_processors import bump_menu_generation
from dynamic_menu.models import MainMenu, SubMenu


@receiver(post_save, sender=MainMenu)
@receiver(post_save, sender=SubMenu)
@receiver(post_delete, sender=MainMenu)
@receiver(post_delete, sender=SubMenu)
def _menu_changed(sender, **kwargs):
    bump_menu_generation()


@receiver(m2m_changed, sender=MainMenu.groups.through)
@receiver(m2m_changed, sender=SubMenu.groups.through)
def _menu_acl_changed(sender, **kwargs):
    bump_menu_generation()
