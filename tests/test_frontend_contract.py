from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
import re

import pytest
import quickjs
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        upload_dir=tmp_path / "uploads",
        database_path=tmp_path / "timeline.sqlite3",
        session_secret="test-session-secret",
        session_days=30,
        undo_seconds=30,
        max_upload_size=2 * 1024,
        allowed_extensions={".txt", ".md"},
        allowed_origins=["*"],
        auth_token="secret-token",
        rate_limit_count=0,
        rate_limit_window_seconds=60,
        retention_days=0,
    )
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/session",
        json={
            "access_token": settings.auth_token,
            "device_id": "frontend-tests",
            "device_name": "Frontend tests",
        },
    )
    assert response.status_code == 200
    return client


def read_web(path: str) -> str:
    return (Path(__file__).resolve().parent.parent / "web" / path).read_text()


class InlineStyleAttributeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.found = self.found or any(name.lower() == "style" for name, _ in attrs)

    handle_startendtag = handle_starttag


class ShellContractParser(HTMLParser):
    NAV_LABELS = {"主导航", "移动端主导航"}

    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.route_labels: dict[str, dict[str, str]] = {}
        self._current_nav: str | None = None
        self._current_route: str | None = None
        self._label_parts: list[str] = []
        self._badge_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if element_id := attributes.get("id"):
            self.ids.append(element_id)
        if tag == "nav" and attributes.get("aria-label") in self.NAV_LABELS:
            self._current_nav = attributes["aria-label"]
            self.route_labels[self._current_nav] = {}
        elif tag == "button" and self._current_nav and attributes.get("data-route"):
            self._current_route = attributes["data-route"]
            self._label_parts = []
        elif tag == "b" and self._current_route:
            self._badge_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "b" and self._badge_depth:
            self._badge_depth -= 1
        elif tag == "button" and self._current_nav and self._current_route:
            self.route_labels[self._current_nav][self._current_route] = "".join(
                self._label_parts
            ).strip()
            self._current_route = None
            self._label_parts = []
        elif tag == "nav" and self._current_nav:
            self._current_nav = None

    def handle_data(self, data: str) -> None:
        if self._current_route and not self._badge_depth:
            self._label_parts.append(data)


def find_inline_style_violations(source_name: str, source: str) -> list[str]:
    violations: list[str] = []
    parser = InlineStyleAttributeParser()
    parser.feed(source)
    if parser.found:
        violations.append(f"{source_name}: style attribute")

    if Path(source_name).suffix.lower() != ".js":
        return violations

    js_inline_style_patterns = {
        "setAttribute('style')": re.compile(
            r"\.setAttribute\s*\(\s*(['\"])style\1\s*,", re.IGNORECASE
        ),
        "element.style assignment": re.compile(
            r"\.style\s*=(?!=)", re.IGNORECASE
        ),
        "element.style property assignment": re.compile(
            r"\.style\s*(?:\.\s*[$A-Z_a-z][$\w]*|\[\s*[^\]]+\s*\])\s*=(?!=)",
            re.IGNORECASE,
        ),
        "element.style.setProperty": re.compile(
            r"\.style\s*\.\s*setProperty\s*\(", re.IGNORECASE
        ),
    }
    violations.extend(
        f"{source_name}: {label}"
        for label, pattern in js_inline_style_patterns.items()
        if pattern.search(source)
    )
    return violations


@pytest.mark.parametrize(
    ("source_name", "source"),
    [
        ("app.js", "const style = getTheme();"),
        ("app.js", "const currentStyle = model.style;"),
        ("app.js", "render(model.style.color);"),
        ("app.js", "const same = element.style === expected;"),
        ("app.js", "element.setAttribute('style');"),
        ("index.html", '<div data-note="style=display:none"></div>'),
    ],
)
def test_inline_style_scanner_ignores_non_dom_style_data(
    source_name: str, source: str
) -> None:
    assert find_inline_style_violations(source_name, source) == []


@pytest.mark.parametrize(
    ("source_name", "source"),
    [
        ("index.html", '<div style="display:none"></div>'),
        ("composer.js", "const row = `<div\n style='width:50%'></div>`;"),
        ("app.js", "element.setAttribute('style', 'display:none');"),
        ("app.js", "element.style = 'display:none';"),
        ("app.js", "element.style.display = 'none';"),
        ("app.js", "element.style['color'] = 'red';"),
        ("app.js", "element.style.setProperty('--progress', '50%');"),
    ],
)
def test_inline_style_scanner_reports_dom_inline_writes(
    source_name: str, source: str
) -> None:
    assert find_inline_style_violations(source_name, source)


def test_csp_and_html_disallow_all_inline_script_and_style_sources(
    client: TestClient,
) -> None:
    response = client.get("/")
    csp = response.headers["content-security-policy"]
    directives = {
        parts[0]: parts[1:]
        for directive in csp.split(";")
        if (parts := directive.strip().split())
    }
    html = response.text

    assert "'unsafe-inline'" not in directives["script-src"]
    assert "'unsafe-inline'" not in directives["style-src"]
    assert re.search(r"\son[a-z]+\s*=", html, flags=re.IGNORECASE) is None
    assert "fonts.googleapis.com" not in read_web("styles.css")

    web_root = Path(__file__).resolve().parent.parent / "web"
    sources = [web_root / "index.html", *sorted((web_root / "js").rglob("*.js"))]
    violations = [
        violation
        for source in sources
        for violation in find_inline_style_violations(
            str(source.relative_to(web_root)), source.read_text(encoding="utf-8")
        )
    ]
    assert violations == []


QUICKJS_BROWSER_STUBS = r"""
globalThis.__modules = Object.create(null);
globalThis.__elements = Object.create(null);
globalThis.__elementDefinitions = {
  accessToken: { tagName: 'input' },
  deviceName: { tagName: 'input' },
  unlockSubmit: { tagName: 'button' },
  composerTextarea: { tagName: 'textarea' },
  gridViewBtn: { tagName: 'button' },
  listViewBtn: { tagName: 'button' },
  closePreviewBtn: { tagName: 'button' },
  skipLink: { tagName: 'a', href: '#mainContent' },
  mainContent: { tagName: 'main', tabindex: '-1' },
  timelineContainer: { tagName: 'div', tabindex: '-1' },
  libraryView: { tagName: 'section', tabindex: '-1' },
};

class Element {
  constructor(tagName = 'div', id = '') {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.inert = false;
    this.attributes = Object.create(null);
    this.className = '';
    this._textContent = '';
    this._innerHTML = '';
    Object.defineProperty(this, 'textContent', {
      get: () => this._textContent,
      set: value => {
        this._textContent = String(value);
        this._innerHTML = this._textContent
          .replace(/&/g, '&amp;').replace(/</g, '&lt;')
          .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
      },
    });
    Object.defineProperty(this, 'innerHTML', {
      get: () => this._innerHTML,
      set: value => { this._innerHTML = String(value); this._textContent = ''; this.children = []; },
    });
    this.value = '';
    this.files = [];
    this.scrollHeight = 0;
    this.scrollTop = 0;
    this.clientHeight = 600;
    this.offsetTop = 0;
    this.offsetHeight = 0;
    this.listeners = {};
    this.classList = {
      add: (...names) => { this.className = [...new Set(this.className.split(/\s+/).filter(Boolean).concat(names))].join(' '); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter(name => !names.includes(name)).join(' '); },
      contains: name => this.className.split(/\s+/).includes(name),
      toggle: (name, force) => {
        const active = force === undefined ? !this.classList.contains(name) : force;
        active ? this.classList.add(name) : this.classList.remove(name);
        return active;
      },
    };
  }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  removeEventListener(type, listener) {
    this.listeners[type] = (this.listeners[type] || []).filter(item => item !== listener);
  }
  append(...nodes) { for (const node of nodes) { node.parentNode = this; this.children.push(node); } }
  insertBefore(node, reference) {
    node.parentNode = this;
    const index = this.children.indexOf(reference);
    index < 0 ? this.children.push(node) : this.children.splice(index, 0, node);
  }
  before(node) { if (this.parentNode) this.parentNode.insertBefore(node, this); }
  replaceWith(node) {
    if (!this.parentNode) return;
    const index = this.parentNode.children.indexOf(this);
    node.parentNode = this.parentNode;
    this.parentNode.children[index] = node;
  }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); }
  setAttribute(name, value) { this.attributes[name] = String(value); this[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; delete this[name]; }
  focus() {
    const nativeFocusable = ['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA'].includes(this.tagName)
      || (this.tagName === 'A' && Boolean(this.href || this.getAttribute('href')));
    if (!this.disabled && (nativeFocusable || this.getAttribute('tabindex') !== null)) {
      document.activeElement = this;
    }
  }
  click() {}
  scrollTo(options) { this.scrollTop = options.top || 0; }
  scrollIntoView() {
    globalThis.__scrollIntoViewCalls = (globalThis.__scrollIntoViewCalls || 0) + 1;
    if (typeof globalThis.__scrollIntoViewEffect === 'function') {
      globalThis.__scrollIntoViewEffect(this);
    }
  }
  getBoundingClientRect() {
    const key = this.dataset.messageId || this.id;
    const configured = globalThis.__rects && globalThis.__rects[key];
    if (configured) return configured;
    return {
      top: this.offsetTop,
      bottom: this.offsetTop + this.offsetHeight,
      left: 0,
      right: 0,
      width: 0,
      height: this.offsetHeight,
    };
  }
  closest(selector) {
    if (this.matches(selector)) return this;
    return this.parentNode ? this.parentNode.closest(selector) : null;
  }
  matches(selector) {
    if (selector.startsWith('#')) return this.id === selector.slice(1);
    if (selector === 'a') return this.tagName === 'A';
    const classMatch = selector.match(/^\.([\w-]+)/);
    if (classMatch && !this.classList.contains(classMatch[1])) return false;
    const dataMatch = selector.match(/\[data-([\w-]+)="([^"]+)"\]/);
    if (dataMatch) {
      const key = dataMatch[1].replace(/-([a-z])/g, (_, char) => char.toUpperCase());
      if (this.dataset[key] !== dataMatch[2]) return false;
    }
    return Boolean(classMatch || dataMatch);
  }
  querySelector(selector) {
    if (selector.startsWith('#')) return document.getElementById(selector.slice(1));
    for (const child of this.children) {
      if (child.matches(selector)) return child;
      const nested = child.querySelector(selector);
      if (nested) return nested;
    }
    return null;
  }
  querySelectorAll(selector) {
    const matches = [];
    for (const child of this.children) {
      if (child.matches(selector)) matches.push(child);
      matches.push(...child.querySelectorAll(selector));
    }
    return matches;
  }
}

globalThis.document = {
  activeElement: null,
  body: new Element('body', 'body'),
  listeners: Object.create(null),
  createElement: tag => new Element(tag),
  createTextNode: text => { const node = new Element('#text'); node.textContent = String(text); return node; },
  getElementById: id => {
    if (__elements[id]) return __elements[id];
    const definition = __elementDefinitions[id] || {};
    const element = new Element(definition.tagName || 'div', id);
    if (definition.href) element.setAttribute('href', definition.href);
    if (definition.tabindex !== undefined) element.setAttribute('tabindex', definition.tabindex);
    __elements[id] = element;
    return element;
  },
  querySelector: selector => selector.startsWith('#') ? document.getElementById(selector.slice(1)) : new Element('div'),
  querySelectorAll: selector => document.body.querySelectorAll(selector),
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); },
  removeEventListener(type, listener) {
    this.listeners[type] = (this.listeners[type] || []).filter(item => item !== listener);
  },
};
globalThis.window = globalThis;
window.scrollY = 0;
window.scrollTo = () => {};
window.listeners = Object.create(null);
window.addEventListener = (type, listener) => { (window.listeners[type] ||= []).push(listener); };
window.removeEventListener = (type, listener) => {
  window.listeners[type] = (window.listeners[type] || []).filter(item => item !== listener);
};
window.dispatchEvent = event => {
  for (const listener of window.listeners[event.type] || []) listener(event);
  return true;
};
window.setTimeout = () => 1;
window.clearTimeout = () => {};
globalThis.setTimeout = window.setTimeout;
globalThis.location = { origin: 'http://testserver', hash: '' };
globalThis.history = { replaceState(_state, _title, hash) { location.hash = hash; } };
globalThis.navigator = { clipboard: { writeText: () => Promise.resolve() } };
globalThis.localStorage = { values: {}, getItem(key) { return this.values[key] || null; }, setItem(key, value) { this.values[key] = String(value); } };
globalThis.crypto = { randomUUID: () => '00000000-0000-4000-8000-000000000001' };
globalThis.CustomEvent = class CustomEvent { constructor(type, options = {}) { this.type = type; this.detail = options.detail; } };
globalThis.URLSearchParams = class URLSearchParams { constructor() { this.values = []; } set(key, value) { this.values.push([key, value]); } toString() { return this.values.map(item => item.join('=')).join('&'); } };
globalThis.URL = class URL { static createObjectURL() { return 'blob:test'; } static revokeObjectURL() {} };
globalThis.confirm = () => true;
globalThis.fetch = () => Promise.resolve({
  status: 200,
  ok: true,
  headers: { get: () => 'application/json' },
  json: () => Promise.resolve({}),
  text: () => Promise.resolve(''),
  blob: () => Promise.resolve({}),
});
"""


def create_js_context() -> quickjs.Context:
    context = quickjs.Context()
    context.eval(QUICKJS_BROWSER_STUBS)
    return context


def test_quickjs_focus_matches_programmatic_focusability() -> None:
    context = create_js_context()

    result = json.loads(context.eval(r"""
      const plain = document.createElement('div');
      const button = document.createElement('button');
      plain.focus();
      const plainFocused = document.activeElement === plain;
      button.focus();
      const nativeFocused = document.activeElement === button;
      plain.setAttribute('tabindex', '-1');
      plain.focus();
      JSON.stringify({
        plainFocused,
        nativeFocused,
        tabindexFocused: document.activeElement === plain,
      });
    """))

    assert result == {
        "plainFocused": False,
        "nativeFocused": True,
        "tabindexFocused": True,
    }


def transform_module(source: str, module_path: str) -> str:
    exports = re.findall(
        r"\bexport\s+(?:async\s+)?(?:function|class|const|let|var)\s+(\w+)", source
    )
    default_match = re.search(
        r"\bexport\s+default\s+(?:async\s+)?(?:function|class)\s+(\w+)", source
    )

    import_index = 0

    def replace_import(match: re.Match[str]) -> str:
        nonlocal import_index
        clause, dependency = match.group(1).strip(), match.group(2)
        module_ref = f"__importedModule{import_index}"
        import_index += 1
        bindings = [
            f"const {module_ref} = globalThis.__modules[{json.dumps(dependency)}];",
            (
                f"if ({module_ref} === undefined) "
                f"throw new SyntaxError('Module {json.dumps(dependency)} not loaded');"
            ),
        ]

        def bind_export(imported: str, local: str) -> None:
            bindings.append(
                f"if (!Object.prototype.hasOwnProperty.call({module_ref}, {json.dumps(imported)})) "
                f"throw new SyntaxError('Export {imported} is not exported by module {dependency}');"
            )
            bindings.append(f"const {local} = {module_ref}.{imported};")

        if clause.startswith("{"):
            for item in clause[1:-1].split(","):
                parts = item.strip().split()
                if not parts:
                    continue
                imported, local = parts[0], parts[-1]
                bind_export(imported, local)
        elif clause.startswith("* as "):
            bindings.append(f"const {clause[5:].strip()} = {module_ref};")
        else:
            bind_export("default", clause)
        return "\n".join(bindings)

    transformed = re.sub(
        r"\bimport\s+(.+?)\s+from\s+['\"]([^'\"]+)['\"]\s*;",
        replace_import,
        source,
    )
    transformed = re.sub(r"\bexport\s+default\s+", "", transformed)
    transformed = re.sub(r"\bexport\s+", "", transformed)
    registrations = [f"{name}: {name}" for name in exports]
    if default_match:
        registrations.append(f"default: {default_match.group(1)}")
    return (
        "(function() { 'use strict';\n"
        f"{transformed}\n"
        f"globalThis.__modules[{json.dumps(module_path)}] = {{{', '.join(registrations)}}};\n"
        "})();"
    )


def load_js_module(context: quickjs.Context, module_path: str, source: str) -> None:
    if module_path != "./config.js":
        try:
            context.eval('typeof globalThis.__modules["./config.js"]')
            if 'undefined' in str(context.eval('typeof globalThis.__modules["./config.js"]')):
                context.eval(transform_module(read_web("js/config.js"), "./config.js"))
        except Exception:
            context.eval(transform_module(read_web("js/config.js"), "./config.js"))
    if module_path == "./app.js":
        try:
            navigation_type = context.eval('typeof globalThis.__modules["./navigation.js"]')
            if "undefined" in str(navigation_type):
                context.eval(transform_module(read_web("js/navigation.js"), "./navigation.js"))
        except Exception:
            context.eval(transform_module(read_web("js/navigation.js"), "./navigation.js"))
    context.eval(transform_module(source, module_path))


def set_json(context: quickjs.Context, name: str, value: object) -> None:
    context.set(f"{name}Json", json.dumps(value))
    context.eval(f"globalThis.{name} = JSON.parse({name}Json);")


def drain_jobs(context: quickjs.Context) -> None:
    while context.execute_pending_job():
        pass


def test_navigation_normalizes_supported_hashes_and_defaults() -> None:
    context = create_js_context()
    load_js_module(context, "./navigation.js", read_web("js/navigation.js"))
    assert context.eval("__modules['./navigation.js'].normalizeRoute('#files')") == "files"
    assert context.eval("__modules['./navigation.js'].normalizeRoute('#manage')") == "manage"
    assert context.eval("__modules['./navigation.js'].normalizeRoute('#unknown')") == "transfer"
    assert context.eval("__modules['./navigation.js'].normalizeRoute('')") == "transfer"


def test_navigation_module_exports_controller_contract() -> None:
    source = read_web("js/navigation.js")
    for token in ("ROUTES", "normalizeRoute", "createNavigation", "hashchange", "aria-current"):
        assert token in source


def test_shell_has_three_matching_desktop_and_mobile_routes() -> None:
    html = read_web("index.html")
    for route in ("transfer", "files", "manage"):
        assert html.count(f'data-route="{route}"') == 2
        assert f'data-route-page="{route}"' in html
        assert f'data-route-heading="{route}"' in html
    assert 'data-section=' not in html
    assert 'data-route="activity"' not in html
    assert 'data-route="devices"' not in html


def test_transfer_page_is_timeline_first_and_composer_is_docked() -> None:
    html = read_web("index.html")
    transfer = html[html.index('id="transferPage"'):html.index('id="filesPage"')]
    assert 'class="transfer-workspace"' in transfer
    assert transfer.index('id="timelinePanel"') < transfer.index('id="composerPanel"')
    assert 'class="panel transfer-panel composer-dock"' in transfer
    assert 'class="transfer-status"' in transfer


def test_transfer_timeline_has_bounded_internal_scroll_at_all_viewports() -> None:
    css = read_web("styles.css")
    workspace = css[css.index(".transfer-workspace {"):css.index(".transfer-status {")]
    timeline_panel_start = css.rindex(".timeline-panel {")
    timeline_panel = css[timeline_panel_start:css.index(".composer-form {", timeline_panel_start)]
    timeline_container = css[css.index(".timeline-container {"):css.index(".timeline-date-separator {")]
    mobile = css[css.index("@media (max-width: 720px)"):css.index("@media (max-width: 430px)")]

    assert "height: clamp(" in workspace
    assert "max-height: calc(100dvh" in workspace
    assert "grid-template-rows: auto minmax(0, 1fr) auto" in workspace
    assert "overflow: hidden" in timeline_panel
    assert "min-height: 0" in timeline_container
    assert "max-height: clamp(" in timeline_container
    assert "overflow-y: auto" in timeline_container
    assert "height: auto" in mobile
    assert "min-height: max(" in mobile
    assert "max-height: none" in mobile
    assert "var(--mobile-fixed-offset)" in mobile
    assert ".transfer-workspace .timeline-container" in mobile
    assert ".composer-dock .panel-head" in mobile


def test_mobile_transfer_layout_budget_for_short_and_tall_viewports() -> None:
    css = read_web("styles.css")
    mobile_start = css.index("@media (max-width: 720px)")
    compact_start = css.index("@media (max-width: 430px)")
    mobile = css[mobile_start:compact_start]

    def rule(source: str, selector: str) -> str:
        match = re.search(rf"{re.escape(selector)}\s*\{{([^{{}}]*)\}}", source)
        assert match, f"missing CSS rule for {selector}"
        return match.group(1)

    def declaration(block: str, name: str) -> str:
        match = re.search(rf"(?:^|;)\s*{re.escape(name)}\s*:\s*([^;]+)", block)
        assert match, f"missing CSS declaration for {name}"
        return match.group(1).strip()

    def pixels(expression: str) -> float:
        match = re.search(r"(-?\d+(?:\.\d+)?)px", expression)
        assert match, f"missing pixel value in {expression}"
        return float(match.group(1))

    def clamp_pixels(expression: str, viewport_height: int) -> float:
        match = re.fullmatch(
            r"clamp\((\d+)px,\s*(\d+(?:\.\d+)?)dvh,\s*(\d+)px\)",
            expression,
        )
        assert match, f"unsupported clamp expression {expression}"
        minimum, viewport_ratio, maximum = map(float, match.groups())
        return min(max(minimum, viewport_height * viewport_ratio / 100), maximum)

    def layout(
        stylesheet: str, viewport_width: int, viewport_height: int
    ) -> dict[str, float]:
        stylesheet_root = stylesheet[stylesheet.index(":root {"):stylesheet.index(".dark {")]
        stylesheet_mobile_start = stylesheet.index("@media (max-width: 720px)")
        stylesheet_compact_start = stylesheet.index("@media (max-width: 430px)")
        stylesheet_mobile = stylesheet[stylesheet_mobile_start:stylesheet_compact_start]
        stylesheet_compact = stylesheet[stylesheet_compact_start:]
        assert viewport_width <= 430 < 720

        root_vars = rule(stylesheet_root, ":root")
        mobile_vars = rule(stylesheet_mobile, ":root")
        compact_vars = rule(stylesheet_compact, ":root")
        workspace_rule = rule(stylesheet_mobile, ".transfer-workspace")
        timeline_rule = rule(stylesheet_mobile, ".transfer-workspace .timeline-container")
        composer_rule = rule(stylesheet_mobile, ".composer-dock")
        body_rule = rule(stylesheet_mobile, "body")
        nav_rule = rule(stylesheet_mobile, ".mobile-nav")
        compact_topbar_rule = rule(stylesheet_compact, ".topbar")

        shell_variable = "var(--mobile-timeline-shell-min-height)"
        inner_variable = "var(--mobile-timeline-min-height)"
        assert declaration(workspace_rule, "grid-template-rows") == (
            f"auto minmax({shell_variable}, 1fr) auto"
        )
        assert declaration(timeline_rule, "min-height") == inner_variable
        assert declaration(workspace_rule, "min-height") == (
            "max(var(--mobile-workspace-min-height), calc(100dvh - "
            "var(--topbar-height) - var(--mobile-fixed-offset) - "
            "var(--transfer-page-offset)))"
        )
        assert declaration(composer_rule, "position") == "relative"
        assert declaration(composer_rule, "bottom") == "auto"
        assert declaration(composer_rule, "scroll-margin-bottom") == "var(--mobile-fixed-offset)"
        assert declaration(compact_topbar_rule, "height") == "var(--topbar-height)"

        topbar_height = pixels(declaration(compact_vars, "--topbar-height"))
        nav_offset = pixels(declaration(root_vars, "--mobile-fixed-offset"))
        page_offset = pixels(declaration(mobile_vars, "--transfer-page-offset"))
        workspace_floor = pixels(declaration(mobile_vars, "--mobile-workspace-min-height"))
        timeline_shell_min = pixels(
            declaration(mobile_vars, "--mobile-timeline-shell-min-height")
        )
        timeline_inner_min = pixels(
            declaration(mobile_vars, "--mobile-timeline-min-height")
        )
        timeline_max = clamp_pixels(
            declaration(timeline_rule, "max-height"), viewport_height
        )
        composer_max = clamp_pixels(
            declaration(composer_rule, "max-height"), viewport_height
        )
        nav_height = pixels(declaration(nav_rule, "height"))
        body_clearance = pixels(declaration(body_rule, "padding-bottom"))
        viewport_budget = viewport_height - topbar_height - nav_offset - page_offset
        workspace_height = max(workspace_floor, viewport_budget)

        assert timeline_shell_min > timeline_inner_min > 0
        assert timeline_max >= timeline_inner_min
        assert nav_offset >= nav_height
        assert body_clearance >= nav_height
        return {
            "viewport_width": viewport_width,
            "workspace": workspace_height,
            "page_scroll": max(0, workspace_height - viewport_budget),
            "timeline_shell_min": timeline_shell_min,
            "timeline_inner_min": timeline_inner_min,
            "timeline_max": round(timeline_max, 2),
            "composer_max": round(composer_max, 2),
            "composer_nav_gap": nav_offset - nav_height,
            "page_nav_clearance": body_clearance - nav_height,
        }

    assert layout(css, 390, 568) == {
        "viewport_width": 390,
        "workspace": 520,
        "page_scroll": 220,
        "timeline_shell_min": 220,
        "timeline_inner_min": 120,
        "timeline_max": 160,
        "composer_max": 181.76,
        "composer_nav_gap": 8,
        "page_nav_clearance": 4,
    }
    assert layout(css, 390, 844) == {
        "viewport_width": 390,
        "workspace": 576,
        "page_scroll": 0,
        "timeline_shell_min": 220,
        "timeline_inner_min": 120,
        "timeline_max": 236.32,
        "composer_max": 240,
        "composer_nav_gap": 8,
        "page_nav_clearance": 4,
    }

    def mutate_mobile(old: str, new: str) -> str:
        assert old in mobile
        return css[:mobile_start] + mobile.replace(old, new, 1) + css[compact_start:]

    broken_stylesheets = (
        mutate_mobile(
            "minmax(var(--mobile-timeline-shell-min-height), 1fr)",
            "minmax(0, 1fr)",
        ),
        mutate_mobile(
            "min-height: var(--mobile-timeline-min-height);",
            "min-height: 0;",
        ),
        mutate_mobile(
            "--mobile-timeline-shell-min-height: 220px;",
            "--removed-timeline-shell-min-height: 220px;",
        ),
        mutate_mobile(
            "--mobile-timeline-min-height: 120px;",
            "--removed-timeline-min-height: 120px;",
        ),
        mutate_mobile(".transfer-workspace {", ".removed-transfer-workspace {"),
        mutate_mobile(
            ".transfer-workspace .timeline-container {",
            ".removed-timeline-container {",
        ),
        mutate_mobile(".composer-dock {", ".removed-composer-dock {"),
        mutate_mobile(
            "scroll-margin-bottom: var(--mobile-fixed-offset);",
            "scroll-margin-bottom: 0;",
        ),
        mutate_mobile("body { padding-bottom: 70px; }", "body { padding-bottom: 0; }"),
        mutate_mobile("height: 66px;", "height: auto;"),
    )
    for broken_css in broken_stylesheets:
        with pytest.raises(AssertionError):
            layout(broken_css, 390, 568)


def test_transfer_route_dead_styles_are_removed() -> None:
    css = read_web("styles.css")
    for selector in (".transfer-route", ".route-node", "route-pulse"):
        assert selector not in css


def test_shell_ids_are_unique_and_navigation_labels_match_route_hashes() -> None:
    parser = ShellContractParser()
    parser.feed(read_web("index.html"))
    duplicate_ids = sorted({element_id for element_id in parser.ids if parser.ids.count(element_id) > 1})
    assert duplicate_ids == []

    expected_labels = {"transfer": "传输", "files": "文件", "manage": "管理"}
    assert parser.route_labels == {
        "主导航": expected_labels,
        "移动端主导航": expected_labels,
    }

    context = create_js_context()
    load_js_module(context, "./navigation.js", read_web("js/navigation.js"))
    set_json(context, "shellRoutes", list(expected_labels))
    hashes = json.loads(context.eval(r"""
      const routeHashes = {};
      for (const route of shellRoutes) {
        let clickHandler = null;
        const button = {
          dataset: { route },
          classList: { toggle() {} },
          setAttribute() {}, removeAttribute() {},
          addEventListener(_type, handler) { clickHandler = handler; },
          removeEventListener() {}, click() { clickHandler(); },
        };
        const windowObject = {
          scrollY: 0,
          location: { hash: '#transfer' },
          history: { replaceState(_state, _title, hash) { windowObject.location.hash = hash; } },
          addEventListener() {}, removeEventListener() {}, scrollTo() {},
        };
        const documentObject = {
          title: '',
          querySelectorAll(selector) { return selector === '[data-route]' ? [button] : []; },
          querySelector() { return null; },
        };
        const navigation = __modules['./navigation.js'].createNavigation({ windowObject, documentObject });
        navigation.start();
        button.click();
        routeHashes[route] = windowObject.location.hash;
      }
      JSON.stringify(routeHashes);
    """))
    assert hashes == {"transfer": "#transfer", "files": "#files", "manage": "#manage"}


def test_app_starts_navigation_and_clears_file_selection_on_route_exit() -> None:
    source = read_web("js/app.js")
    assert "from './navigation.js'" in source
    assert "createNavigation" in source
    assert "navigation.start()" in source
    assert "route !== 'files'" in source
    assert "library.clearSelection?.()" in source


def test_batch_toolbar_cannot_intercept_pointer_events_while_inactive() -> None:
    css = read_web("styles.css")
    base = css[css.index(".batch-toolbar {"):css.index(".batch-toolbar.visible {")]
    visible = css[
        css.index(".batch-toolbar.visible {"):css.index(".batch-toolbar .pill")
    ]
    assert "visibility: hidden" in base
    assert "pointer-events: none" in base
    assert "visibility: visible" in visible
    assert "pointer-events: auto" in visible


def test_files_empty_state_has_transfer_action() -> None:
    source = read_web("js/library.js")
    assert 'id="emptyFilesAction"' in source
    assert 'data-file-action="empty-attach"' in source
    assert "onAttach" in source


def test_library_empty_attach_listener_lifecycle_is_scoped_to_controller() -> None:
    context = create_js_context()
    load_js_module(context, "./library.js", read_web("js/library.js"))
    context.eval(r"""
      const root = document.getElementById('libraryView');
      const fileList = document.getElementById('fileList');
      const emptyAction = document.createElement('button');
      emptyAction.dataset.fileAction = 'empty-attach';
      const api = path => Promise.resolve(
        path.startsWith('/api/files?')
          ? { items: [], next_cursor: null }
          : { file_count: 0, total_size: '0 B', largest_files: [] }
      );
      globalThis.firstAttachCalls = 0;
      globalThis.secondAttachCalls = 0;
      globalThis.dispatchEmptyAttach = () => {
        for (const listener of [...(fileList.listeners.click || [])]) {
          listener({ target: { closest: () => emptyAction } });
        }
      };
      globalThis.firstLibrary = __modules['./library.js'].createLibrary({
        root,
        api,
        timeline: null,
        onAttach: () => { firstAttachCalls += 1; },
      });
      firstLibrary.load({});
    """)
    drain_jobs(context)

    context.eval("dispatchEmptyAttach();")
    assert context.eval("firstAttachCalls") == 1
    assert 'id="emptyFilesAction"' in context.eval(
        "document.getElementById('fileList').innerHTML"
    )

    result = json.loads(context.eval(r"""
      firstLibrary.destroy();
      firstLibrary.destroy();
      dispatchEmptyAttach();
      globalThis.secondLibrary = __modules['./library.js'].createLibrary({
        root,
        api,
        timeline: null,
        onAttach: () => { secondAttachCalls += 1; },
      });
      dispatchEmptyAttach();
      secondLibrary.destroy();
      secondLibrary.destroy();
      dispatchEmptyAttach();
      let remainingListeners = Object.values(document.listeners)
        .reduce((total, listeners) => total + listeners.length, 0);
      for (const element of Object.values(__elements)) {
        remainingListeners += Object.values(element.listeners)
          .reduce((total, listeners) => total + listeners.length, 0);
      }
      JSON.stringify({ firstAttachCalls, secondAttachCalls, remainingListeners });
    """))
    assert result == {
        "firstAttachCalls": 1,
        "secondAttachCalls": 1,
        "remainingListeners": 0,
    }


def test_app_empty_attach_navigation_and_picker_click_are_synchronous() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.attachSequence = [];
      globalThis.userActivation = false;
      document.getElementById('composerFileInput').click = () => {
        attachSequence.push(`click:${userActivation}`);
      };
      __modules['./api.js'] = {
        request: () => Promise.resolve({}),
        unlock: () => Promise.resolve({}),
        logout: () => Promise.resolve({}),
        getSession: () => Promise.resolve({}),
        ApiError: class ApiError extends Error {},
        connectEvents: () => ({ close() {} }),
        getLastSequence: () => 0,
      };
      __modules['./timeline.js'] = {
        createTimeline: () => ({
          loadInitial() {}, mergeEvent() {}, remove() {}, upsert() {},
        }),
      };
      __modules['./composer.js'] = { createComposer: () => ({}) };
      __modules['./library.js'] = {
        createLibrary: options => {
          globalThis.emptyAttach = options.onAttach;
          return { clearSelection() {} };
        },
      };
      __modules['./navigation.js'] = {
        createNavigation: () => ({
          navigate(route) { attachSequence.push(`navigate:${route}:${userActivation}`); },
          start() {},
        }),
      };
    """)
    load_js_module(context, "./app.js", read_web("js/app.js"))

    result = json.loads(context.eval(r"""
      userActivation = true;
      emptyAttach();
      attachSequence.push('returned');
      userActivation = false;
      JSON.stringify(attachSequence);
    """))
    assert result == ["navigate:transfer:true", "click:true", "returned"]


def test_skip_link_preserves_application_route_hash() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.WebSocket = class WebSocket { close() {} };
      location.hash = '#files';
    """)
    load_app_modules(context)
    context.eval(r"""
      document.getElementById('skipLink').listeners.click[0]({ preventDefault() {} });
    """)
    result = json.loads(context.eval(r"""
      JSON.stringify({ hash: location.hash, focus: document.activeElement.id });
    """))
    assert result == {"hash": "#files", "focus": "mainContent"}


def test_navigation_focus_preserves_restored_scroll_position() -> None:
    context = create_js_context()
    load_js_module(context, "./navigation.js", read_web("js/navigation.js"))
    result = json.loads(context.eval(r"""
      const focusOptions = [];
      const scrollCalls = [];
      const windowListeners = {};
      const headings = Object.fromEntries(['transfer', 'files'].map(route => [route, {
        focus(options) {
          focusOptions.push(options || null);
          if (!options || options.preventScroll !== true) windowObject.scrollY = 999;
        },
      }]));
      const windowObject = {
        scrollY: 0,
        location: { hash: '#transfer' },
        history: { replaceState(_state, _title, hash) { windowObject.location.hash = hash; } },
        addEventListener(type, listener) { windowListeners[type] = listener; },
        removeEventListener() {},
        scrollTo(x, y) { scrollCalls.push([x, y]); windowObject.scrollY = y; },
      };
      const documentObject = {
        title: '',
        querySelectorAll() { return []; },
        querySelector(selector) {
          const match = selector.match(/^\[data-route-heading="(.+)"\]$/);
          return match ? headings[match[1]] : null;
        },
      };
      const navigation = __modules['./navigation.js'].createNavigation({ windowObject, documentObject });
      navigation.start();
      windowObject.scrollY = 140;
      navigation.navigate('files');
      windowListeners.hashchange();
      windowObject.scrollY = 60;
      navigation.navigate('transfer');
      windowListeners.hashchange();
      JSON.stringify({ focusOptions, scrollCalls, scrollY: windowObject.scrollY });
    """))
    assert result == {
        "focusOptions": [{"preventScroll": True}, {"preventScroll": True}],
        "scrollCalls": [[0, 0], [0, 0], [0, 140]],
        "scrollY": 140,
    }


def create_navigation_lifecycle_context() -> quickjs.Context:
    context = create_js_context()
    load_js_module(context, "./navigation.js", read_web("js/navigation.js"))
    context.eval(r"""
      globalThis.createNavigationHarness = () => {
        const listeners = () => ({
          values: Object.create(null),
          add(type, listener) { (this.values[type] ||= []).push(listener); },
          remove(type, listener) {
            this.values[type] = (this.values[type] || []).filter(item => item !== listener);
          },
          count(type) { return (this.values[type] || []).length; },
          dispatch(type) { for (const listener of [...(this.values[type] || [])]) listener(); },
        });
        const buttonListeners = listeners();
        const windowListeners = listeners();
        const button = {
          dataset: { route: 'files' },
          classList: { toggle() {} },
          setAttribute() {},
          removeAttribute() {},
          addEventListener: (type, listener) => buttonListeners.add(type, listener),
          removeEventListener: (type, listener) => buttonListeners.remove(type, listener),
          click: () => buttonListeners.dispatch('click'),
        };
        const pages = [
          { dataset: { routePage: 'transfer' }, hidden: false },
          { dataset: { routePage: 'files' }, hidden: true },
        ];
        const title = { textContent: '' };
        const documentObject = {
          title: '',
          querySelectorAll(selector) {
            if (selector === '[data-route]') return [button];
            if (selector === '[data-route-page]') return pages;
            return [];
          },
          querySelector(selector) {
            return selector === '[data-route-title]' ? title : null;
          },
        };
        const windowObject = {
          scrollY: 0,
          location: { hash: '#transfer' },
          history: { replaceState(_state, _title, hash) { windowObject.location.hash = hash; } },
          addEventListener: (type, listener) => windowListeners.add(type, listener),
          removeEventListener: (type, listener) => windowListeners.remove(type, listener),
          scrollTo() {},
        };
        let routeChangeCount = 0;
        const navigation = __modules['./navigation.js'].createNavigation({
          windowObject,
          documentObject,
          onRouteChange: () => { routeChangeCount += 1; },
        });
        return {
          navigation,
          button,
          windowObject,
          buttonListenerCount: () => buttonListeners.count('click'),
          windowListenerCount: () => windowListeners.count('hashchange'),
          routeChangeCount: () => routeChangeCount,
        };
      };
    """)
    return context


def test_navigation_destroy_removes_button_handlers_and_prevents_navigation() -> None:
    context = create_navigation_lifecycle_context()
    result = json.loads(context.eval(r"""
      const harness = createNavigationHarness();
      harness.navigation.start();
      harness.navigation.destroy();
      harness.navigation.destroy();
      harness.button.click();
      JSON.stringify({
        hash: harness.windowObject.location.hash,
        buttonListeners: harness.buttonListenerCount(),
        windowListeners: harness.windowListenerCount(),
      });
    """))

    assert result == {"hash": "#transfer", "buttonListeners": 0, "windowListeners": 0}


def test_navigation_repeated_start_does_not_accumulate_listeners() -> None:
    context = create_navigation_lifecycle_context()
    result = json.loads(context.eval(r"""
      const harness = createNavigationHarness();
      harness.navigation.start();
      harness.navigation.start();
      const repeatedStart = {
        buttonListeners: harness.buttonListenerCount(),
        windowListeners: harness.windowListenerCount(),
        routeChanges: harness.routeChangeCount(),
      };
      harness.navigation.destroy();
      harness.navigation.start();
      const restarted = {
        buttonListeners: harness.buttonListenerCount(),
        windowListeners: harness.windowListenerCount(),
        routeChanges: harness.routeChangeCount(),
      };
      JSON.stringify({ repeatedStart, restarted });
    """))

    assert result == {
        "repeatedStart": {"buttonListeners": 1, "windowListeners": 1, "routeChanges": 1},
        "restarted": {"buttonListeners": 1, "windowListeners": 1, "routeChanges": 2},
    }


def test_frontend_is_split_and_has_unlock_contract(client: TestClient) -> None:
    html = client.get("/").text
    assert '<link rel="stylesheet" href="/styles.css">' in html
    assert '<script type="module" src="/js/app.js"></script>' in html
    assert '<script>' not in html and '<style>' not in html
    for element_id in ("unlockForm", "accessToken", "deviceName", "sessionExpired", "mainContent", "libraryView"):
        assert f'id="{element_id}"' in html
    assert re.search(r'<section[^>]*id="libraryView"[^>]*tabindex="-1"', html)
    assert re.search(r'<div[^>]*id="timelineContainer"[^>]*tabindex="-1"', html)


def test_static_assets_are_served(client: TestClient) -> None:
    css = client.get("/styles.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]

    api_js = client.get("/js/api.js")
    assert api_js.status_code == 200
    assert "javascript" in api_js.headers["content-type"]

    app_js = client.get("/js/app.js")
    assert app_js.status_code == 200
    assert "javascript" in app_js.headers["content-type"]


def test_no_token_leakage_in_source(client: TestClient) -> None:
    html = client.get("/").text
    css = client.get("/styles.css").text
    api_js = client.get("/js/api.js").text
    app_js = client.get("/js/app.js").text

    for source in (html, css, api_js, app_js):
        assert "upload-token" not in source
        assert "X-Upload-Token" not in source
        assert "?token=" not in source


def test_unlock_form_and_session_expired_markup(client: TestClient) -> None:
    html = client.get("/").text

    assert 'id="sessionExpired"' in html
    assert 'id="unlockForm"' in html
    assert 'id="accessToken"' in html
    assert 'id="deviceName"' in html
    assert 'type="password"' in html
    assert 'maxlength="40"' in html
    assert '访问验证' in html
    assert '访问令牌' in html
    assert '设备名称' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-labelledby="unlockTitle"' in html


def load_app_modules(context: quickjs.Context) -> None:
    for module_path in ("./config.js", "./api.js", "./timeline.js", "./composer.js", "./library.js"):
        load_js_module(context, module_path, read_web(f"js/{module_path[2:]}"))
    load_js_module(context, "./app.js", read_web("js/app.js"))
    drain_jobs(context)


def test_unlock_dialog_traps_focus_inerts_background_and_restores_focus() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.WebSocket = class WebSocket { close() {} };
      const overlay = document.getElementById('sessionExpired');
      overlay.classList.add('is-hidden');
      const unlockForm = document.getElementById('unlockForm');
      const token = document.getElementById('accessToken');
      const device = document.getElementById('deviceName');
      const submit = document.getElementById('unlockSubmit');
      unlockForm.append(token, device, submit);
      const previous = document.getElementById('composerTextarea');
      const skip = document.getElementById('skipLink');
      const header = document.getElementById('headerRegion');
      const main = document.getElementById('mainContent');
      const footer = document.getElementById('footerRegion');
      const originallyInert = document.getElementById('originallyInert');
      originallyInert.inert = true;
      document.body.append(skip, header, main, footer, originallyInert, overlay);
      globalThis.previousFocus = previous;
    """)
    load_app_modules(context)

    context.eval(r"""
      previousFocus.focus();
      window.dispatchEvent(new CustomEvent('session-expired'));
    """)
    locked = json.loads(context.eval(r"""
      JSON.stringify({
        skip: document.getElementById('skipLink').inert,
        header: document.getElementById('headerRegion').inert,
        main: document.getElementById('mainContent').inert,
        footer: document.getElementById('footerRegion').inert,
        originallyInert: document.getElementById('originallyInert').inert,
        overlay: document.getElementById('sessionExpired').inert,
      })
    """))
    assert locked == {
        "skip": True,
        "header": True,
        "main": True,
        "footer": True,
        "originallyInert": True,
        "overlay": False,
    }
    assert context.eval("document.activeElement.id") == "accessToken"

    context.eval(r"""
      document.getElementById('unlockSubmit').focus();
      document.getElementById('sessionExpired').listeners.keydown[0]({
        key: 'Tab', shiftKey: false, preventDefault() {},
      });
    """)
    assert context.eval("document.activeElement.id") == "accessToken"

    context.eval(r"""
      document.getElementById('accessToken').focus();
      document.getElementById('sessionExpired').listeners.keydown[0]({
        key: 'Tab', shiftKey: true, preventDefault() {},
      });
    """)
    assert context.eval("document.activeElement.id") == "unlockSubmit"

    context.eval(r"""
      document.getElementById('accessToken').value = 'secret-token';
      document.getElementById('deviceName').value = 'Browser';
      document.getElementById('unlockForm').listeners.submit[0]({ preventDefault() {} });
    """)
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      JSON.stringify({
        skip: document.getElementById('skipLink').inert,
        header: document.getElementById('headerRegion').inert,
        main: document.getElementById('mainContent').inert,
        footer: document.getElementById('footerRegion').inert,
        originallyInert: document.getElementById('originallyInert').inert,
        focus: document.activeElement.id,
      })
    """))
    assert result == {
        "skip": False,
        "header": False,
        "main": False,
        "footer": False,
        "originallyInert": True,
        "focus": "composerTextarea",
    }


def test_view_tabs_update_selection_and_support_arrow_keys() -> None:
    context = create_js_context()
    context.eval(r"""
      const root = document.getElementById('libraryView');
      const grid = document.getElementById('gridViewBtn');
      const list = document.getElementById('listViewBtn');
      grid.setAttribute('role', 'tab');
      grid.setAttribute('aria-selected', 'true');
      list.setAttribute('role', 'tab');
      list.setAttribute('aria-selected', 'false');
    """)
    load_js_module(context, "./library.js", read_web("js/library.js"))
    context.eval(r"""
      __modules['./library.js'].createLibrary({
        root: document.getElementById('libraryView'), api: () => Promise.resolve({}), timeline: null,
      });
      document.getElementById('gridViewBtn').listeners.keydown[0]({
        key: 'ArrowRight', preventDefault() {},
      });
    """)
    result = json.loads(context.eval(r"""
      JSON.stringify({
        gridSelected: document.getElementById('gridViewBtn').getAttribute('aria-selected'),
        listSelected: document.getElementById('listViewBtn').getAttribute('aria-selected'),
        focused: document.activeElement.id,
      })
    """))
    assert result == {"gridSelected": "false", "listSelected": "true", "focused": "listViewBtn"}

    context.eval(r"""
      document.getElementById('listViewBtn').listeners.keydown[0]({
        key: 'ArrowRight', preventDefault() {},
      });
    """)
    wrapped = json.loads(context.eval(r"""
      JSON.stringify({
        gridSelected: document.getElementById('gridViewBtn').getAttribute('aria-selected'),
        listSelected: document.getElementById('listViewBtn').getAttribute('aria-selected'),
        focused: document.activeElement.id,
      })
    """))
    assert wrapped == {"gridSelected": "true", "listSelected": "false", "focused": "gridViewBtn"}


def test_skip_link_focuses_main_content() -> None:
    context = create_js_context()
    context.eval("globalThis.WebSocket = class WebSocket { close() {} };")
    load_app_modules(context)
    context.eval(r"""
      document.getElementById('skipLink').listeners.click[0]({ preventDefault() {} });
    """)
    assert context.eval("document.activeElement.id") == "mainContent"
    css = read_web("styles.css")
    assert ".skip-link:focus," in css
    assert ".skip-link:focus-visible" in css


def test_composer_enter_sends_and_shift_enter_keeps_newline_behavior() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.AbortController = class AbortController { constructor() { this.signal = {}; } };
      globalThis.sentTexts = [];
      __modules['./api.js'] = {
        sendText: text => { sentTexts.push(text); return Promise.resolve({ id: 'm1', body: text }); },
        uploadFile: () => Promise.resolve({}),
      };
    """)
    load_js_module(context, "./composer.js", read_web("js/composer.js"))
    context.eval(r"""
      const textarea = document.getElementById('composerTextarea');
      __modules['./composer.js'].createComposer({
        form: document.getElementById('composerForm'), textarea,
        fileInput: document.getElementById('composerFileInput'),
        dropTarget: document.getElementById('composerDropTarget'),
        queue: document.getElementById('composerQueue'), api: null,
        timeline: { upsert() {} },
      });
      textarea.value = 'send me';
      globalThis.enterPrevented = false;
      textarea.listeners.keydown[0]({ key: 'Enter', shiftKey: false, preventDefault() { enterPrevented = true; } });
    """)
    drain_jobs(context)
    context.eval(r"""
      document.getElementById('composerTextarea').value = 'keep newline';
      globalThis.shiftPrevented = false;
      document.getElementById('composerTextarea').listeners.keydown[0]({
        key: 'Enter', shiftKey: true, preventDefault() { shiftPrevented = true; },
      });
    """)
    context.eval(r"""
      document.getElementById('composerTextarea').value = '正在输入';
      globalThis.composingPrevented = false;
      document.getElementById('composerTextarea').listeners.keydown[0]({
        key: 'Enter', shiftKey: false, isComposing: true,
        preventDefault() { composingPrevented = true; },
      });
    """)
    result = json.loads(context.eval(
        "JSON.stringify({ sentTexts, enterPrevented, shiftPrevented, composingPrevented, value: document.getElementById('composerTextarea').value })"
    ))
    assert result == {
        "sentTexts": ["send me"],
        "enterPrevented": True,
        "shiftPrevented": False,
        "composingPrevented": False,
        "value": "正在输入",
    }


def test_timeline_position_uses_container_scroll_and_message_anchor() -> None:
    source = read_web("js/app.js")
    assert "window.scrollY" not in source
    assert "window.scrollTo" not in source

    context = create_js_context()
    context.eval(r"""
      __modules['./api.js'] = {
        request: () => Promise.resolve({}),
        unlock: () => Promise.resolve({}),
        logout: () => Promise.resolve({}),
        getSession: () => Promise.resolve({}),
        ApiError: class ApiError extends Error {},
        connectEvents: () => ({ close() {} }),
        getLastSequence: () => 0,
      };
      __modules['./timeline.js'] = {
        createTimeline: () => ({
          loadInitial() {}, mergeEvent() {}, remove() {}, upsert() {},
        }),
      };
      __modules['./composer.js'] = { createComposer: () => ({}) };
      __modules['./library.js'] = { createLibrary: () => ({}) };
    """)
    load_js_module(context, "./app.js", source)
    context.eval(r"""
      const container = document.getElementById('timelineContainer');
      globalThis.__rects = {
        timelineContainer: { top: 100, bottom: 700 },
        m1: { top: 60, bottom: 90 },
        m2: { top: 160, bottom: 200 },
      };
      container.scrollTop = 140;
      const first = document.createElement('article');
      first.className = 'timeline-message';
      first.dataset.messageId = 'm1';
      const anchor = document.createElement('article');
      anchor.className = 'timeline-message';
      anchor.dataset.messageId = 'm2';
      container.append(first, anchor);
      globalThis.positionSnapshot = __modules['./app.js'].captureTimelinePosition(container);
      anchor.remove();
      container.scrollTop = 20;
      __rects.m2 = { top: 260, bottom: 300 };
      globalThis.loadedAnchorId = null;
      const timeline = {
        loadUntil(id) {
          loadedAnchorId = id;
          container.append(anchor);
          return Promise.resolve(true);
        },
      };
      globalThis.restoreDone = false;
      __modules['./app.js'].restoreTimelinePosition(container, positionSnapshot, timeline)
        .then(() => { restoreDone = true; });
    """)
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      JSON.stringify({
        snapshot: positionSnapshot,
        loadedAnchorId,
        restored: document.getElementById('timelineContainer').scrollTop,
        restoreDone,
      })
    """))
    assert result == {
        "snapshot": {"scrollTop": 140, "messageId": "m2", "anchorOffset": 60},
        "loadedAnchorId": "m2",
        "restored": 120,
        "restoreDone": True,
    }

    context.eval(r"""
      document.getElementById('timelineContainer').children = [];
      document.getElementById('timelineContainer').scrollTop = 0;
      globalThis.missingDone = false;
      __modules['./app.js'].restoreTimelinePosition(
        document.getElementById('timelineContainer'),
        { scrollTop: 140, messageId: 'missing', anchorOffset: 60 },
        { loadUntil: () => Promise.resolve(false) },
      ).then(() => { missingDone = true; });
    """)
    drain_jobs(context)
    missing = json.loads(context.eval(r"""
      JSON.stringify({
        scrollTop: document.getElementById('timelineContainer').scrollTop,
        missingDone,
      })
    """))
    assert missing == {"scrollTop": 140, "missingDone": True}


def test_real_app_unlock_loads_old_anchor_page_before_restoring_position() -> None:
    context = create_js_context()
    set_json(context, "oldMessage", {
        "id": "old-anchor",
        "kind": "text",
        "body": "old",
        "created_at": "2026-07-16T12:00:00Z",
        "device_name": "Browser",
    })
    set_json(context, "newMessage", {
        "id": "new-message",
        "kind": "text",
        "body": "new",
        "created_at": "2026-07-17T12:00:00Z",
        "device_name": "Browser",
    })
    context.eval(r"""
      globalThis.WebSocket = class WebSocket { close() {} };
      globalThis.reloadMode = false;
      globalThis.messagePage = 0;
      globalThis.__scrollIntoViewCalls = 0;
      globalThis.fetch = path => {
        let payload = {};
        if (path === '/api/messages?limit=50') {
          messagePage += 1;
          payload = reloadMode
            ? { items: [newMessage], next_before: 'older-page' }
            : { items: [oldMessage], next_before: null };
        } else if (path.includes('before=older-page')) {
          messagePage += 1;
          payload = { items: [oldMessage], next_before: null };
        } else if (path.startsWith('/api/files?')) {
          payload = { items: [], next_cursor: null };
        } else if (path === '/api/audit') {
          payload = { events: [] };
        }
        return Promise.resolve({
          status: 200,
          ok: true,
          headers: { get: () => 'application/json' },
          json: () => Promise.resolve(payload),
          text: () => Promise.resolve(''),
        });
      };
      const overlay = document.getElementById('sessionExpired');
      overlay.classList.add('is-hidden');
      document.body.append(
        document.getElementById('skipLink'),
        document.getElementById('mainContent'),
        overlay,
      );
    """)
    load_app_modules(context)
    context.eval(r"""
      globalThis.__rects = {
        timelineContainer: { top: 100, bottom: 700 },
        'old-anchor': { top: 140, bottom: 180 },
        'new-message': { top: 120, bottom: 160 },
      };
      const container = document.getElementById('timelineContainer');
      globalThis.__scrollIntoViewEffect = () => { container.scrollTop = 999; };
      container.scrollTop = 400;
      window.dispatchEvent(new CustomEvent('session-expired'));
      reloadMode = true;
      messagePage = 0;
      __rects['old-anchor'] = { top: 260, bottom: 300 };
      document.getElementById('accessToken').value = 'secret-token';
      document.getElementById('deviceName').value = 'Browser';
      document.getElementById('unlockForm').listeners.submit[0]({ preventDefault() {} });
    """)
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      JSON.stringify({
        messagePage,
        hasOldAnchor: Boolean(document.getElementById('timelineContainer').querySelector('[data-message-id="old-anchor"]')),
        scrollTop: document.getElementById('timelineContainer').scrollTop,
        scrollIntoViewCalls: __scrollIntoViewCalls,
      })
    """))
    assert result == {
        "messagePage": 2,
        "hasOldAnchor": True,
        "scrollTop": 120,
        "scrollIntoViewCalls": 0,
    }


def test_unlock_waits_for_reload_and_restores_equivalent_rebuilt_action() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.resolveReload = null;
      globalThis.reloadGate = new Promise(resolve => { resolveReload = resolve; });
      globalThis.loadInitialCount = 0;
      globalThis.replacementHasAction = true;
      __modules['./api.js'] = {
        request: () => Promise.resolve({}),
        unlock: () => Promise.resolve({}),
        logout: () => Promise.resolve({}),
        getSession: () => Promise.resolve({}),
        ApiError: class ApiError extends Error {},
        connectEvents: () => ({ close() {} }),
        getLastSequence: () => 0,
      };
      __modules['./timeline.js'] = {
        createTimeline: options => ({
          loadInitial() {
            loadInitialCount += 1;
            if (loadInitialCount === 1) return Promise.resolve();
            return reloadGate.then(async () => {
              const container = document.getElementById('timelineContainer');
              container.children = [];
              const message = document.createElement('article');
              message.className = 'timeline-message';
              message.dataset.messageId = 'stable-message';
              if (replacementHasAction) {
                const action = document.createElement('button');
                action.dataset.timelineAction = 'copy';
                message.append(action);
              }
              container.append(message);
              await options.onRestore();
            });
          },
          loadUntil: () => Promise.resolve(false),
          mergeEvent() {}, remove() {}, upsert() {},
        }),
      };
      __modules['./composer.js'] = { createComposer: () => ({}) };
      __modules['./library.js'] = { createLibrary: () => ({ load: () => Promise.resolve() }) };

      const overlay = document.getElementById('sessionExpired');
      overlay.classList.add('is-hidden');
      const main = document.getElementById('mainContent');
      main.append(document.getElementById('timelineContainer'));
      document.body.append(document.getElementById('skipLink'), main, overlay);
    """)
    load_js_module(context, "./app.js", read_web("js/app.js"))
    drain_jobs(context)

    context.eval(r"""
      const container = document.getElementById('timelineContainer');
      const message = document.createElement('article');
      message.className = 'timeline-message';
      message.dataset.messageId = 'stable-message';
      const action = document.createElement('button');
      action.dataset.timelineAction = 'copy';
      message.append(action);
      container.append(message);
      globalThis.originalAction = action;
      action.focus();
      window.dispatchEvent(new CustomEvent('session-expired'));
      document.getElementById('accessToken').value = 'secret-token';
      document.getElementById('deviceName').value = 'Browser';
      document.getElementById('unlockForm').listeners.submit[0]({ preventDefault() {} });
    """)
    drain_jobs(context)

    pending = json.loads(context.eval(r"""
      JSON.stringify({
        overlayVisible: !document.getElementById('sessionExpired').classList.contains('is-hidden'),
        backgroundInert: document.getElementById('mainContent').inert,
        focused: document.activeElement.id,
      })
    """))
    assert pending == {
        "overlayVisible": True,
        "backgroundInert": True,
        "focused": "accessToken",
    }

    context.eval("resolveReload();")
    drain_jobs(context)
    restored = json.loads(context.eval(r"""
      const replacement = document.getElementById('timelineContainer')
        .querySelector('[data-message-id="stable-message"]')
        .querySelector('[data-timeline-action="copy"]');
      JSON.stringify({
        overlayHidden: document.getElementById('sessionExpired').classList.contains('is-hidden'),
        backgroundInert: document.getElementById('mainContent').inert,
        focusedEquivalent: document.activeElement === replacement,
        focusedDetachedOriginal: document.activeElement === originalAction,
      });
    """))
    assert restored == {
        "overlayHidden": True,
        "backgroundInert": False,
        "focusedEquivalent": True,
        "focusedDetachedOriginal": False,
    }

    context.eval(r"""
      replacementHasAction = false;
      const currentAction = document.getElementById('timelineContainer')
        .querySelector('[data-message-id="stable-message"]')
        .querySelector('[data-timeline-action="copy"]');
      currentAction.focus();
      window.dispatchEvent(new CustomEvent('session-expired'));
      document.getElementById('accessToken').value = 'secret-token';
      document.getElementById('deviceName').value = 'Browser';
      document.getElementById('unlockForm').listeners.submit[0]({ preventDefault() {} });
    """)
    drain_jobs(context)

    fallback = json.loads(context.eval(r"""
      JSON.stringify({
        actionMissing: !document.getElementById('timelineContainer')
          .querySelector('[data-timeline-action="copy"]'),
        focusedTimelineContainer: document.activeElement
          === document.getElementById('timelineContainer'),
      });
    """))
    assert fallback == {
        "actionMissing": True,
        "focusedTimelineContainer": True,
    }


def test_api_module_exports(client: TestClient) -> None:
    api_js = client.get("/js/api.js").text
    assert "export async function request" in api_js
    assert "export async function getSession" in api_js
    assert "export async function unlock" in api_js
    assert "export async function logout" in api_js
    assert "credentials: 'same-origin'" in api_js
    assert "session-expired" in api_js


def test_app_module_has_session_management(client: TestClient) -> None:
    app_js = client.get("/js/app.js").text
    assert "import" in app_js
    assert "session-expired" in app_js
    assert "showLockOverlay" in app_js or "sessionExpired" in app_js


def test_timeline_contract_covers_paging_links_dates_and_scroll() -> None:
    source = read_web("js/timeline.js")
    for token in ("loadInitial", "loadOlder", "limit=50", "appliedSequences", "newMessageButton",
                  "IntersectionObserver", "focusMessage", "navigator.clipboard.writeText"):
        assert token in source
    assert "http:" in source and "https:" in source
    assert "innerHTML = message.body" not in source


def test_timeline_module_is_served(client: TestClient) -> None:
    resp = client.get("/js/timeline.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_timeline_html_elements_exist(client: TestClient) -> None:
    html = client.get("/").text
    for element_id in ("timelineContainer", "newMessageButton", "timelinePanel"):
        assert f'id="{element_id}"' in html
    assert 'class="timeline-container"' in html
    assert 'class="btn btn-primary timeline-new-btn"' in html


def test_app_imports_timeline_module(client: TestClient) -> None:
    app_js = client.get("/js/app.js").text
    assert "createTimeline" in app_js
    assert "timeline.loadInitial" in app_js


def test_timeline_css_classes_present(client: TestClient) -> None:
    css = client.get("/styles.css").text
    for cls in ("timeline-message", "timeline-date-separator", "timeline-new-btn",
                "timeline-container", "timeline-copy-btn"):
        assert cls in css


def test_composer_contract_has_keyboard_paste_cancel_retry_and_preserved_text() -> None:
    source = read_web("js/composer.js")
    for token in ("event.key === 'Enter'", "event.shiftKey", "clipboardData.items", "kind === 'file'",
                  "AbortController", "cancelUpload", "retryUpload", "MAX_TEXT_LENGTH", "crypto.randomUUID"):
        assert token in source
    assert "textarea.value = ''" in source
    assert source.index("await sendText") < source.index("textarea.value = ''")
    assert '<progress class="progress" max="100"' in source
    assert 'value="${task.progress}"' in source
    assert 'aria-label="上传进度"' in source


def test_composer_module_is_served(client: TestClient) -> None:
    resp = client.get("/js/composer.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_composer_html_elements_exist(client: TestClient) -> None:
    html = client.get("/").text
    for element_id in ("composerForm", "composerTextarea", "composerFileInput",
                       "composerDropTarget", "composerQueue"):
        assert f'id="{element_id}"' in html
    assert 'class="panel transfer-panel composer-dock"' in html
    assert 'Enter 发送，Shift+Enter 换行' in html


def test_api_module_has_send_text_and_upload_file(client: TestClient) -> None:
    api_js = client.get("/js/api.js").text
    assert "export async function sendText" in api_js
    assert "export function uploadFile" in api_js
    assert "client_request_id" in api_js
    assert "AbortController" not in api_js or "signal" in api_js


def test_app_imports_composer_module(client: TestClient) -> None:
    app_js = client.get("/js/app.js").text
    assert "createComposer" in app_js
    assert "composerTextarea" in app_js or "composer" in app_js


def test_library_contract_has_filters_batches_storage_and_timeline_location() -> None:
    html, source = read_web("index.html"), read_web("js/library.js")
    for element_id in ("librarySearch", "fileTypeFilter", "deviceFilter", "dateFrom", "dateTo",
                       "batchDownload", "batchCopy", "batchDelete", "storageSummary"):
        assert f'id="{element_id}"' in html
    assert html.count('role="tab"') == 2
    assert 'id="gridViewBtn"' in html and 'aria-selected="true"' in html
    assert 'id="listViewBtn"' in html and 'aria-selected="false"' in html
    for token in ("/api/files?", "/api/files/batch-download", "/api/messages/batch-delete",
                  "navigator.clipboard.writeText", "timeline.focusMessage", "/api/storage"):
        assert token in source


def test_reconnect_and_responsive_contracts() -> None:
    api, config, css, html = read_web("js/api.js"), read_web("js/config.js"), read_web("styles.css"), read_web("index.html")
    assert "RECONNECT_DELAYS" in api and "from './config.js'" in api
    assert "RECONNECT_DELAYS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000]" in config
    assert "transfer-last-sequence" in api and "event.sequence" in api
    assert "reconnecting" in api and "after=" in api
    assert "@media (max-width: 360px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ":focus-visible" in css and "min-height: 44px" in css
    assert 'id="connectionStatus"' in html and 'aria-live="polite"' in html


def test_app_module_loads_without_unresolved_identifiers() -> None:
    context = create_js_context()
    for module_path in ("./api.js", "./timeline.js", "./composer.js", "./library.js"):
        load_js_module(context, module_path, read_web(f"js/{module_path[2:]}"))
    load_js_module(context, "./app.js", read_web("js/app.js"))
    drain_jobs(context)


def test_app_module_rejects_missing_request_export() -> None:
    context = create_js_context()
    api_source = read_web("js/api.js").replace(
        "export async function request", "async function request", 1
    )
    load_js_module(context, "./api.js", api_source)
    for module_path in ("./timeline.js", "./composer.js", "./library.js"):
        load_js_module(context, module_path, read_web(f"js/{module_path[2:]}"))

    with pytest.raises(quickjs.JSException, match="request.*not exported"):
        load_js_module(context, "./app.js", read_web("js/app.js"))


def test_quickjs_module_execution_rejects_unresolved_value_reference() -> None:
    context = create_js_context()
    with pytest.raises(quickjs.JSException, match="missingName.*not defined"):
        load_js_module(context, "./broken.js", "const value = missingName;")


def test_quickjs_module_execution_accepts_default_import() -> None:
    context = create_js_context()
    load_js_module(
        context,
        "./helper.js",
        "export default function helper() { return 42; }",
    )
    load_js_module(
        context,
        "./consumer.js",
        "import helper from './helper.js'; const value = helper();",
    )


def test_quickjs_module_execution_rejects_missing_module() -> None:
    context = create_js_context()
    with pytest.raises(quickjs.JSException, match="Module.*missing.js.*not loaded"):
        load_js_module(
            context,
            "./consumer.js",
            "import helper from './missing.js'; helper();",
        )


def test_quickjs_module_execution_rejects_missing_default_export() -> None:
    context = create_js_context()
    load_js_module(context, "./helper.js", "export function helper() {}")
    with pytest.raises(quickjs.JSException, match="default.*not exported"):
        load_js_module(
            context,
            "./consumer.js",
            "import helper from './helper.js'; helper();",
        )


def test_timeline_render_consumes_real_message_dtos(client: TestClient) -> None:
    text = client.post(
        "/api/messages",
        json={"body": "真实消息", "client_request_id": "frontend-text"},
    )
    upload = client.post(
        "/api/upload",
        data={"client_request_id": "frontend-file"},
        files={"file": ("contract.txt", b"dto", "text/plain")},
    )
    assert text.status_code == upload.status_code == 200

    response = client.get("/api/messages")
    assert response.status_code == 200
    items = response.json()["items"]
    context = create_js_context()
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    set_json(context, "messageDtos", items)
    result = context.eval(r"""
      const container = document.getElementById('timelineContainer');
      const timeline = __modules['./timeline.js'].createTimeline({
        container,
        newMessageButton: document.getElementById('newMessageButton'),
        api: () => Promise.resolve({ items: [] }),
        onRestore: null,
      });
      timeline.setLastSequence(41);
      messageDtos.forEach(message => timeline.upsert(message));
      const collectText = node => node.textContent + node.children.map(collectText).join('');
      JSON.stringify({
        sequence: timeline.getLastSequence(),
        messages: messageDtos.map(message => {
          const element = container.querySelector(`[data-message-id="${message.id}"]`);
          const link = element.querySelector('a');
          return {
            id: element.dataset.messageId,
            text: collectText(element),
            href: link ? link.href : null,
          };
        }),
      });
    """)
    rendered = json.loads(result)

    assert rendered["sequence"] == 41
    assert text.json()["id"] in [message["id"] for message in rendered["messages"]]
    assert any("真实消息" in message["text"] for message in rendered["messages"])
    assert upload.json()["file"]["download_url"] in [
        message["href"] for message in rendered["messages"]
    ]


def test_timeline_delete_confirmation_and_thirty_second_restore_execute_in_quickjs() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.confirmCount = 0;
      globalThis.confirm = () => { confirmCount += 1; return true; };
      globalThis.timerDelays = [];
      Date.now = () => Date.parse('2026-07-17T00:00:10+00:00');
      globalThis.setTimeout = window.setTimeout = (callback, delay) => {
        timerDelays.push(delay);
        return timerDelays.length;
      };
      globalThis.apiCalls = [];
    """)
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      const message = {
        id: 'timeline-delete-message',
        kind: 'text',
        body: 'delete and restore',
        created_at: '2026-07-17T00:00:00+00:00',
        deleted_at: null,
        file: null,
      };
      const container = document.getElementById('timelineDeleteContainer');
      globalThis.deleteTimeline = __modules['./timeline.js'].createTimeline({
        container,
        newMessageButton: null,
        api: (path, options = {}) => {
          apiCalls.push({ path, method: options.method || 'GET' });
          return Promise.resolve(path.endsWith('/restore')
            ? { ...message, deleted_at: null }
            : { ...message, deleted_at: '2026-07-17T00:00:00+00:00' });
        },
        onRestore: null,
      });
      deleteTimeline.upsert(message);
      container.querySelector('.timeline-delete-btn').listeners.click[0]();
    """)
    drain_jobs(context)
    context.eval(r"""
      document.getElementById('timelineDeleteContainer')
        .querySelector('.timeline-undo-btn').listeners.click[0]();
    """)
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      JSON.stringify({
        confirmCount,
        apiCalls,
        timerDelays,
        restored: Boolean(document.getElementById('timelineDeleteContainer').querySelector('[data-message-id="timeline-delete-message"]')),
        undoVisible: Boolean(document.getElementById('timelineDeleteContainer').querySelector('.timeline-undo-btn')),
      });
    """))

    assert result == {
        "confirmCount": 1,
        "apiCalls": [
            {"path": "/api/messages/timeline-delete-message", "method": "DELETE"},
            {
                "path": "/api/messages/timeline-delete-message/restore",
                "method": "POST",
            },
        ],
        "timerDelays": [20000],
        "restored": True,
        "undoVisible": False,
    }


def test_app_timeline_api_preserves_delete_and_restore_methods_in_quickjs() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.fetchCalls = [];
      globalThis.fetch = (path, options = {}) => {
        const method = options.method || 'GET';
        fetchCalls.push({ path, method });
        const sessionRequest = path === '/api/session' && method === 'GET';
        const deletedAt = method === 'DELETE' ? '2026-07-17T00:00:00+00:00' : null;
        return Promise.resolve({
          status: sessionRequest ? 401 : 200,
          ok: !sessionRequest,
          headers: { get: () => 'application/json' },
          json: () => Promise.resolve({
            id: 'app-timeline-message', kind: 'text', body: 'integration',
            created_at: '2026-07-17T00:00:00+00:00', deleted_at: deletedAt,
            file: null,
          }),
          text: () => Promise.resolve(''),
        });
      };
      Date.now = () => Date.parse('2026-07-17T00:00:10+00:00');
    """)
    for module_path in ("./api.js", "./timeline.js", "./composer.js", "./library.js"):
        load_js_module(context, module_path, read_web(f"js/{module_path[2:]}"))
    instrumented_app = read_web("js/app.js").replace(
        "const timeline = createTimeline({",
        "const timeline = globalThis.appTimeline = createTimeline({",
        1,
    )
    load_js_module(context, "./app.js", instrumented_app)
    drain_jobs(context)
    context.eval(r"""
      appTimeline.upsert({
        id: 'app-timeline-message', kind: 'text', body: 'integration',
        created_at: '2026-07-17T00:00:00+00:00', deleted_at: null, file: null,
      });
      document.getElementById('timelineContainer')
        .querySelector('.timeline-delete-btn').listeners.click[0]();
    """)
    drain_jobs(context)
    context.eval(r"""
      document.getElementById('timelineContainer')
        .querySelector('.timeline-undo-btn').listeners.click[0]();
    """)
    drain_jobs(context)

    mutation_calls = json.loads(context.eval(
        "JSON.stringify(fetchCalls.filter(call => call.method !== 'GET'))"
    ))
    assert mutation_calls == [
        {"path": "/api/messages/app-timeline-message", "method": "DELETE"},
        {
            "path": "/api/messages/app-timeline-message/restore",
            "method": "POST",
        },
    ]


def test_library_render_consumes_real_file_message_dto(client: TestClient) -> None:
    upload = client.post(
        "/api/upload",
        data={"client_request_id": "frontend-library"},
        files={"file": ("library.txt", b"dto", "text/plain")},
    )
    assert upload.status_code == 200

    response = client.get("/api/files")
    assert response.status_code == 200
    message = response.json()["items"][0]
    context = create_js_context()
    load_js_module(context, "./library.js", read_web("js/library.js"))
    set_json(context, "fileResponse", response.json())
    context.eval(r"""
      const root = document.getElementById('libraryView');
      document.getElementById('fileTypeFilter').value = 'all';
      globalThis.libraryController = __modules['./library.js'].createLibrary({
        root,
        api: path => Promise.resolve(path.startsWith('/api/files?')
          ? fileResponse
          : { file_count: 1, total_size: '3 B', largest_files: [] }),
        timeline: { focusMessage: () => true },
      });
      libraryController.load({});
    """)
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      JSON.stringify({
        html: document.getElementById('fileList').innerHTML,
        messages: libraryController.getFiles(),
        clickListeners: document.getElementById('fileList').listeners.click.length,
        changeListeners: document.getElementById('fileList').listeners.change.length,
      });
    """))

    assert result["messages"][0]["id"] == message["id"]
    assert message["file"]["name"] in result["html"]
    assert message["file"]["download_url"] in result["html"]
    assert 'data-file-action="copy"' in result["html"]
    assert 'data-file-action="delete"' in result["html"]
    assert 'data-file-action="locate"' in result["html"]
    assert result["clickListeners"] == result["changeListeners"] == 1


def test_timeline_pages_from_top_and_loads_until_real_dto(client: TestClient) -> None:
    created = []
    for index in range(51):
        response = client.post(
            "/api/messages",
            json={"body": f"page-{index}", "client_request_id": f"page-{index}"},
        )
        assert response.status_code == 200
        created.append(response.json())
    first_page = client.get("/api/messages", params={"limit": 50}).json()
    second_page = client.get(
        "/api/messages",
        params={"limit": 50, "before": first_page["next_before"]},
    ).json()
    oldest_id = created[0]["id"]

    context = create_js_context()
    set_json(context, "firstPage", first_page)
    set_json(context, "secondPage", second_page)
    context.eval(r"""
      globalThis.rafCallbacks = [];
      globalThis.requestAnimationFrame = callback => { rafCallbacks.push(callback); return rafCallbacks.length; };
      globalThis.observedSentinels = [];
      globalThis.IntersectionObserver = class IntersectionObserver {
        constructor(callback) { this.callback = callback; }
        observe(element) { observedSentinels.push(element); }
      };
    """)
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      const container = document.getElementById('timelineContainer');
      const originalAppend = container.append.bind(container);
      const originalInsertBefore = container.insertBefore.bind(container);
      container.append = (...nodes) => { originalAppend(...nodes); container.scrollHeight += nodes.length * 10; };
      container.insertBefore = (node, reference) => { originalInsertBefore(node, reference); container.scrollHeight += 10; };
      globalThis.timelinePaths = [];
      globalThis.timeline = __modules['./timeline.js'].createTimeline({
        container,
        newMessageButton: document.getElementById('newMessageButton'),
        api: path => {
          timelinePaths.push(path);
          return Promise.resolve(path.includes('before=') ? secondPage : firstPage);
        },
        onRestore: null,
      });
      globalThis.initialDone = false;
      timeline.loadInitial().then(() => { initialDone = true; });
    """)
    drain_jobs(context)
    context.eval("rafCallbacks.splice(0).forEach(callback => callback());")
    set_json(context, "oldestMessageId", oldest_id)
    context.eval(r"""
      const pagingContainer = document.getElementById('timelineContainer');
      pagingContainer.scrollTop = 75;
      globalThis.heightBeforeOlderPage = pagingContainer.scrollHeight;
      globalThis.loadUntilResult = null;
      globalThis.__scrollIntoViewCalls = 0;
      timeline.loadUntil(oldestMessageId, { focus: false }).then(result => { loadUntilResult = result; });
    """)
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      const resultContainer = document.getElementById('timelineContainer');
      JSON.stringify({
        paths: timelinePaths,
        sentinelIsFirst: resultContainer.children[0].classList.contains('timeline-sentinel'),
        sentinelObserved: observedSentinels.includes(resultContainer.children[0]),
        scrollTop: resultContainer.scrollTop,
        heightDelta: resultContainer.scrollHeight - heightBeforeOlderPage,
        loadUntilResult,
        loadScrollCalls: __scrollIntoViewCalls,
        focused: timeline.focusMessage(oldestMessageId),
        focusedScrollCalls: __scrollIntoViewCalls,
      });
    """))

    assert result["paths"][1] == f"/api/messages?limit=50&before={first_page['next_before']}"
    assert result["sentinelIsFirst"] is True
    assert result["sentinelObserved"] is True
    assert result["scrollTop"] == 75 + result["heightDelta"]
    assert result["loadUntilResult"] is True
    assert result["loadScrollCalls"] == 0
    assert result["focused"] is True
    assert result["focusedScrollCalls"] == 1


def test_library_filters_reload_and_pagination_is_reachable(client: TestClient) -> None:
    uploads = []
    for index in range(2):
        response = client.post(
            "/api/upload",
            data={"client_request_id": f"library-page-{index}"},
            files={"file": (f"page-{index}.txt", b"dto", "text/plain")},
        )
        assert response.status_code == 200
        uploads.append(response.json())

    context = create_js_context()
    set_json(context, "libraryFirstPage", {"items": [uploads[1]], "next_cursor": uploads[1]["id"]})
    set_json(context, "librarySecondPage", {"items": [uploads[0]], "next_cursor": None})
    load_js_module(context, "./library.js", read_web("js/library.js"))
    assert 'id="libraryLoadMore"' in client.get("/").text
    context.eval(r"""
      const root = document.getElementById('libraryView');
      document.getElementById('fileTypeFilter').value = 'all';
      globalThis.libraryPaths = [];
      globalThis.libraryController = __modules['./library.js'].createLibrary({
        root,
        api: path => {
          libraryPaths.push(path);
          if (path.startsWith('/api/files?')) {
            return Promise.resolve(path.includes('cursor=') ? librarySecondPage : libraryFirstPage);
          }
          return Promise.resolve({ file_count: 2, total_size: '6 B', largest_files: [] });
        },
        timeline: null,
      });
      libraryController.load({});
    """)
    drain_jobs(context)
    for script in (
        "document.getElementById('fileTypeFilter').value = 'document'; "
        "document.getElementById('fileTypeFilter').listeners.change[0]();",
        "document.getElementById('deviceFilter').value = 'browser-01'; "
        "document.getElementById('deviceFilter').listeners.input[0]();",
        "document.getElementById('dateFrom').value = '2026-07-01'; "
        "document.getElementById('dateFrom').listeners.change[0]();",
        "document.getElementById('dateTo').value = '2026-08-01'; "
        "document.getElementById('dateTo').listeners.change[0]();",
    ):
        context.eval(script)
        drain_jobs(context)
    before_click = json.loads(context.eval(r"""
      const loadMoreButton = document.getElementById('libraryLoadMore');
      JSON.stringify({ hidden: loadMoreButton.hidden, disabled: Boolean(loadMoreButton.disabled) });
    """))
    context.eval("document.getElementById('libraryLoadMore').listeners.click[0]();")
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      JSON.stringify({
        paths: libraryPaths,
        files: libraryController.getFiles(),
        hidden: document.getElementById('libraryLoadMore').hidden,
      });
    """))

    file_paths = [path for path in result["paths"] if path.startswith("/api/files?")]
    assert len(file_paths) == 6
    assert "type=document" in file_paths[-1]
    assert "device_id=browser-01" in file_paths[-1]
    assert "from=2026-07-01" in file_paths[-1]
    assert "to=2026-08-01" in file_paths[-1]
    assert f"cursor={uploads[1]['id']}" in file_paths[-1]
    assert [item["id"] for item in result["files"]] == [uploads[1]["id"], uploads[0]["id"]]
    assert before_click == {"hidden": False, "disabled": False}
    assert result["hidden"] is True


def test_library_batch_uses_message_ids_and_deleted_count(client: TestClient) -> None:
    uploads = []
    for index in range(2):
        response = client.post(
            "/api/upload",
            data={"client_request_id": f"batch-ui-{index}"},
            files={"file": (f"batch-{index}.txt", b"dto", "text/plain")},
        )
        assert response.status_code == 200
        uploads.append(response.json())

    context = create_js_context()
    set_json(context, "batchFiles", {"items": uploads, "next_cursor": None})
    load_js_module(context, "./library.js", read_web("js/library.js"))
    context.eval(r"""
      globalThis.apiBodies = [];
      globalThis.downloadedBlob = null;
      URL.createObjectURL = blob => { downloadedBlob = blob; return 'blob:test'; };
      const root = document.getElementById('libraryView');
      document.getElementById('fileTypeFilter').value = 'all';
      globalThis.libraryController = __modules['./library.js'].createLibrary({
        root,
        api: (path, options = {}) => {
          if (options.body) apiBodies.push(JSON.parse(options.body));
          if (path === '/api/files/batch-download') return Promise.resolve({ kind: 'zip-blob' });
          if (path === '/api/messages/batch-delete') return Promise.resolve({ deleted: 1 });
          return Promise.resolve(path.startsWith('/api/files?')
            ? batchFiles
            : { file_count: 2, total_size: '6 B', largest_files: [] });
        },
        timeline: null,
      });
      libraryController.load({});
    """)
    drain_jobs(context)
    context.eval(r"""
      document.getElementById('selectVisibleBtn').listeners.click[0]();
      document.getElementById('batchDownload').listeners.click[0]();
      document.getElementById('batchDelete').listeners.click[0]();
    """)
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      JSON.stringify({
        bodies: apiBodies,
        blobKind: downloadedBlob && downloadedBlob.kind,
        toast: document.getElementById('toast').textContent,
      });
    """))

    expected_body = {"message_ids": [item["id"] for item in uploads]}
    assert result["bodies"] == [expected_body, expected_body]
    assert result["blobKind"] == "zip-blob"
    assert result["toast"] == "已删除 1 个文件"


def test_library_batch_401s_dispatch_session_expired_through_api_request(
    client: TestClient,
) -> None:
    upload = client.post(
        "/api/upload",
        data={"client_request_id": "batch-401"},
        files={"file": ("batch-401.txt", b"dto", "text/plain")},
    )
    assert upload.status_code == 200

    context = create_js_context()
    set_json(context, "batch401Files", {"items": [upload.json()], "next_cursor": None})
    context.eval(r"""
      globalThis.expiredEvents = 0;
      window.addEventListener('session-expired', () => { expiredEvents += 1; });
      globalThis.fetch = path => {
        const unauthorized = path.includes('batch-');
        const payload = path.startsWith('/api/files?')
          ? batch401Files
          : { file_count: 1, total_size: '3 B', largest_files: [] };
        return Promise.resolve({
          status: unauthorized ? 401 : 200,
          ok: !unauthorized,
          headers: { get: () => 'application/json' },
          json: () => Promise.resolve(unauthorized ? { detail: 'Session required' } : payload),
          text: () => Promise.resolve(''),
        });
      };
    """)
    load_js_module(context, "./api.js", read_web("js/api.js"))
    load_js_module(context, "./library.js", read_web("js/library.js"))
    context.eval(r"""
      const root = document.getElementById('libraryView');
      document.getElementById('fileTypeFilter').value = 'all';
      globalThis.library401 = __modules['./library.js'].createLibrary({
        root,
        api: __modules['./api.js'].request,
        timeline: null,
      });
      library401.load({});
    """)
    drain_jobs(context)
    context.eval(r"""
      document.getElementById('selectVisibleBtn').listeners.click[0]();
      document.getElementById('batchDownload').listeners.click[0]();
      document.getElementById('batchDelete').listeners.click[0]();
    """)
    drain_jobs(context)

    assert context.eval("expiredEvents") == 2


def test_api_request_supports_blob_without_forwarding_response_type_to_fetch() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.fetchOptions = null;
      globalThis.fetch = (path, options) => {
        fetchOptions = options;
        return Promise.resolve({
          status: 200,
          ok: true,
          headers: { get: () => 'application/zip' },
          blob: () => Promise.resolve({ kind: 'zip-blob' }),
          text: () => Promise.resolve('wrong-response'),
        });
      };
      globalThis.blobKind = null;
    """)
    load_js_module(context, "./api.js", read_web("js/api.js"))
    context.eval(r"""
      __modules['./api.js'].request('/api/files/batch-download', {
        method: 'POST',
        responseType: 'blob',
      }).then(blob => { blobKind = blob.kind; });
    """)
    drain_jobs(context)

    result = json.loads(
        context.eval(
            "JSON.stringify({ blobKind, responseType: fetchOptions.responseType || null })"
        )
    )
    assert result == {"blobKind": "zip-blob", "responseType": None}


def test_app_lock_logout_close_once_and_unlock_reconnects() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.socketCount = 0;
      globalThis.socketCloseCount = 0;
      globalThis.WebSocket = class WebSocket {
        constructor(url) { this.url = url; this.closed = false; socketCount += 1; }
        close() {
          if (this.closed) return;
          this.closed = true;
          socketCloseCount += 1;
          if (this.onclose) this.onclose({ code: 1000 });
        }
      };
      globalThis.fetch = path => Promise.resolve({
        status: 200,
        ok: true,
        headers: { get: () => 'application/json' },
        json: () => Promise.resolve(
          path.startsWith('/api/files?') ? { items: [], next_cursor: null }
          : path === '/api/audit' ? { events: [] }
          : {}
        ),
        text: () => Promise.resolve(''),
      });
    """)
    for module_path in ("./api.js", "./timeline.js", "./composer.js", "./library.js"):
        load_js_module(context, module_path, read_web(f"js/{module_path[2:]}"))
    load_js_module(context, "./app.js", read_web("js/app.js"))
    drain_jobs(context)

    assert context.eval("socketCount") == 1
    context.eval(r"""
      window.dispatchEvent(new CustomEvent('session-expired'));
      window.dispatchEvent(new CustomEvent('session-expired'));
    """)
    assert context.eval("socketCloseCount") == 1

    context.eval(r"""
      document.getElementById('accessToken').value = 'secret-token';
      document.getElementById('deviceName').value = 'Browser';
      document.getElementById('unlockForm').listeners.submit[0]({ preventDefault() {} });
    """)
    drain_jobs(context)
    assert context.eval("socketCount") == 2

    context.eval("window.dispatchEvent(new CustomEvent('session-logout')); ")
    drain_jobs(context)
    assert context.eval("socketCloseCount") == 2


def test_upload_abort_rejects_abort_error_and_composer_continues_queue() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.FormData = class FormData { append() {} };
      globalThis.AbortController = class AbortController {
        constructor() {
          const listeners = [];
          this.signal = { addEventListener: (type, listener) => { if (type === 'abort') listeners.push(listener); } };
          this.abort = () => listeners.forEach(listener => listener());
        }
      };
      globalThis.xhrs = [];
      globalThis.XMLHttpRequest = class XMLHttpRequest {
        constructor() { this.upload = {}; xhrs.push(this); }
        open() {}
        send() {}
        abort() { if (this.onabort) this.onabort(); }
      };
    """)
    load_js_module(context, "./api.js", read_web("js/api.js"))
    load_js_module(context, "./composer.js", read_web("js/composer.js"))
    context.eval(r"""
      const directController = new AbortController();
      globalThis.abortName = null;
      __modules['./api.js'].uploadFile(
        { name: 'direct.txt', size: 1 }, 'direct', null, directController.signal
      ).catch(error => { abortName = error.name; });
      directController.abort();

      const fileInput = document.getElementById('composerFileInput');
      const queue = document.getElementById('composerQueue');
      __modules['./composer.js'].createComposer({
        form: document.getElementById('composerForm'),
        textarea: document.getElementById('composerTextarea'),
        fileInput,
        dropTarget: document.getElementById('composerDropTarget'),
        queue,
        api: null,
        timeline: null,
      });
      fileInput.listeners.change[0]({
        target: { files: [{ name: 'one.txt', size: 1 }, { name: 'two.txt', size: 1 }], value: '' },
      });
      queue.listeners.click[0]({
        target: { closest: () => ({ dataset: { action: 'cancel', taskId: '00000000-0000-4000-8000-000000000001' } }) },
      });
    """)
    drain_jobs(context)
    result = json.loads(context.eval("JSON.stringify({ abortName, xhrCount: xhrs.length })"))

    assert result == {"abortName": "AbortError", "xhrCount": 3}


def test_upload_with_preaborted_signal_rejects_without_abort_event() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.FormData = class FormData { append() {} };
      globalThis.preabortedXhr = null;
      globalThis.XMLHttpRequest = class XMLHttpRequest {
        constructor() { this.upload = {}; this.sent = false; preabortedXhr = this; }
        open() {}
        send() { this.sent = true; }
        abort() {}
      };
    """)
    load_js_module(context, "./api.js", read_web("js/api.js"))
    context.eval(r"""
      globalThis.preabortedName = null;
      __modules['./api.js'].uploadFile(
        { name: 'cancelled.txt', size: 1 },
        'preaborted',
        null,
        { aborted: true, addEventListener() {} },
      ).catch(error => { preabortedName = error.name; });
    """)
    drain_jobs(context)

    result = json.loads(context.eval(r"""
      JSON.stringify({ name: preabortedName, sent: preabortedXhr.sent });
    """))
    assert result == {"name": "AbortError", "sent": False}


def test_timeline_retries_event_after_first_dom_application_failure() -> None:
    context = create_js_context()
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      const retryContainer = document.getElementById('timelineRetryContainer');
      globalThis.retryTimeline = __modules['./timeline.js'].createTimeline({
        container: retryContainer,
        newMessageButton: null,
        api: () => Promise.resolve({ items: [], next_before: null }),
        onRestore: null,
      });
      globalThis.retryEvent = {
        sequence: 7,
        event_type: 'message.created',
        entity_id: 'retry-message',
        payload: { id: 'retry-message', body: 'retry body', created_at: '2026-07-17T00:00:00+00:00' },
      };
      const originalQuerySelector = retryContainer.querySelector.bind(retryContainer);
      retryContainer.querySelector = () => { throw new Error('first DOM failure'); };
      globalThis.firstFailed = false;
      try { retryTimeline.mergeEvent(retryEvent); } catch { firstFailed = true; }
      retryContainer.querySelector = originalQuerySelector;
      globalThis.retryApplied = retryTimeline.mergeEvent(retryEvent);
    """)
    result = json.loads(context.eval(r"""
      JSON.stringify({
        firstFailed,
        retryApplied,
        sequence: retryTimeline.getLastSequence(),
        rendered: Boolean(document.getElementById('timelineRetryContainer').querySelector('[data-message-id="retry-message"]')),
      });
    """))

    assert result == {
        "firstFailed": True,
        "retryApplied": True,
        "sequence": 7,
        "rendered": True,
    }


def test_connect_events_persists_sequence_only_after_successful_application() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.webSockets = [];
      globalThis.WebSocket = class WebSocket {
        constructor(url) { this.url = url; webSockets.push(this); }
        close() {}
      };
    """)
    load_js_module(context, "./api.js", read_web("js/api.js"))
    context.eval(r"""
      globalThis.applyAttempts = 0;
      __modules['./api.js'].connectEvents({
        after: () => 0,
        onEvent: () => (++applyAttempts > 1),
        onStatus: () => {},
      });
      const message = { data: JSON.stringify({ sequence: 9, event_type: 'message.created' }) };
      webSockets[0].onmessage(message);
      globalThis.sequenceAfterFailure = localStorage.getItem('transfer-last-sequence');
      webSockets[0].onmessage(message);
      globalThis.sequenceAfterSuccess = localStorage.getItem('transfer-last-sequence');
    """)
    result = json.loads(context.eval(r"""
      JSON.stringify({ sequenceAfterFailure, sequenceAfterSuccess, applyAttempts });
    """))

    assert result == {
        "sequenceAfterFailure": None,
        "sequenceAfterSuccess": "9",
        "applyAttempts": 2,
    }


def test_connect_events_cancels_stale_timer_and_preserves_reconnect() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.webSockets = [];
      globalThis.timerEntries = [];
      globalThis.activeTimerIds = [];
      globalThis.clearedTimerIds = [];
      window.setTimeout = (callback, delay) => {
        const id = timerEntries.length + 1;
        timerEntries.push({ id, callback, delay });
        activeTimerIds.push(id);
        return id;
      };
      window.clearTimeout = id => {
        clearedTimerIds.push(id);
        activeTimerIds = activeTimerIds.filter(activeId => activeId !== id);
      };
      globalThis.runTimer = id => {
        activeTimerIds = activeTimerIds.filter(activeId => activeId !== id);
        timerEntries.find(entry => entry.id === id).callback();
      };
      globalThis.WebSocket = class WebSocket {
        constructor(url) { this.url = url; webSockets.push(this); }
        close() {}
      };
    """)
    load_js_module(context, "./api.js", read_web("js/api.js"))
    context.eval(r"""
      globalThis.stoppedStatuses = [];
      const stoppedConnection = __modules['./api.js'].connectEvents({
        after: () => 0,
        onEvent: () => true,
        onStatus: status => stoppedStatuses.push(status),
      });
      webSockets[0].onclose({ code: 1006 });
      globalThis.staleTimerId = activeTimerIds[0];
      stoppedConnection.close();
      runTimer(staleTimerId);
      globalThis.webSocketCountAfterStaleTimer = webSockets.length;
      globalThis.statusesAfterStaleTimer = stoppedStatuses.slice();

      globalThis.reconnectStatuses = [];
      globalThis.normalSocketIndex = webSockets.length;
      __modules['./api.js'].connectEvents({
        after: () => 0,
        onEvent: () => true,
        onStatus: status => reconnectStatuses.push(status),
      });
      webSockets[normalSocketIndex].onclose({ code: 1006 });
      webSockets[normalSocketIndex].onclose({ code: 1006 });
      globalThis.activeTimersAfterReschedule = activeTimerIds.slice();
      globalThis.statusesBeforeCanceledTimer = reconnectStatuses.slice();
      runTimer(2);
      globalThis.webSocketCountAfterCanceledTimer = webSockets.length;
      globalThis.statusesAfterCanceledTimer = reconnectStatuses.slice();
      globalThis.activeTimersAfterCanceledTimer = activeTimerIds.slice();
      runTimer(activeTimerIds[0]);
      webSockets[webSockets.length - 1].onopen();
      globalThis.finalReconnectWebSocketCount = webSockets.length;

      globalThis.unauthorizedStatuses = [];
      const unauthorizedSocketIndex = webSockets.length;
      __modules['./api.js'].connectEvents({
        after: () => 0,
        onEvent: () => true,
        onStatus: status => unauthorizedStatuses.push(status),
      });
      webSockets[unauthorizedSocketIndex].onclose({ code: 4401 });
    """)
    result = json.loads(
        context.eval(
            """
            JSON.stringify({
              stoppedStatuses,
              staleTimerId,
              clearedTimerIds,
              webSocketCountAfterStaleTimer,
              statusesAfterStaleTimer,
              activeTimersAfterReschedule,
              statusesBeforeCanceledTimer,
              webSocketCountAfterCanceledTimer,
              statusesAfterCanceledTimer,
              activeTimersAfterCanceledTimer,
              reconnectStatuses,
              finalReconnectWebSocketCount,
              unauthorizedStatuses,
            });
            """
        )
    )

    assert result == {
        "stoppedStatuses": ["connecting", "reconnecting"],
        "staleTimerId": 1,
        "clearedTimerIds": [1, 2],
        "webSocketCountAfterStaleTimer": 1,
        "statusesAfterStaleTimer": ["connecting", "reconnecting"],
        "activeTimersAfterReschedule": [3],
        "statusesBeforeCanceledTimer": [
            "connecting",
            "reconnecting",
            "reconnecting",
        ],
        "webSocketCountAfterCanceledTimer": 2,
        "statusesAfterCanceledTimer": [
            "connecting",
            "reconnecting",
            "reconnecting",
        ],
        "activeTimersAfterCanceledTimer": [3],
        "reconnectStatuses": [
            "connecting",
            "reconnecting",
            "reconnecting",
            "reconnecting",
            "connected",
        ],
        "finalReconnectWebSocketCount": 3,
        "unauthorizedStatuses": ["connecting", "closed"],
    }


def test_timeline_concurrent_loads_share_promise_and_load_until_continues(
    client: TestClient,
) -> None:
    created = []
    for index in range(51):
        response = client.post(
            "/api/messages",
            json={"body": f"race-{index}", "client_request_id": f"race-{index}"},
        )
        assert response.status_code == 200
        created.append(response.json())
    first_page = client.get("/api/messages", params={"limit": 50}).json()
    second_page = client.get(
        "/api/messages",
        params={"limit": 50, "before": first_page["next_before"]},
    ).json()

    context = create_js_context()
    set_json(context, "raceFirstPage", first_page)
    set_json(context, "raceSecondPage", second_page)
    set_json(context, "raceTargetId", created[0]["id"])
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      globalThis.pageResolvers = [];
      globalThis.racePaths = [];
      const raceTimeline = __modules['./timeline.js'].createTimeline({
        container: document.getElementById('timelineRaceContainer'),
        newMessageButton: null,
        api: path => {
          racePaths.push(path);
          return new Promise(resolve => pageResolvers.push(resolve));
        },
        onRestore: null,
      });
      globalThis.raceTimeline = raceTimeline;
      const firstLoad = raceTimeline.loadOlder();
      const concurrentLoad = raceTimeline.loadOlder();
      globalThis.samePromise = firstLoad === concurrentLoad;
      globalThis.locatedAfterRace = null;
      raceTimeline.loadUntil(raceTargetId).then(result => { locatedAfterRace = result; });
      pageResolvers.shift()(raceFirstPage);
    """)
    drain_jobs(context)
    assert context.eval("racePaths.length") == 2
    context.eval("pageResolvers.shift()(raceSecondPage);")
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      JSON.stringify({ samePromise, locatedAfterRace, paths: racePaths });
    """))

    assert result["samePromise"] is True
    assert result["locatedAfterRace"] is True
    assert result["paths"] == [
        "/api/messages?limit=50",
        f"/api/messages?limit=50&before={first_page['next_before']}",
    ]
