"""Playwright session shared by sync.py and timelog.py.

No API token exists for op.sebpo.net, so every call rides on a real browser
login: ``--login`` opens Chromium, the user signs in, and the storage state is
saved outside the repo. Later runs reuse it headless and talk to OpenProject's
own ``/api/v3`` (the JSON API its frontend uses) with the session cookie plus
the page's CSRF token. See docs/OPENPROJECT.md.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import yaml
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.getenv("ORBIS_OP_CONFIG_DIR", Path.home() / ".config" / "orbis-openproject"))
AUTH_FILE = CONFIG_DIR / "auth.json"


def load_config() -> dict:
    with open(HERE / "config.yaml") as f:
        return yaml.safe_load(f)


class _InventoryLoader(yaml.SafeLoader):
    """SafeLoader that reads every item of a ``commits:`` list as a string.

    A short hash can be all digits (``7818098`` → int) or start with a zero
    (``0123456`` → YAML 1.1 octal, whose str() isn't the hash any more), so
    converting after the load can't recover it. Retagging the scalar nodes as
    ``str`` before construction keeps the exact characters from the file.
    """


def _construct_inventory_mapping(loader, node, deep=False):
    for key_node, value_node in node.value:
        if getattr(key_node, "value", None) == "commits" and isinstance(value_node, yaml.SequenceNode):
            for item in value_node.value:
                if isinstance(item, yaml.ScalarNode):
                    item.tag = "tag:yaml.org,2002:str"
    return loader.construct_mapping(node, deep=deep)


_InventoryLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_inventory_mapping)


def load_inventory() -> list[dict]:
    with open(HERE / "inventory.yaml") as f:
        return yaml.load(f, Loader=_InventoryLoader)  # noqa: S506 — SafeLoader subclass


TOOLING_KEY = "infra.openproject"   # owns commits that only touch tools/openproject/


def commit_owners(inv: list[dict]) -> dict[str, str]:
    """{short hash: inventory key}. Explicit `commits:` lists win; a commit that
    only touches tools/openproject/ (e.g. recording a hash in the inventory)
    belongs to the tooling entry automatically, so the check can always pass."""
    import subprocess

    owner = {h[:7]: e["key"] for e in inv for h in (e.get("commits") or [])}
    out = subprocess.run(["git", "-C", str(HERE.parents[1]), "log", "--no-merges",
                          "--format=@%h", "--name-only"],
                         capture_output=True, text=True, check=True).stdout
    for block in out.split("@")[1:]:
        lines = [l for l in block.splitlines() if l.strip()]
        h, files = lines[0][:7], lines[1:]
        if h not in owner and files and all(f.startswith("tools/openproject/") for f in files):
            owner[h] = TOOLING_KEY
    return owner


class SessionExpired(RuntimeError):
    pass


class ApiError(RuntimeError):
    def __init__(self, status: int, body):
        self.status, self.body = status, body
        msg = body.get("message") if isinstance(body, dict) else str(body)[:300]
        super().__init__(f"HTTP {status}: {msg}")


def login(base_url: str) -> None:
    """Open a visible browser, let the user sign in, save the session."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(f"{base_url}/login")
        print("Sign in to OpenProject in the browser window (SSO or password).")
        print("This window closes by itself once you're signed in.")
        while True:
            page.wait_for_timeout(2000)
            try:
                r = ctx.request.get(f"{base_url}/api/v3/users/me",
                                    headers={"X-Requested-With": "XMLHttpRequest"})
                if r.ok and r.json().get("_type") == "User":
                    me = r.json()
                    break
            except Exception:
                pass
        ctx.storage_state(path=str(AUTH_FILE))
        os.chmod(AUTH_FILE, 0o600)
        browser.close()
    print(f"Signed in as {me.get('name')} — session saved to {AUTH_FILE}")


class OpenProject:
    """Context manager: headless browser + JSON helpers over the saved session."""

    def __init__(self, cfg: dict, headless: bool = True):
        self.cfg = cfg
        self.base = cfg["base_url"].rstrip("/")
        self.headless = headless

    def __enter__(self):
        if not AUTH_FILE.exists():
            raise SessionExpired("No saved session — run with --login first.")
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.headless)
        self.ctx = self.browser.new_context(storage_state=str(AUTH_FILE))
        self.page = self.ctx.new_page()
        self.page.goto(f"{self.base}/projects/{self.cfg['project']}")
        token = self.page.locator('meta[name="csrf-token"]')
        self.csrf = token.get_attribute("content") if token.count() else None
        me = self.get("/api/v3/users/me")
        if me.get("_type") != "User":
            raise SessionExpired("Saved session has expired — run with --login again.")
        self.me = me
        # Persist refreshed cookies so the session keeps rolling forward.
        self.ctx.storage_state(path=str(AUTH_FILE))
        return self

    def __exit__(self, *exc):
        try:
            self.browser.close()
        finally:
            self._pw.stop()

    # -- JSON API -------------------------------------------------------
    def _headers(self):
        h = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/hal+json",
             "Content-Type": "application/json"}
        if self.csrf:
            h["X-CSRF-Token"] = self.csrf
        return h

    def request(self, method: str, path: str, body: dict | None = None, params: dict | None = None):
        url = path if path.startswith("http") else f"{self.base}{path}"
        r = self.ctx.request.fetch(url, method=method, headers=self._headers(),
                                   data=json.dumps(body) if body is not None else None,
                                   params=params)
        try:
            data = r.json()
        except Exception:
            data = r.text()
        if r.status == 401:
            raise SessionExpired("OpenProject answered 401 — run with --login again.")
        if not r.ok:
            raise ApiError(r.status, data)
        return data

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, body):
        return self.request("POST", path, body)

    def patch(self, path, body):
        return self.request("PATCH", path, body)

    def collect(self, path: str, params: dict | None = None, page_size: int = 200) -> list[dict]:
        """All elements of a paged HAL collection."""
        out, offset = [], 1
        while True:
            p = dict(params or {}, pageSize=page_size, offset=offset)
            data = self.get(path, p)
            els = data.get("_embedded", {}).get("elements", [])
            out += els
            if len(out) >= data.get("total", 0) or not els:
                return out
            offset += 1

    # -- Rails HTML forms (wiki, categories have no write API) ------------
    def submit_form(self, url: str, form_selector: str, mutate) -> int:
        """Load a page, read its form's current fields, let ``mutate`` change
        them (it gets and returns a dict), and POST the form as the browser would.

        Used where OpenProject has no write API (wiki pages). Reading the real
        form keeps the authenticity token, lock version and any hidden fields
        intact without hard-coding OpenProject's field names.
        """
        self.page.goto(url)
        form = self.page.locator(form_selector).first
        if not form.count():
            raise ApiError(404, f"no form matching {form_selector!r} on {url}")
        fields = form.evaluate(
            "f => Array.from(new FormData(f).entries()).filter(([k,v]) => typeof v === 'string')"
        )
        action = form.evaluate("f => f.action")
        data = mutate(dict(fields))
        r = self.ctx.request.post(action, form=data, max_redirects=0)
        if r.status >= 400:
            raise ApiError(r.status, r.text()[:500])
        return r.status


def href(kind: str, ident) -> dict:
    return {"href": f"/api/v3/{kind}/{quote(str(ident))}"}


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)
