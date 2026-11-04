"""Seed ProtectedView rows from every named URL in the project.

Idempotent: existing rows keep their allowed_groups; missing ones are created
with an empty group list.
"""

from django.core.management.base import BaseCommand
from django.urls import URLPattern, URLResolver, get_resolver

from dynamic_menu.models import ProtectedView


def _walk(patterns, app_namespace=""):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            namespace = pattern.namespace or pattern.app_name or app_namespace
            yield from _walk(pattern.url_patterns, namespace)
        elif isinstance(pattern, URLPattern):
            if pattern.name:
                yield app_namespace or "core", pattern.name


class Command(BaseCommand):
    help = "Scan URL patterns and create ProtectedView rows for each named view."

    def handle(self, *args, **options):
        resolver = get_resolver()
        seen = set()
        created = 0
        for app, name in _walk(resolver.url_patterns):
            key = (app, name)
            if key in seen:
                continue
            seen.add(key)
            _, was_created = ProtectedView.objects.get_or_create(
                view_name=name,
                defaults={"registered_app": app, "is_active": True},
            )
            if was_created:
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Scanned {len(seen)} views · created {created} new ProtectedView row(s)."
        ))
