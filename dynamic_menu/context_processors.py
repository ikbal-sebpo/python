from django.conf import settings
from django.core.cache import cache

from accounts.permissions import user_group_pks
from dynamic_menu.models import MainMenu, SubMenu

# The menu tree is identical for everyone sharing a group set and changes only
# when an admin edits menus/ACLs, yet it was rebuilt (3 queries) on every
# authenticated request. Cache per group-set; invalidation is a generation
# counter bumped by the signals in dynamic_menu/signals.py. TTL comes from
# DYNAMIC_MENU_CACHE_SECONDS (0 = bypass, used by settings_test).
_GENERATION_KEY = "dynamic_menu:generation"


def _generation() -> int:
    gen = cache.get(_GENERATION_KEY)
    if gen is None:
        gen = 1
        cache.add(_GENERATION_KEY, gen, None)
    return gen


def bump_menu_generation() -> None:
    """Invalidate every cached menu tree (called on menu/ACL changes)."""
    try:
        cache.incr(_GENERATION_KEY)
    except ValueError:
        cache.set(_GENERATION_KEY, 1, None)


def dynamic_menus(request):
    """Inject the caller's visible menu tree.

    Built via direct M2M on MainMenu/SubMenu → auth.Group, distinct-ified so
    a user in multiple groups granted the same menu doesn't see duplicates.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"dynamic_main_menus": [], "dynamic_sub_menus": []}

    ttl = settings.DYNAMIC_MENU_CACHE_SECONDS
    group_pks = sorted(user_group_pks(user, request))
    key = "dynamic_menu:tree:{}:{}".format(
        _generation(), ",".join(map(str, group_pks)),
    )
    if ttl > 0:
        tree = cache.get(key)
        if tree is not None:
            return tree

    main = (
        MainMenu.objects
        .filter(is_active=True, groups__in=group_pks)
        .distinct()
        .order_by("rank", "title")
        .prefetch_related("submenus")
    )
    subs = (
        SubMenu.objects
        .filter(is_active=True, main_menu__in=main, groups__in=group_pks)
        .distinct()
        .order_by("main_menu__rank", "rank", "title")
    )
    tree = {
        "dynamic_main_menus": list(main),
        "dynamic_sub_menus": list(subs),
    }
    if ttl > 0:
        cache.set(key, tree, ttl)
    return tree
