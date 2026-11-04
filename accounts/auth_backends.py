"""Microsoft Entra (Azure AD) OIDC authentication backend for Orbis.

Subclass of `mozilla_django_oidc.auth.OIDCAuthenticationBackend` that:

- Links Entra identities to existing CustomUser rows by email, or auto-creates
  one on first sign-in (full JIT, OIDC_CREATE_USER=True in settings).
- Enriches the user from Microsoft Graph `/me` **once, on first login only**:
  names, employee_id, joining_date, department / designation / job_location
  (auto-provisioned by name), contact_number, and JobInfo.reports_to (manager
  — only when the manager already has an Orbis account). Orbis is the system
  of record after that first sign-in — later logins don't re-pull from Graph,
  so an HR admin's edits in Orbis are never clobbered by a subsequent login.
- Records the Entra object id (`CustomUser.entra_object_id`) on **every**
  login and matches on it before email — the stable key other apps use to
  look people up in the Directory API (docs/DIRECTORY_API_PLAN.md §6.1). It
  is an identifier, not HR data, so writing it doesn't reopen the rule above.
- Falls back gracefully if the Graph call fails — login still completes using
  the id_token claims, just without the rich enrichment.

The Graph $select + $expand=manager(...) query is configured via OIDC_OP_USER_ENDPOINT
in core/settings.py — the parent `get_userinfo` method GETs that URL with the
access token, so we don't need to hand-roll the Graph call.
"""

import base64
import json
import logging
import uuid
from datetime import date, datetime

import requests
from django.contrib.auth.backends import ModelBackend
from mozilla_django_oidc.auth import OIDCAuthenticationBackend

logger = logging.getLogger(__name__)


def _decode_jwt_payload_unsafe(token: str) -> dict:
    """Base64-decode a JWT's payload segment without verifying signature.

    Used only for diagnostic logging — we trust the token because it came
    from Microsoft over a TLS-validated OIDC flow; we just want to *peek*
    at the `scp` (scopes) and `aud` claims to debug Graph 403s.
    """
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        # Re-pad if base64url stripped trailing `=`s.
        padding = (-len(payload_b64)) % 4
        payload_b64 += "=" * padding
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def _email_from_graph(payload: dict) -> str:
    """Graph sometimes returns `mail` as null for guest accounts; fall back to UPN."""
    return (payload.get("mail") or payload.get("userPrincipalName") or "").strip().lower()


def _object_id_from_claims(claims: dict) -> str | None:
    """The Entra object id: Graph `/me` returns it as `id`; the id_token
    fallback (`get_userinfo`'s last resort) carries it as `oid`. Normalised to
    the canonical lower-case UUID form, or None when absent/malformed."""
    if not isinstance(claims, dict):
        return None
    raw = claims.get("id") or claims.get("oid")
    if not raw:
        return None
    try:
        return str(uuid.UUID(str(raw)))
    except ValueError:
        logger.warning("Ignoring malformed Entra object id in claims: %r", raw)
        return None


def _domain_allowed(email: str) -> bool:
    """Defense-in-depth domain gate for SSO sign-in and JIT provisioning.

    The Entra app registration being single-tenant is the primary control; this
    check keeps an Azure-side misconfiguration (multi-tenant flip, borrowed
    test app reg) from letting outside identities into an HR system. An empty
    OIDC_ALLOWED_EMAIL_DOMAINS disables the check.
    """
    from django.conf import settings as dj_settings

    allowed = getattr(dj_settings, "OIDC_ALLOWED_EMAIL_DOMAINS", [])
    if not allowed:
        return True
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    return domain in allowed


def _parse_hire_date(value):
    """Parse Graph's `employeeHireDate` (ISO 8601 string) into a date."""
    if not value:
        return None
    try:
        if "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        return date.fromisoformat(value)
    except (ValueError, AttributeError, TypeError):
        return None


class OrbisOIDCBackend(OIDCAuthenticationBackend):

    def get_userinfo(self, access_token, id_token, payload):
        """Call Graph /me with our $select + $expand. On failure, retry without
        the manager $expand (which requires User.Read.All), then fall back to
        the id_token payload as a last resort. This way, login still works and
        basic profile enrichment happens even when User.Read.All isn't
        admin-consented — only the manager auto-link is skipped.
        """
        try:
            return super().get_userinfo(access_token, id_token, payload)
        except requests.RequestException as exc:
            # When the call fails, peek at the actual scopes Entra granted —
            # makes diagnosing missing-permission 403s a one-line answer
            # instead of a hours-long round-trip with IT.
            scopes = _decode_jwt_payload_unsafe(access_token).get("scp", "")
            logger.warning(
                "Graph /me with $expand=manager failed (%s); token scopes: %r; "
                "retrying without manager expansion.",
                exc, scopes,
            )

        # Retry without $expand=manager. Drops only the manager field; all
        # other selected fields (employeeId, jobTitle, department, etc.) come
        # back fine with just User.Read.
        try:
            response = requests.get(
                "https://graph.microsoft.com/v1.0/me"
                "?$select=id,displayName,givenName,surname,mail,"
                "userPrincipalName,jobTitle,department,officeLocation,"
                "employeeId,employeeHireDate,mobilePhone,companyName",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logger.warning(
                "Graph /me without manager also failed (%s); "
                "falling back to id_token claims (sparse enrichment).",
                exc,
            )
            return payload

    def verify_claims(self, claims):
        """Override the default `email`-only check — Graph /me returns `mail`
        / `userPrincipalName`, not `email`. Accept either source, then apply
        the OIDC_ALLOWED_EMAIL_DOMAINS gate."""
        email = _email_from_graph(claims) or (claims.get("email") or "").strip().lower()
        if not email:
            logger.warning(
                "OIDC verify_claims failed — no email in claims. Keys present: %s",
                sorted(claims.keys()) if isinstance(claims, dict) else type(claims).__name__,
            )
            return False
        if not _domain_allowed(email):
            logger.warning(
                "OIDC reject (domain not allowed): %s — add the domain to "
                "OIDC_ALLOWED_EMAIL_DOMAINS if this identity is legitimate.",
                email,
            )
            return False
        return True

    def filter_users_by_claims(self, claims):
        # Object id first: it survives an email rename in Entra, email doesn't.
        object_id = _object_id_from_claims(claims)
        if object_id:
            by_oid = self.UserModel.objects.filter(entra_object_id=object_id)
            if by_oid.exists():
                return by_oid
        email = _email_from_graph(claims) or (claims.get("email") or "").strip().lower()
        if not email:
            return self.UserModel.objects.none()
        qs = self.UserModel.objects.filter(email__iexact=email)
        if not qs.exists():
            from django.conf import settings as dj_settings
            if not dj_settings.OIDC_CREATE_USER:
                # Only log a "reject" when auto-create is off and the user
                # is genuinely going to bounce. With full JIT, this is just
                # a first-time login (handled by create_user) — not a reject.
                logger.warning(
                    "OIDC reject (user not provisioned): %s — pre-create with "
                    "`manage.py preprovision_sso_user %s` or set "
                    "OIDC_AUTO_CREATE_USERS=1 to auto-onboard.",
                    email, email,
                )
            else:
                logger.info("OIDC first-time login (will auto-create): %s", email)
        return qs

    def create_user(self, claims):
        """Auto-create a CustomUser from Graph claims on first SSO login.

        Overrides mozilla-django-oidc's default for two reasons:

        1. Default reads `claims['email']`, but Graph's `/me` returns `mail`
           / `userPrincipalName` (not `email`). Email is USERNAME_FIELD on
           CustomUser, so we set both consistently.
        2. mozilla-django-oidc's parent `authenticate` does NOT call
           `update_user` for newly-created users — it returns the bare user
           from `create_user` and stops. Without chaining into
           `_provision_from_claims` here, first-time SSO sign-ins would never
           get `employee_id`, `department`, `designation`, `job_location`,
           `mobilePhone`, or `manager` populated from Graph.
        """
        email = _email_from_graph(claims) or (claims.get("email") or "").strip().lower()
        if not email:
            # Defensive: verify_claims should have caught this already.
            raise ValueError("Cannot create OIDC user without an email claim.")
        user = self.UserModel.objects.create_user(
            email=email,
            username=email[:150],
            first_name=(claims.get("givenName") or claims.get("given_name") or "").strip(),
            last_name=(claims.get("surname") or claims.get("family_name") or "").strip(),
        )
        user.set_unusable_password()
        user.save(update_fields=["password"])
        logger.info("OIDC auto-onboarded new user: %s", email)
        self._record_object_id(user, claims)
        # One-time enrichment from Graph — see module docstring: Orbis is the
        # system of record after this, so later logins don't repeat this.
        return self._provision_from_claims(user, claims)

    def update_user(self, user, claims):
        """Called by mozilla-django-oidc on every returning-user login.

        Deliberately no HR-data enrichment: that happens once, in
        `create_user` (first login only). Orbis is the system of record
        after that, so we don't want a stale/edited Entra profile field
        silently overwriting an HR admin's change in Orbis on next login.
        The one thing recorded is the Entra object id, if still missing.
        """
        self._record_object_id(user, claims)
        return user

    def _record_object_id(self, user, claims) -> None:
        """Fill `entra_object_id` when it's empty. Never overwrites, never
        moves an id off another user — both cases are logged for a human and
        the login carries on. Never raises."""
        from django.db import IntegrityError, transaction

        from accounts.models import directory_stamp

        object_id = _object_id_from_claims(claims)
        if not object_id or user.entra_object_id == object_id:
            return
        if user.entra_object_id:
            logger.warning(
                "Entra object id mismatch for %s: Orbis has %s, login brought %s "
                "(account recreated in Entra?) — left unchanged; fix in the admin.",
                user.email, user.entra_object_id, object_id,
            )
            return
        owner = (
            self.UserModel.objects.filter(entra_object_id=object_id)
            .exclude(pk=user.pk).values_list("email", flat=True).first()
        )
        if owner:
            logger.warning(
                "Entra object id %s from %s's login already belongs to %s — not recorded.",
                object_id, user.email, owner,
            )
            return
        try:
            with transaction.atomic():
                # Queryset update: an identifier, so skip CustomUser's
                # post_save handlers (designation/team history, line managers).
                self.UserModel.objects.filter(pk=user.pk, entra_object_id__isnull=True).update(
                    entra_object_id=object_id, **directory_stamp("entra_object_id"),
                )
        except IntegrityError:  # a concurrent login claimed it first
            logger.warning("Entra object id %s for %s lost a race — not recorded.", object_id, user.email)
            return
        user.entra_object_id = object_id
        if hasattr(user, "_directory_snapshot"):
            user._directory_snapshot = user._directory_values()

    def _provision_from_claims(self, user, claims):
        """One-time enrichment of a newly-created user from Graph claims.

        Called only from `create_user` (first SSO login). Never called again
        for this user afterwards — see `update_user`.
        """
        from accounts.models import Department, Designation, JobLocation
        from employees.models import JobInfo

        # Diagnostic: surface which Graph fields actually came back so
        # "nothing populated" reports can be triaged without DB access.
        # Empty `missing` = Graph is fine, Orbis is doing its job.
        # Non-empty `missing` = Entra/Graph sync gap, fix is upstream.
        graph_keys = (
            "employeeId", "jobTitle", "department", "officeLocation",
            "employeeHireDate", "mobilePhone", "manager",
        )
        present = sorted(k for k in graph_keys if claims.get(k))
        missing = sorted(k for k in graph_keys if not claims.get(k))
        logger.info(
            "Graph profile sync for %s: present=%s missing=%s",
            user.email, present, missing,
        )

        # Names (prefer Graph, fall back to id_token claim names if Graph failed).
        user.first_name = (
            claims.get("givenName") or claims.get("given_name") or user.first_name
        )
        user.last_name = (
            claims.get("surname") or claims.get("family_name") or user.last_name
        )

        if claims.get("employeeId"):
            user.employee_id = claims["employeeId"]

        # Hire date is a one-shot enrichment — don't overwrite an admin-set value.
        if not user.joining_date:
            hire_date = _parse_hire_date(claims.get("employeeHireDate"))
            if hire_date:
                user.joining_date = hire_date

        # FK enrichment — auto-provision the reference rows by name. Entra's
        # free-text values do create duplicates ("Human Resource" next to
        # "Human Resources", a full street address next to "MBD" — both seen
        # 2026-09-28); an admin folds those back with
        # `manage.py merge_reference <department|location|designation>`.
        dept_name = (claims.get("department") or "").strip()
        if dept_name:
            user.department, _ = Department.objects.get_or_create(name=dept_name)

        title = (claims.get("jobTitle") or "").strip()
        if title:
            user.designation, _ = Designation.objects.get_or_create(name=title)

        office = (claims.get("officeLocation") or "").strip()
        if office:
            user.job_location, _ = JobLocation.objects.get_or_create(name=office)

        if not user.contact_number and claims.get("mobilePhone"):
            user.contact_number = claims["mobilePhone"]

        user.save()

        # Manager → JobInfo.reports_to. If the manager doesn't exist as an Orbis
        # user yet, create a stub so the org chart isn't broken — the stub gets
        # fully enriched the first time that manager themselves signs in via SSO.
        manager_data = claims.get("manager") or {}
        manager_email = _email_from_graph(manager_data)
        if manager_email:
            logger.info(
                "Graph manager for %s: %s (%s)",
                user.email,
                manager_email,
                manager_data.get("displayName") or "(no displayName)",
            )
            manager_user = self._get_or_create_manager(manager_data)
            if manager_user and manager_user.pk != user.pk:
                ji, _ = JobInfo.objects.get_or_create(employee=user)
                if ji.reports_to_id != manager_user.pk:
                    ji.reports_to = manager_user
                    ji.save(update_fields=["reports_to", "updated_at"])
                    logger.info(
                        "Set %s.reports_to → %s",
                        user.email, manager_user.email,
                    )

        return user

    def _get_or_create_manager(self, manager_data):
        """Resolve a manager from Graph's $expand=manager(...) payload.

        Returns the existing CustomUser if one matches by email; otherwise
        creates a minimal stub (email + displayName + jobTitle) with an
        unusable password. The stub becomes a normal user the first time
        that manager signs in via SSO.
        """
        from accounts.models import Designation

        email = _email_from_graph(manager_data)
        if not email:
            return None

        existing = self.UserModel.objects.filter(email__iexact=email).first()
        if existing:
            return existing

        if not _domain_allowed(email):
            logger.warning(
                "Skipping manager stub for %s — domain not in "
                "OIDC_ALLOWED_EMAIL_DOMAINS.",
                email,
            )
            return None

        display = (manager_data.get("displayName") or "").strip()
        first_name, _, last_name = display.partition(" ")

        stub = self.UserModel.objects.create_user(
            email=email,
            username=email[:150],
            first_name=first_name,
            last_name=last_name,
        )
        stub.set_unusable_password()

        title = (manager_data.get("jobTitle") or "").strip()
        if title:
            stub.designation, _ = Designation.objects.get_or_create(name=title)

        stub.save()
        logger.info(
            "Created manager stub %s (%s); will be enriched on first SSO login.",
            email, display or "(no displayName)",
        )
        return stub


class SuperuserPasswordBackend(ModelBackend):
    """Password login for superusers only — the break-glass path when SSO is on.

    With SSO enabled everyone signs in through Entra (new joiners included:
    IT hands them their mailbox password), so `/login/` shows only the SSO
    button and refuses a password POST. This backend replaces the stock
    ModelBackend in that mode so the one remaining password path — the
    Django admin's own login at /admin/login/ — still works for superusers
    during an Entra outage or an expired client secret, and for nobody else
    (it also closes DRF's default BasicAuthentication to everyone else).
    Without SSO (local dev, no Azure creds) settings keep plain ModelBackend.
    """

    def user_can_authenticate(self, user):
        return super().user_can_authenticate(user) and user.is_superuser
