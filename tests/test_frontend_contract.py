from __future__ import annotations

import json
from html.parser import HTMLParser
import itertools
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
        upload_chunk_size_bytes=1024,
        event_retention_limit=100,
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
  replaceChildren(...nodes) {
    this.children.forEach(child => { child.parentNode = null; });
    this.children = [];
    this._textContent = '';
    this._innerHTML = '';
    this.append(...nodes);
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
  scrollIntoView(options) {
    globalThis.__scrollIntoViewCalls = (globalThis.__scrollIntoViewCalls || 0) + 1;
    (globalThis.__scrollIntoViewOptions ||= []).push(options || null);
    if (typeof globalThis.__scrollIntoViewEffect === 'function') {
      globalThis.__scrollIntoViewEffect(this);
    }
  }
  getBoundingClientRect() {
    const key = this.dataset.messageId || this.dataset.uploadId || this.id;
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
globalThis.localStorage = { values: {}, getItem(key) { return this.values[key] || null; }, setItem(key, value) { this.values[key] = String(value); }, removeItem(key) { delete this.values[key]; } };
globalThis.sessionStorage = { values: {}, getItem(key) { return this.values[key] || null; }, setItem(key, value) { this.values[key] = String(value); }, removeItem(key) { delete this.values[key]; } };
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
        for dependency in ("./upload-coordinator.js", "./upload-persistence.js"):
            dependency_type = context.eval(
                f'typeof globalThis.__modules[{json.dumps(dependency)}]'
            )
            if "undefined" in str(dependency_type):
                context.eval(transform_module(read_web(f"js/{dependency[2:]}"), dependency))
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


def test_manage_page_groups_connection_storage_appearance_and_session() -> None:
    html = read_web("index.html")
    transfer = html[html.index('id="transferPage"'):html.index('id="filesPage"')]
    manage = html[html.index('id="managePage"'):html.index('class="mobile-nav"')]
    for token in (
        'id="connectionPanel"',
        'id="operationsPanel"',
        'id="appearancePanel"',
        'id="sessionPanel"',
        'id="logoutButton"',
    ):
        assert token in manage
    for token in ('id="healthConnection"', 'id="metricCount"', 'id="metricSize"'):
        assert token in manage
        assert token not in transfer
    assert manage.count('class="manage-panel-body') == 4


def test_mobile_fixed_elements_share_offset_and_430_rule_is_single() -> None:
    css = read_web("styles.css")
    root = css[css.index(":root {"):css.index(".dark {")]
    assert "--mobile-nav-base-height: 66px" in root
    assert (
        "--mobile-nav-height: calc(var(--mobile-nav-base-height) + "
        "env(safe-area-inset-bottom))"
    ) in root
    assert "--mobile-fixed-offset: calc(var(--mobile-nav-height) + 12px)" in root
    fixed_offset = re.search(r"--mobile-fixed-offset:\s*([^;]+)", root)
    assert fixed_offset and "safe-area-inset-bottom" not in fixed_offset.group(1)
    assert css.count("@media (max-width: 430px)") == 1
    assert "bottom: var(--mobile-fixed-offset)" in css


def test_mobile_batch_toolbar_effective_bottom_respects_cascade_order() -> None:
    css = read_web("styles.css")

    def effective_bottom(stylesheet: str) -> str:
        declarations: list[tuple[tuple[int, int, int], int, str]] = []
        for rule_match in re.finditer(r"([^{}]+)\{([^{}]*)\}", stylesheet):
            selectors, body = rule_match.groups()
            bottom_match = re.search(r"(?:^|;)\s*bottom\s*:\s*([^;]+)", body)
            if not bottom_match:
                continue
            for raw_selector in selectors.split(","):
                selector = re.sub(r"/\*.*?\*/", "", raw_selector, flags=re.S).strip()
                if selector != ".batch-toolbar":
                    continue
                specificity = (
                    selector.count("#"),
                    selector.count(".") + selector.count("["),
                    0,
                )
                declarations.append((specificity, rule_match.start(), bottom_match.group(1).strip()))
        assert declarations, "missing .batch-toolbar bottom declaration"
        return max(declarations, key=lambda item: (item[0], item[1]))[2]

    def assert_safe_mobile_bottom(stylesheet: str) -> None:
        assert effective_bottom(stylesheet) == "var(--mobile-fixed-offset)"

    assert_safe_mobile_bottom(css)

    late_rule = css.rindex(".batch-toolbar {")
    broken_css = css[:late_rule] + css[late_rule:].replace(
        "position: fixed;",
        "position: fixed;\n            bottom: 22px;",
        1,
    )
    with pytest.raises(AssertionError):
        assert_safe_mobile_bottom(broken_css)


def test_manage_mobile_grid_active_nav_and_reduced_motion_contract() -> None:
    css = read_web("styles.css")
    mobile = css[css.index("@media (max-width: 720px)"):css.index("@media (max-width: 430px)")]
    assert ".manage-grid" in css
    assert ".manage-grid" in mobile
    assert "grid-template-columns: 1fr" in mobile
    assert ".mobile-nav button.active" in mobile
    assert "@media (prefers-reduced-motion: reduce)" in css


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


def test_mobile_transfer_reserves_space_for_fixed_composer_dock() -> None:
    css = read_web("styles.css")
    compact_start = css.index("@media (max-width: 430px)")
    compact = css[compact_start:]

    def rule(source: str, selector: str) -> str:
        match = re.search(rf"{re.escape(selector)}\s*\{{([^{{}}]*)\}}", source)
        assert match, f"missing CSS rule for {selector}"
        return match.group(1)

    def declaration(block: str, name: str) -> str:
        match = re.search(rf"(?:^|;)\s*{re.escape(name)}\s*:\s*([^;]+)", block)
        assert match, f"missing CSS declaration for {name}"
        return match.group(1).strip()

    mobile = css[css.index("@media (max-width: 720px)"):compact_start]
    root_vars = rule(mobile, ":root")
    workspace_rule = rule(mobile, ".transfer-workspace")
    composer_rule = rule(mobile, ".composer-dock")
    nav_rule = rule(mobile, ".mobile-nav")

    assert declaration(root_vars, "--mobile-composer-reserve")
    assert "var(--mobile-timeline-min-height)" in declaration(
        root_vars, "--mobile-workspace-min-height"
    )
    assert "var(--mobile-composer-reserve)" in declaration(workspace_rule, "padding-bottom")
    assert "var(--mobile-workspace-min-height)" in declaration(workspace_rule, "min-height")
    assert declaration(composer_rule, "position") == "fixed"
    assert declaration(composer_rule, "bottom") == "var(--mobile-fixed-offset)"
    assert declaration(composer_rule, "left") == "14px"
    assert declaration(composer_rule, "right") == "14px"
    assert declaration(nav_rule, "height") == "var(--mobile-nav-height)"
    assert "env(safe-area-inset-bottom)" in declaration(nav_rule, "padding")
    assert "--mobile-composer-reserve" in rule(compact, ":root")


def test_mobile_toast_offset_clears_fixed_composer() -> None:
    css = read_web("styles.css")
    mobile = css[
        css.index("@media (max-width: 720px)"):css.index("@media (max-width: 430px)")
    ]
    root_match = re.search(r":root\s*\{([^{}]*)\}", mobile)
    toast_match = re.search(r"\.toast\s*\{([^{}]*)\}", mobile)
    assert root_match and toast_match
    assert (
        "--mobile-toast-offset: calc(var(--mobile-fixed-offset) + "
        "var(--mobile-composer-reserve) + 12px)"
    ) in root_match.group(1)
    assert "bottom: var(--mobile-toast-offset)" in toast_match.group(1)


def test_transfer_route_dead_styles_are_removed() -> None:
    css = read_web("styles.css")
    for selector in (
        ".transfer-route", ".route-node", "route-pulse", ".hero",
        ".quick-stats", ".stat-card", ".dashboard-grid", ".rail",
        ".library-filter-toggle",
    ):
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


def test_library_locate_loads_before_app_navigation_without_message_hash() -> None:
    context = create_js_context()
    load_js_module(context, "./library.js", read_web("js/library.js"))
    context.eval(r"""
      location.hash = '#files';
      globalThis.locateSequence = [];
      const timeline = {
        ensureMessageLoaded(messageId) {
          locateSequence.push(`load:${messageId}`);
          return Promise.resolve(true);
        },
        focusMessage(messageId) { locateSequence.push(`early-focus:${messageId}`); return true; },
      };
      const controller = __modules['./library.js'].createLibrary({
        root: document.getElementById('libraryView'),
        api: () => Promise.resolve({}),
        timeline,
        onLocate(messageId) {
          locateSequence.push(`route:${messageId}`);
          locateSequence.push(`focus:${messageId}`);
        },
      });
      globalThis.locatePromise = controller.openMessage('message-7');
    """)
    drain_jobs(context)

    result = json.loads(context.eval(r"""
      JSON.stringify({ sequence: locateSequence, hash: location.hash });
    """))
    assert result == {
        "sequence": ["load:message-7", "route:message-7", "focus:message-7"],
        "hash": "#files",
    }


def test_app_locate_navigates_before_focusing_timeline_message() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.locateSequence = [];
      __modules['./api.js'] = {
        request: () => Promise.resolve({}), unlock: () => Promise.resolve({}),
        logout: () => Promise.resolve({}), getSession: () => Promise.resolve({}),
        ApiError: class ApiError extends Error {},
        connectEvents: () => ({ close() {} }), getLastSequence: () => 0,
      };
      __modules['./timeline.js'] = {
        createTimeline: () => ({
          loadInitial() {}, mergeEvent() {}, remove() {}, upsert() {},
          focusMessage(messageId) { locateSequence.push(`focus:${messageId}`); return true; },
        }),
      };
      __modules['./composer.js'] = { createComposer: () => ({}) };
      __modules['./library.js'] = {
        createLibrary: options => {
          globalThis.locateFromLibrary = options.onLocate;
          return { clearSelection() {} };
        },
      };
      __modules['./navigation.js'] = {
        createNavigation: () => ({
          navigate(route, options) {
            locateSequence.push(`route:${route}:${options && options.focus === false}`);
            return Promise.resolve();
          },
          start() {},
        }),
      };
    """)
    load_js_module(context, "./app.js", read_web("js/app.js"))
    context.eval("globalThis.locatePromise = locateFromLibrary('message-8');")
    drain_jobs(context)

    assert json.loads(context.eval("JSON.stringify(locateSequence)")) == [
        "route:transfer:true",
        "focus:message-8",
    ]


def test_library_destroy_clears_toast_timer_and_stale_callback_is_inert() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.timerCallbacks = Object.create(null);
      globalThis.activeTimers = new Set();
      globalThis.nextTimerId = 0;
      window.setTimeout = callback => {
        const id = ++nextTimerId;
        timerCallbacks[id] = callback;
        activeTimers.add(id);
        return id;
      };
      window.clearTimeout = id => { activeTimers.delete(id); };
    """)
    load_js_module(context, "./library.js", read_web("js/library.js"))

    result = json.loads(context.eval(r"""
      const root = document.getElementById('libraryView');
      const batchDownload = document.getElementById('batchDownload');
      const toast = document.getElementById('toast');
      const api = () => Promise.resolve({});
      const first = __modules['./library.js'].createLibrary({ root, api, timeline: null });
      batchDownload.listeners.click[0]();
      const firstTimer = nextTimerId;
      first.destroy();
      const firstTimerCleared = !activeTimers.has(firstTimer);

      const second = __modules['./library.js'].createLibrary({ root, api, timeline: null });
      batchDownload.listeners.click[0]();
      const secondTimer = nextTimerId;
      timerCallbacks[firstTimer]();
      const staleTimerKeptNewToast = toast.classList.contains('show');
      timerCallbacks[secondTimer]();
      const currentTimerHidToast = !toast.classList.contains('show');
      second.destroy();
      JSON.stringify({ firstTimerCleared, staleTimerKeptNewToast, currentTimerHidToast });
    """))

    assert result == {
        "firstTimerCleared": True,
        "staleTimerKeptNewToast": True,
        "currentTimerHidToast": True,
    }


def test_library_destroy_prevents_inflight_load_from_mutating_dom() -> None:
    context = create_js_context()
    load_js_module(context, "./library.js", read_web("js/library.js"))
    context.eval(r"""
      globalThis.resolveFiles = null;
      const filesGate = new Promise(resolve => { resolveFiles = resolve; });
      const fileList = document.getElementById('fileList');
      fileList.innerHTML = 'sentinel';
      const controller = __modules['./library.js'].createLibrary({
        root: document.getElementById('libraryView'),
        api: path => path.startsWith('/api/files?') ? filesGate : Promise.resolve({}),
        timeline: null,
      });
      controller.load({});
      controller.destroy();
      resolveFiles({ items: [], next_cursor: null });
    """)
    drain_jobs(context)

    assert context.eval("document.getElementById('fileList').innerHTML") == "sentinel"


def test_library_one_time_preview_and_undo_listeners_release_immediately() -> None:
    context = create_js_context()
    load_js_module(context, "./library.js", read_web("js/library.js"))
    context.eval(r"""
      const message = {
        id: 'message-1',
        created_at: '2026-07-18T00:00:00Z',
        file: {
          name: 'preview.png', size: '1 B', created_at: '2026-07-18T00:00:00Z',
          extension: '.png', media_kind: 'image', is_previewable: true,
          download_url: '/api/files/file-1/download', sha256: 'abc',
        },
      };
      const api = (path, options = {}) => Promise.resolve(
        path.startsWith('/api/files?') ? { items: [message], next_cursor: null }
          : options.method === 'DELETE' ? { deleted_at: '2026-07-18T00:00:00Z' }
          : { file_count: 1, total_size: '1 B', largest_files: [] }
      );
      globalThis.keydownRemovals = 0;
      const originalDocumentRemove = document.removeEventListener.bind(document);
      document.removeEventListener = (type, listener) => {
        if (type === 'keydown') keydownRemovals += 1;
        originalDocumentRemove(type, listener);
      };
      globalThis.controller = __modules['./library.js'].createLibrary({
        root: document.getElementById('libraryView'), api, timeline: null,
      });
      controller.load({});
    """)
    drain_jobs(context)

    result = json.loads(context.eval(r"""
      const fileList = document.getElementById('fileList');
      const previewAction = {
        dataset: { fileAction: 'preview', messageId: 'message-1' },
        focus() {},
      };
      fileList.listeners.click[0]({ target: { closest: () => previewAction } });
      document.getElementById('closePreviewBtn').listeners.click[0]();
      const removalsAfterClose = keydownRemovals;
      controller.destroy();
      const removalsAfterDestroy = keydownRemovals;
      JSON.stringify({ removalsAfterClose, removalsAfterDestroy });
    """))
    assert result == {"removalsAfterClose": 1, "removalsAfterDestroy": 2}

    context = create_js_context()
    load_js_module(context, "./library.js", read_web("js/library.js"))
    context.eval(r"""
      const message = {
        id: 'message-1', created_at: '2026-07-18T00:00:00Z',
        file: {
          name: 'file.txt', size: '1 B', created_at: '2026-07-18T00:00:00Z',
          extension: '.txt', media_kind: 'document', is_previewable: false,
          download_url: '/api/files/file-1/download', sha256: 'abc',
        },
      };
      const api = (path, options = {}) => Promise.resolve(
        path.startsWith('/api/files?') ? { items: [message], next_cursor: null }
          : options.method === 'DELETE' ? { deleted_at: '2026-07-18T00:00:00Z' }
          : { file_count: 1, total_size: '1 B', largest_files: [] }
      );
      globalThis.controller = __modules['./library.js'].createLibrary({
        root: document.getElementById('libraryView'), api, timeline: null,
      });
      controller.load({});
    """)
    drain_jobs(context)
    context.eval(r"""
      const deleteAction = { dataset: { fileAction: 'delete', messageId: 'message-1' } };
      document.getElementById('fileList').listeners.click[0]({
        target: { closest: () => deleteAction },
      });
    """)
    drain_jobs(context)

    result = json.loads(context.eval(r"""
      const undoButton = document.getElementById('toast').children[0];
      globalThis.undoRemovals = 0;
      const originalUndoRemove = undoButton.removeEventListener.bind(undoButton);
      undoButton.removeEventListener = (type, listener) => {
        undoRemovals += 1;
        originalUndoRemove(type, listener);
      };
      document.getElementById('batchDownload').listeners.click[0]();
      const removalsAfterReplace = undoRemovals;
      controller.destroy();
      JSON.stringify({ removalsAfterReplace, removalsAfterDestroy: undoRemovals });
    """))
    assert result == {"removalsAfterReplace": 1, "removalsAfterDestroy": 1}


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


def test_navigation_focuses_user_navigation_and_preserves_focus_on_history() -> None:
    context = create_js_context()
    load_js_module(context, "./navigation.js", read_web("js/navigation.js"))
    result = json.loads(context.eval(r"""
      const focusCalls = [];
      const windowListeners = {};
      const stableControl = { id: 'stable-control' };
      const headings = Object.fromEntries(['transfer', 'files'].map(route => [route, {
        id: `${route}-heading`,
        focus(options) {
          focusCalls.push({ route, options: options || null });
          documentObject.activeElement = this;
        },
      }]));
      const title = { textContent: '' };
      const windowObject = {
        scrollY: 0,
        location: { hash: '#transfer' },
        history: { replaceState(_state, _title, hash) { windowObject.location.hash = hash; } },
        addEventListener(type, listener) { windowListeners[type] = listener; },
        removeEventListener() {},
        scrollTo() {},
      };
      const documentObject = {
        activeElement: stableControl,
        title: '',
        querySelectorAll() { return []; },
        querySelector(selector) {
          if (selector === '[data-route-title]') return title;
          const match = selector.match(/^\[data-route-heading="(.+)"\]$/);
          return match ? headings[match[1]] : null;
        },
      };
      const navigation = __modules['./navigation.js'].createNavigation({ windowObject, documentObject });
      navigation.start();
      navigation.navigate('files');
      windowListeners.hashchange();
      const clickFocus = documentObject.activeElement.id;

      documentObject.activeElement = stableControl;
      windowObject.location.hash = '#transfer';
      windowListeners.hashchange();
      JSON.stringify({
        clickFocus,
        historyFocus: documentObject.activeElement.id,
        focusCalls,
        title: documentObject.title,
        breadcrumb: title.textContent,
      });
    """))
    assert result == {
        "clickFocus": "files-heading",
        "historyFocus": "stable-control",
        "focusCalls": [
            {"route": "files", "options": {"preventScroll": True}},
        ],
        "title": "传输工作台 · MonkeyCode",
        "breadcrumb": "传输工作台",
    }


def test_navigation_owns_scroll_restoration_and_restores_route_positions() -> None:
    context = create_js_context()
    load_js_module(context, "./navigation.js", read_web("js/navigation.js"))
    result = json.loads(context.eval(r"""
      const windowListeners = {};
      const stableControl = { id: 'stable-control' };
      const windowObject = {
        scrollY: 0,
        location: { hash: '#transfer' },
        history: {
          scrollRestoration: 'auto',
          replaceState(_state, _title, hash) { windowObject.location.hash = hash; },
        },
        addEventListener(type, listener) { windowListeners[type] = listener; },
        removeEventListener() {},
        scrollTo(_x, y) { windowObject.scrollY = y; },
      };
      const documentObject = {
        activeElement: stableControl,
        title: '',
        querySelectorAll() { return []; },
        querySelector() { return null; },
      };
      const navigation = __modules['./navigation.js'].createNavigation({ windowObject, documentObject });
      navigation.start();
      const during = windowObject.history.scrollRestoration;

      windowObject.scrollY = 125;
      navigation.navigate('files', { focus: false });
      windowListeners.hashchange();
      windowObject.scrollY = 375;
      navigation.navigate('manage', { focus: false });
      windowListeners.hashchange();
      windowObject.scrollY = 625;

      windowObject.location.hash = '#files';
      windowListeners.hashchange();
      const filesScroll = windowObject.scrollY;
      const historyFocus = documentObject.activeElement.id;
      windowObject.location.hash = '#manage';
      windowListeners.hashchange();
      const manageScroll = windowObject.scrollY;
      navigation.destroy();

      JSON.stringify({
        during,
        after: windowObject.history.scrollRestoration,
        filesScroll,
        manageScroll,
        historyFocus,
      });
    """))
    assert result == {
        "during": "manual",
        "after": "auto",
        "filesScroll": 375,
        "manageScroll": 625,
        "historyFocus": "stable-control",
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
          dispatchHashChange: () => windowListeners.dispatch('hashchange'),
          buttonListenerCount: () => buttonListeners.count('click'),
          windowListenerCount: () => windowListeners.count('hashchange'),
          routeChangeCount: () => routeChangeCount,
        };
      };
    """)
    return context


def test_navigation_runtime_unknown_hash_is_replaced_with_supported_route() -> None:
    context = create_navigation_lifecycle_context()
    result = json.loads(context.eval(r"""
      const harness = createNavigationHarness();
      harness.navigation.start();
      harness.windowObject.location.hash = '#message-unsupported';
      harness.dispatchHashChange();
      JSON.stringify({
        hash: harness.windowObject.location.hash,
        route: harness.navigation.getCurrentRoute(),
      });
    """))

    assert result == {"hash": "#transfer", "route": "transfer"}


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


def test_composer_contract_has_keyboard_paste_and_preserved_text() -> None:
    source = read_web("js/composer.js")
    for token in ("event.key === 'Enter'", "event.shiftKey", "clipboardData.items", "kind === 'file'",
                  "MAX_TEXT_LENGTH", "crypto.randomUUID"):
        assert token in source
    assert "textarea.value = ''" in source
    assert source.index("await sendText") < source.index("textarea.value = ''")


def test_composer_delegates_select_drop_and_paste_to_one_coordinator() -> None:
    source = read_web("js/composer.js")
    assert "uploadCoordinator.enqueueFiles" in source
    assert "uploadFile(" not in source
    assert "uploadTasks" not in source
    assert "renderQueue" not in source


def test_timeline_upload_projection_replaces_card_without_duplicate() -> None:
    context = create_js_context()
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      globalThis.container = document.createElement('div');
      globalThis.timeline = __modules['./timeline.js'].createTimeline({
        container, newMessageButton: null, api: () => Promise.resolve({ items: [] }),
        onRestore: () => {}, onUploadAction: () => {},
      });
      timeline.upsertUpload({ uploadId: 'upload-1', clientRequestId: 'request-1', name: 'a.txt', sizeBytes: 4, status: 'uploading', confirmedBytes: 2, progressPercent: 50, isSourceDevice: true });
      timeline.upsert({ id: 'message-1', upload_id: 'upload-1', client_request_id: 'request-1', created_at: '2026-07-19T00:00:00Z', file: { id: 'upload-1', name: 'a.txt', size: '4 B', download_url: '/download/upload-1' } });
    """)
    assert context.eval("container.querySelectorAll('[data-upload-id=\"upload-1\"]').length") == 0
    assert context.eval("container.querySelectorAll('[data-message-id=\"message-1\"]').length") == 1
    assert context.eval("container.querySelectorAll('.timeline-message').length") == 1
    assert context.eval("container.querySelectorAll('.timeline-date-separator').length") == 1


@pytest.mark.parametrize(
    "order",
    list(itertools.permutations(("rest", "upload_completed", "message_created"))),
)
def test_timeline_completion_routes_converge_to_one_permanent_dom(order: tuple[str, ...]) -> None:
    context = create_js_context()
    context.set("completionOrder", json.dumps(order))
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      globalThis.container = document.createElement('div');
      globalThis.timeline = __modules['./timeline.js'].createTimeline({
        container, newMessageButton: null, api: () => Promise.resolve({ items: [] }),
        onRestore: () => {}, onUploadAction: () => {},
      });
      globalThis.upload = {
        uploadId: 'upload-order', clientRequestId: 'request-order', name: 'ordered.txt',
        sizeBytes: 4, status: 'uploading', confirmedBytes: 2, progressPercent: 50,
        isSourceDevice: true,
      };
      globalThis.message = {
        id: 'message-order', upload_id: 'upload-order', client_request_id: 'request-order',
        created_at: '2026-07-20T00:00:00Z',
        file: { id: 'upload-order', name: 'ordered.txt', size: '4 B', download_url: '/download/upload-order' },
      };
      timeline.upsertUpload(upload);
      const operations = {
        rest: () => timeline.upsert(message),
        upload_completed: () => timeline.upsertUpload({ ...upload, status: 'completed', confirmedBytes: 4, progressPercent: 100 }),
        message_created: () => timeline.mergeEvent({ sequence: 31, event_type: 'message.created', entity_id: message.id, payload: message }),
      };
      JSON.parse(completionOrder).forEach(name => operations[name]());
    """)
    assert context.eval("container.querySelectorAll('[data-message-id=\"message-order\"]').length") == 1
    assert context.eval("container.querySelectorAll('[data-upload-id=\"upload-order\"]').length") == 0
    assert context.eval("container.querySelectorAll('.timeline-message').length") == 1


def test_timeline_duplicate_sequence_is_a_successful_idempotent_application() -> None:
    context = create_js_context()
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      globalThis.container = document.createElement('div');
      globalThis.timeline = __modules['./timeline.js'].createTimeline({
        container, newMessageButton: null, api: () => Promise.resolve({ items: [] }),
        onRestore: () => {},
      });
      globalThis.event = {
        sequence: 41, event_type: 'message.created', entity_id: 'idempotent-message',
        payload: { body: 'once', created_at: '2026-07-20T00:00:00Z' },
      };
      globalThis.firstApplied = timeline.mergeEvent(event);
      globalThis.replayApplied = timeline.mergeEvent(event);
    """)
    assert context.eval("firstApplied") is True
    assert context.eval("replayApplied") is True
    assert context.eval("container.querySelectorAll('[data-message-id=\"idempotent-message\"]').length") == 1


def test_upload_cards_render_text_states_metrics_and_observer_permissions() -> None:
    context = create_js_context()
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      globalThis.container = document.createElement('div');
      globalThis.timeline = __modules['./timeline.js'].createTimeline({
        container, newMessageButton: null, api: () => Promise.resolve({ items: [] }),
        onRestore: () => {}, onUploadAction: () => {},
      });
      ['queued', 'uploading', 'paused', 'verifying', 'failed', 'complete', 'cancelled', 'expired'].forEach((status, index) => {
        timeline.upsertUpload({ uploadId: `upload-${index}`, name: `${status}.txt`, sizeBytes: 100, status, confirmedBytes: 50, progressPercent: 50, speedBytesPerSecond: 10, etaSeconds: 5, isSourceDevice: true, errorMessage: status === 'failed' ? '网络中断，请重试' : null });
      });
      timeline.upsertUpload({ uploadId: 'observer', name: 'remote.txt', sizeBytes: 100, status: 'uploading', confirmedBytes: 50, progressPercent: 50, isSourceDevice: false });
    """)
    states = json.loads(context.eval(
        "JSON.stringify(container.querySelectorAll('.upload-card-status').map(node => node.textContent))"
    ))
    for label in ("等待上传", "上传中", "已暂停", "正在校验", "上传失败", "已完成", "已取消", "已过期"):
        assert label in states
    uploading_text = context.eval(
        "container.querySelector('[data-upload-id=\"upload-1\"]').querySelector('.upload-card-metrics').textContent"
    )
    for token in ("50%", "50 B / 100 B", "10 B/s", "剩余 5 秒"):
        assert token in uploading_text
    observer = context.eval("container.querySelector('[data-upload-id=\"observer\"]')")
    assert observer is not None
    assert context.eval("container.querySelector('[data-upload-id=\"observer\"]').querySelector('[data-upload-action=\"pause\"]') === null") is True
    assert context.eval("container.querySelector('[data-upload-id=\"observer\"]').querySelector('[data-upload-action=\"resume\"]') === null") is True
    assert context.eval("container.querySelector('[data-upload-id=\"observer\"]').querySelector('[data-upload-action=\"cancel\"]') !== null") is True


def test_upload_summary_drop_overlay_and_accessible_responsive_contract() -> None:
    html = read_web("index.html")
    css = read_web("styles.css")
    app = read_web("js/app.js")
    for element_id in ("uploadSummary", "pauseAllUploads", "resumeAllUploads", "cancelAllUploads", "transferDropOverlay", "uploadLiveRegion"):
        assert f'id="{element_id}"' in html
    assert "uploadSummary.hidden = activeTasks.length === 0" in app
    assert "pauseAllUploads" in app and ".pauseAll()" in app
    assert "resumeAllUploads" in app and ".resumeAll()" in app
    assert "cancelAllUploads" in app and ".cancelAll()" in app
    assert "transferDropOverlay" in app
    assert "min-width: 44px" in css and "min-height: 44px" in css
    assert "gap: 8px" in css
    assert ":focus-visible" in css
    assert "@media (max-width: 375px)" in css
    assert "overflow-x: hidden" in css
    assert "transition" in css and "180ms" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "progress" in read_web("js/timeline.js")


def test_app_reconciles_active_uploads_before_timeline_restore_and_websocket() -> None:
    source = read_web("js/app.js")
    for start in (
        source.index("async function checkSession()"),
        source.index("listen(unlockForm, 'submit'"),
        source.index("function resumeFromBFCache()"),
    ):
        section = source[start:]
        reconcile_index = section.index("await restoreUploads()")
        timeline_index = section.index("await timeline.loadInitial()")
        websocket_index = section.index("startEventConnection()")
        assert reconcile_index < timeline_index < websocket_index
    assert "uploadCoordinator.applyRemoteEvent(event)" in source


def test_upload_controls_have_touch_targets_focus_and_reduced_motion() -> None:
    html = read_web("index.html")
    css = read_web("styles.css")
    assert 'id="uploadReselectInput"' in html
    assert ".upload-card-action" in css
    assert "min-width: 44px" in css
    assert "min-height: 44px" in css
    assert ".upload-card-action:focus-visible" in css
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".timeline-upload-card" in reduced
    assert "transition: none" in reduced


def test_live_region_announces_state_and_coarse_milestones_only() -> None:
    source = read_web("js/upload-coordinator.js")
    assert "LIVE_MILESTONES" in source
    assert "[25, 50, 75, 100]" in source
    assert "UPLOAD_ANNOUNCEMENT_INTERVAL_MS" in source
    assert "1000" in source


def test_composer_module_is_served(client: TestClient) -> None:
    resp = client.get("/js/composer.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_composer_html_elements_exist(client: TestClient) -> None:
    html = client.get("/").text
    for element_id in ("composerForm", "composerTextarea", "composerFileInput",
                       "composerDropTarget"):
        assert f'id="{element_id}"' in html
    assert 'id="composerQueue"' not in html
    assert 'class="panel transfer-panel composer-dock"' in html
    assert 'Enter 发送，Shift+Enter 换行' in html


def test_api_module_has_send_text_and_upload_file(client: TestClient) -> None:
    api_js = client.get("/js/api.js").text
    assert "export async function sendText" in api_js
    assert "export function uploadFile" in api_js
    assert "client_request_id" in api_js
    assert "AbortController" not in api_js or "signal" in api_js


def test_resumable_api_constants_exports_and_raw_blob_xhr() -> None:
    config = read_web("js/config.js")
    for token in (
        "UPLOAD_CHUNK_SIZE_BYTES = 8 * 1024 * 1024",
        "MAX_UPLOAD_SIZE_BYTES = 512 * 1024 * 1024",
        "MAX_ACTIVE_UPLOADS = 9",
        "UPLOAD_RETRY_DELAYS = [500, 1000, 2000, 4000, 8000]",
        "UPLOAD_SPEED_WINDOW_MS = 5000",
        "UPLOAD_ETA_MIN_SAMPLE_MS = 2000",
    ):
        assert token in config

    context = create_js_context()
    context.eval(r"""
      globalThis.sentBody = null;
      globalThis.requestHeaders = {};
      globalThis.events = [];
      window.dispatchEvent = event => events.push(event);
      globalThis.XMLHttpRequest = class XMLHttpRequest {
        constructor() { this.upload = {}; this.status = 200; this.responseText = '{"ok":true}'; }
        open(method, path) { this.method = method; this.path = path; }
        setRequestHeader(name, value) { requestHeaders[name] = value; }
        send(body) { sentBody = body; this.onload(); }
        abort() { this.onabort(); }
      };
      globalThis.rawBlob = { marker: 'raw-blob' };
    """)
    load_js_module(context, "./api.js", read_web("js/api.js"))
    context.eval(r"""
      globalThis.partResult = null;
      __modules['./api.js'].uploadPart('upload/a', 2, rawBlob, {
        start: 8, end: 11, total: 20, sha256: 'digest',
      }).then(value => { partResult = value; });
    """)
    drain_jobs(context)
    result = json.loads(context.eval(r"""JSON.stringify({
      sameBody: sentBody === rawBlob,
      method: XMLHttpRequest.prototype.method,
      headers: requestHeaders,
      value: partResult,
    })"""))
    assert result["sameBody"] is True
    assert result["headers"] == {
        "Content-Type": "application/octet-stream",
        "Content-Range": "bytes 8-11/20",
        "X-Chunk-SHA256": "digest",
    }
    assert result["value"] == {"ok": True}

    context.eval(r"""
      XMLHttpRequest = class XMLHttpRequest {
        constructor() { this.upload = {}; this.status = 401; this.responseText = ''; }
        open() {}
        setRequestHeader() {}
        send() { this.onload(); }
      };
      globalThis.rejectedStatus = 0;
      __modules['./api.js'].uploadPart('expired', 0, rawBlob, {
        start: 0, end: 0, total: 1, sha256: 'digest',
      }).catch(error => { rejectedStatus = error.status; });
    """)
    drain_jobs(context)
    assert context.eval("rejectedStatus") == 401
    assert context.eval("events[0].type") == "session-expired"


def test_file_identity_uses_three_versioned_64k_samples_and_metadata() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.digestInputLength = 0;
      globalThis.cryptoObject = {
        subtle: {
          digest(_algorithm, bytes) {
            digestInputLength = bytes.byteLength;
            return Promise.resolve(new Uint8Array([0, 15, 16, 255]).buffer);
          },
        },
      };
      globalThis.file = {
        name: 'sample.bin', size: 300000, lastModified: 42,
        slice(start, end) {
          return { arrayBuffer: () => Promise.resolve(new Uint8Array(end - start).buffer) };
        },
      };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.identity = null;
      globalThis.matches = null;
      __modules['./upload-coordinator.js'].sampleFileIdentity(file, cryptoObject)
        .then(value => {
          identity = value;
          return __modules['./upload-coordinator.js'].matchesFileIdentity(file, value, cryptoObject);
        })
        .then(value => { matches = value; });
    """)
    drain_jobs(context)
    identity = json.loads(context.eval("JSON.stringify(identity)"))
    assert identity == {
        "name": "sample.bin",
        "size": 300000,
        "lastModified": 42,
        "sampleSha256": "000f10ff",
    }
    assert context.eval("matches") is True
    assert context.eval("digestInputLength") < 3 * 65536 + 256


def test_upload_persistence_only_clones_metadata_and_optional_handle() -> None:
    source = read_web("js/upload-persistence.js")
    assert "personal-transfer-timeline" in source
    assert "upload-tasks" in source
    assert "file:" not in source
    assert "blob:" not in source.lower()

    context = create_js_context()
    context.eval(r"""
      globalThis.stored = null;
      globalThis.transaction = {
        objectStore() {
          return {
                put(value) { stored = value; const request = {}; Promise.resolve().then(() => { request.onsuccess(); transaction.oncomplete(); }); return request; },
                getAll() { const request = {}; Promise.resolve().then(() => { request.result = stored ? [stored] : []; request.onsuccess(); transaction.oncomplete(); }); return request; },
                delete() { const request = {}; Promise.resolve().then(() => { request.onsuccess(); transaction.oncomplete(); }); return request; },
          };
        },
      };
      globalThis.database = { transaction: () => transaction, close() {} };
      globalThis.indexedDB = {
        open() {
          const request = {};
          Promise.resolve().then(() => { request.result = database; request.onsuccess(); });
          return request;
        },
      };
    """)
    load_js_module(context, "./upload-persistence.js", read_web("js/upload-persistence.js"))
    context.eval(r"""
      globalThis.persistence = __modules['./upload-persistence.js'].createUploadPersistence({ indexedDB });
      persistence.put({
        uploadId: 'u1', name: 'a.bin', status: 'paused', confirmedParts: [0],
        file: { forbidden: true }, fileHandle: { kind: 'file' }, runtimeOnly: 9,
      }).then(() => persistence.getAll()).then(value => { globalThis.persistedRows = value; });
    """)
    drain_jobs(context)
    row = json.loads(context.eval("JSON.stringify(persistedRows[0])"))
    assert "file" not in row
    assert "runtimeOnly" not in row
    assert row["fileHandle"] == {"kind": "file"}


def test_upload_coordinator_reconcile_fetches_active_before_handles_and_restores_observers() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.calls = [];
      globalThis.sent = [];
      globalThis.AbortController = class AbortController {
        constructor() { this.signal = {}; }
        abort() {}
      };
      globalThis.cryptoObject = {
        randomUUID: () => 'unused',
        subtle: { digest: (_algorithm, bytes) => Promise.resolve(new Uint8Array([bytes[bytes.byteLength - 1]]).buffer) },
      };
      globalThis.makeFile = value => ({
        name: 'source.bin', size: 8, type: '', lastModified: 7,
        slice: () => ({ size: 4, arrayBuffer: () => Promise.resolve(new Uint8Array([value]).buffer) }),
      });
      globalThis.grantedHandle = {
        queryPermission(options) { calls.push(['query', options.mode, 'granted']); return Promise.resolve('granted'); },
        getFile() { calls.push(['getFile', 'granted']); return Promise.resolve(makeFile(1)); },
      };
      globalThis.promptHandle = {
        queryPermission(options) { calls.push(['query', options.mode, 'prompt']); return Promise.resolve('prompt'); },
        getFile() { calls.push(['getFile', 'prompt']); throw new Error('must not prompt'); },
      };
      globalThis.remote = [
        { upload_id: 'granted', client_request_id: 'granted-request', original_name: 'source.bin', size_bytes: 8, status: 'paused', confirmed_parts: [0], confirmed_bytes: 4, source_device_id: 'device-a' },
        { upload_id: 'prompt', client_request_id: 'prompt-request', original_name: 'source.bin', size_bytes: 8, status: 'paused', confirmed_parts: [], confirmed_bytes: 0, source_device_id: 'device-a' },
        { upload_id: 'observer', client_request_id: 'observer-request', original_name: 'remote.bin', size_bytes: 4, status: 'uploading', confirmed_parts: [], confirmed_bytes: 2, source_device_id: 'device-b' },
      ];
      globalThis.records = [
        { uploadId: 'granted', clientRequestId: 'granted-request', fileHandle: grantedHandle, identity: { name: 'source.bin', size: 8, lastModified: 7, sampleSha256: '01' }, name: 'source.bin', sizeBytes: 8, status: 'paused', confirmedParts: [], confirmedBytes: 0, isSourceDevice: true, sourceDeviceId: 'device-a' },
        { uploadId: 'prompt', clientRequestId: 'prompt-request', fileHandle: promptHandle, identity: { name: 'source.bin', size: 8, lastModified: 7, sampleSha256: '01' }, name: 'source.bin', sizeBytes: 8, status: 'paused', confirmedParts: [], confirmedBytes: 0, isSourceDevice: true, sourceDeviceId: 'device-a' },
      ];
      globalThis.api = {
        listActiveUploads() { calls.push(['active']); return Promise.resolve(remote); },
        uploadPart(_id, index) { sent.push(index); return new Promise(() => {}); },
        controlUpload: () => Promise.resolve({}),
        getUploadSession: () => Promise.resolve({ status: 'cancelled' }),
      };
      globalThis.persistence = {
        getAll() { calls.push(['indexedDB']); return Promise.resolve(records); },
        put: () => Promise.resolve(), remove: () => Promise.resolve(), close: () => {},
      };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({
        api, persistence, cryptoObject, chunkSize: 4, maxActive: 1,
      });
      coordinator.start();
    """)
    drain_jobs(context)
    calls = json.loads(context.eval("JSON.stringify(calls)"))
    assert calls[:4] == [
        ["active"],
        ["indexedDB"],
        ["query", "read", "granted"],
        ["getFile", "granted"],
    ]
    assert ["getFile", "prompt"] not in calls
    snapshot = json.loads(context.eval("JSON.stringify(coordinator.getSnapshot())"))
    by_id = {task["uploadId"]: task for task in snapshot}
    assert by_id["granted"]["isSourceDevice"] is True
    assert by_id["granted"]["confirmedParts"] == [0]
    assert by_id["prompt"]["status"] == "paused"
    assert by_id["prompt"]["errorCode"] == "reselect_required"
    assert by_id["observer"]["isSourceDevice"] is False
    assert context.eval("sent[0]") == 1


def test_upload_coordinator_reselect_requires_exact_identity_and_sends_server_missing_parts() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.sent = [];
      globalThis.actions = [];
      globalThis.AbortController = class AbortController {
        constructor() { this.signal = {}; }
        abort() {}
      };
      globalThis.cryptoObject = {
        randomUUID: () => 'unused',
        subtle: { digest: (_algorithm, bytes) => Promise.resolve(new Uint8Array([bytes[bytes.byteLength - 1]]).buffer) },
      };
      globalThis.makeFile = value => ({
        name: 'resume.bin', size: 8, type: '', lastModified: 11,
        slice: () => ({ size: 4, arrayBuffer: () => Promise.resolve(new Uint8Array([value]).buffer) }),
      });
      globalThis.api = {
        listActiveUploads: () => Promise.resolve([{ upload_id: 'resume', client_request_id: 'request', original_name: 'resume.bin', size_bytes: 8, status: 'paused', confirmed_parts: [0], confirmed_bytes: 4 }]),
        uploadPart(_id, index) { sent.push(index); return new Promise(() => {}); },
        controlUpload(_id, action) { actions.push(action); return Promise.resolve({}); },
      };
      globalThis.persistence = {
        getAll: () => Promise.resolve([{ uploadId: 'resume', clientRequestId: 'request', identity: { name: 'resume.bin', size: 8, lastModified: 11, sampleSha256: '01' }, name: 'resume.bin', sizeBytes: 8, status: 'paused', confirmedParts: [], confirmedBytes: 0, isSourceDevice: true }]),
        put: () => Promise.resolve(), remove: () => Promise.resolve(), close: () => {},
      };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.results = [];
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({ api, persistence, cryptoObject, chunkSize: 4, maxActive: 1 });
      coordinator.start()
        .then(() => coordinator.reselect('resume', makeFile(2)))
        .then(value => { results.push(value); return coordinator.reselect('resume', makeFile(1)); })
        .then(value => { results.push(value); });
    """)
    drain_jobs(context)
    assert json.loads(context.eval("JSON.stringify(results)")) == [False, True]
    assert json.loads(context.eval("JSON.stringify(sent)")) == [1]
    assert "resume" in json.loads(context.eval("JSON.stringify(actions)"))
    assert context.eval("coordinator.getSnapshot()[0].errorCode") is None


def test_upload_coordinator_observer_guidance_terminal_order_and_remote_cancel_abort() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.abortCalls = 0;
      globalThis.AbortController = class AbortController {
        constructor() { this.signal = {}; }
        abort() { abortCalls += 1; }
      };
      globalThis.cryptoObject = { randomUUID: () => 'source', subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) } };
      globalThis.file = { name: 'source.bin', size: 4, type: '', lastModified: 1, slice: () => ({ size: 4, arrayBuffer: () => Promise.resolve(new Uint8Array(4).buffer) }) };
      globalThis.api = {
        createUploadSession: () => Promise.resolve({ upload_id: 'source', status: 'queued', confirmed_parts: [] }),
        uploadPart: () => new Promise(() => {}),
        controlUpload: () => Promise.resolve({}), cancelUpload: () => Promise.resolve({}),
      };
      globalThis.persistence = { getAll: () => Promise.resolve([]), put: () => Promise.resolve(), remove: () => Promise.resolve(), close: () => {} };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({ api, persistence, cryptoObject, chunkSize: 4 });
      coordinator.enqueueFiles([file]);
    """)
    drain_jobs(context)
    context.eval(r"""
      coordinator.applyRemoteEvent({ event_type: 'upload.cancelled', sequence: 10, payload: { upload_id: 'source', status: 'cancelled', updated_at: '2026-07-20T00:00:00Z' } });
      globalThis.staleTerminalHandled = coordinator.applyRemoteEvent({ event_type: 'upload.progress', sequence: 11, payload: { upload_id: 'source', status: 'uploading', confirmed_bytes: 4, updated_at: '2026-07-20T00:00:01Z' } });
      coordinator.applyRemoteEvent({ event_type: 'upload.created', sequence: 12, payload: { upload_id: 'observer', original_name: 'remote.bin', size_bytes: 4, status: 'paused' } });
      coordinator.pause('observer');
      coordinator.resume('observer');
    """)
    assert context.eval("abortCalls") == 1
    assert context.eval("staleTerminalHandled") is True
    assert context.eval("coordinator.getSnapshot().find(task => task.uploadId === 'source').status") == "cancelled"
    assert context.eval("coordinator.getSnapshot().find(task => task.uploadId === 'observer').errorCode") == "source_device_required"
    assert context.eval("coordinator.getSnapshot().find(task => task.uploadId === 'observer').errorMessage") == "源设备控制暂停和继续"


def test_upload_live_region_combines_state_and_milestone_in_one_dom_announcement() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.clock = 0;
      globalThis.announcementCalls = 0;
      globalThis.cryptoObject = { randomUUID: () => 'unused', subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) } };
      globalThis.persistence = { getAll: () => Promise.resolve([]), put: () => Promise.resolve(), remove: () => Promise.resolve(), close: () => {} };
      globalThis.api = {};
      globalThis.__modules['./api.js'] = {
        request: () => Promise.resolve({}), unlock: () => Promise.resolve({}), logout: () => Promise.resolve({}),
        getSession: () => Promise.reject(new Error('locked')), ApiError: class ApiError extends Error {},
        connectEvents: () => ({ close() {} }), getLastSequence: () => 0,
      };
      globalThis.__modules['./timeline.js'] = { createTimeline: () => ({
        loadInitial: () => Promise.resolve(), upsertUpload() {}, mergeEvent: () => true, focusMessage() {}, destroy() {},
      }) };
      globalThis.__modules['./composer.js'] = { createComposer: () => ({ destroy() {} }) };
      globalThis.__modules['./upload-persistence.js'] = { createUploadPersistence: () => persistence };
      globalThis.__modules['./library.js'] = { createLibrary: () => ({ load: () => Promise.resolve(), clearSelection() {}, applyEvent: () => true, destroy() {} }) };
      globalThis.__modules['./navigation.js'] = { createNavigation: () => ({ start() {}, navigate: () => Promise.resolve(), destroy() {} }) };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    load_js_module(context, "./app.js", read_web("js/app.js"))
    drain_jobs(context)
    context.eval(r"""
      globalThis.region = document.getElementById('uploadLiveRegion');
      globalThis.announceToRegion = __modules['./app.js'].createLiveRegionAnnouncer(region);
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({
        api, persistence, cryptoObject, now: () => clock,
        onAnnounce: message => { announcementCalls += 1; announceToRegion(message); },
      });
      coordinator.applyRemoteEvent({ event_type: 'upload.created', sequence: 1, payload: { upload_id: 'remote', original_name: 'remote.bin', size_bytes: 100, status: 'queued', confirmed_bytes: 0 } });
      clock = 1000;
      coordinator.applyRemoteEvent({ event_type: 'upload.progress', sequence: 2, payload: { upload_id: 'remote', status: 'uploading', confirmed_bytes: 25 } });
      globalThis.combinedText = region.children[0].textContent;
      globalThis.firstNode = region.children[0];
      announceToRegion(combinedText);
      globalThis.repeatedNode = region.children[0];
    """)
    assert context.eval("announcementCalls") == 1
    assert "上传中" in context.eval("combinedText")
    assert "25%" in context.eval("combinedText")
    assert context.eval("firstNode !== repeatedNode") is True
    assert context.eval("region.children.length") == 1


def test_upload_notify_combines_all_changed_tasks_once_per_reconcile() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.announcements = [];
      globalThis.cryptoObject = { randomUUID: () => 'unused', subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) } };
      globalThis.remote = [
        { upload_id: 'one', original_name: 'one.bin', size_bytes: 100, status: 'uploading', confirmed_bytes: 25, confirmed_parts: [] },
        { upload_id: 'two', original_name: 'two.bin', size_bytes: 100, status: 'uploading', confirmed_bytes: 50, confirmed_parts: [] },
        { upload_id: 'three', original_name: 'three.bin', size_bytes: 100, status: 'uploading', confirmed_bytes: 75, confirmed_parts: [] },
      ];
      globalThis.records = remote.map(item => ({
        uploadId: item.upload_id, name: item.original_name, sizeBytes: item.size_bytes,
        status: 'queued', confirmedBytes: 0, confirmedParts: [], isSourceDevice: false,
        announcedStatus: 'queued',
      }));
      globalThis.api = { listActiveUploads: () => Promise.resolve(remote) };
      globalThis.persistence = {
        getAll: () => Promise.resolve(records), put: () => Promise.resolve(),
        remove: () => Promise.resolve(), close: () => {},
      };
      globalThis.__modules['./api.js'] = {
        request: () => Promise.resolve({}), unlock: () => Promise.resolve({}), logout: () => Promise.resolve({}),
        getSession: () => Promise.reject(new Error('locked')), ApiError: class ApiError extends Error {},
        connectEvents: () => ({ close() {} }), getLastSequence: () => 0,
      };
      globalThis.__modules['./timeline.js'] = { createTimeline: () => ({
        loadInitial: () => Promise.resolve(), upsertUpload() {}, mergeEvent: () => true, focusMessage() {}, destroy() {},
      }) };
      globalThis.__modules['./composer.js'] = { createComposer: () => ({ destroy() {} }) };
      globalThis.__modules['./upload-persistence.js'] = { createUploadPersistence: () => persistence };
      globalThis.__modules['./library.js'] = { createLibrary: () => ({ load: () => Promise.resolve(), clearSelection() {}, applyEvent: () => true, destroy() {} }) };
      globalThis.__modules['./navigation.js'] = { createNavigation: () => ({ start() {}, navigate: () => Promise.resolve(), destroy() {} }) };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    load_js_module(context, "./app.js", read_web("js/app.js"))
    drain_jobs(context)
    context.eval(r"""
      globalThis.region = document.getElementById('multiUploadLiveRegion');
      globalThis.announceToRegion = __modules['./app.js'].createLiveRegionAnnouncer(region);
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({
        api, persistence, cryptoObject,
        onAnnounce: message => { announcements.push(message); announceToRegion(message); },
      });
      coordinator.reconcile();
    """)
    drain_jobs(context)
    assert context.eval("announcements.length") == 1
    combined_text = context.eval("region.children[0].textContent")
    assert all(name in combined_text for name in ("one.bin", "two.bin", "three.bin"))
    assert all(progress in combined_text for progress in ("25%", "50%", "75%"))
    assert combined_text.count("上传中") == 3
    context.eval("coordinator.applyRemoteEvent({ event_type: 'upload.progress', sequence: 2, payload: remote[0] });")
    assert context.eval("announcements.length") == 1
    assert context.eval("region.children.length") == 1


def test_upload_coordinator_limits_active_files_and_one_part_per_file() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.__modules['./api.js'] = {};
      globalThis.__modules['./upload-persistence.js'] = {};
      globalThis.activeParts = {};
      globalThis.peakFiles = 0;
      globalThis.duplicatePart = false;
      globalThis.pendingParts = [];
      globalThis.uuidIndex = 0;
      globalThis.AbortController = class AbortController {
        constructor() { this.signal = { aborted: false }; }
        abort() { this.signal.aborted = true; }
      };
      globalThis.cryptoObject = {
        randomUUID: () => `00000000-0000-4000-8000-${String(++uuidIndex).padStart(12, '0')}`,
        subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) },
      };
      globalThis.api = {
        createUploadSession: metadata => Promise.resolve({
          upload_id: metadata.clientRequestId, status: 'queued', confirmed_parts: [],
          confirmed_bytes: 0, chunk_size_bytes: 4,
        }),
        uploadPart: (id, index) => {
          activeParts[id] = (activeParts[id] || 0) + 1;
          duplicatePart = duplicatePart || activeParts[id] > 1;
          peakFiles = Math.max(peakFiles, Object.keys(activeParts).filter(key => activeParts[key] > 0).length);
          return new Promise(resolve => pendingParts.push({ id, index, resolve }))
            .finally(() => { activeParts[id] -= 1; });
        },
        completeUpload: id => Promise.resolve({ id: `message-${id}`, upload_id: id }),
        getUploadSession: id => Promise.resolve({ upload_id: id, confirmed_parts: [] }),
        controlUpload: () => Promise.resolve({}), cancelUpload: () => Promise.resolve({}),
      };
      globalThis.persistence = { put: () => Promise.resolve(), getAll: () => Promise.resolve([]), remove: () => Promise.resolve(), close: () => {} };
      globalThis.files = Array.from({ length: 11 }, (_, index) => ({
        name: `file-${index}.txt`, size: 4, type: 'text/plain', lastModified: index,
        slice: () => ({ size: 4, arrayBuffer: () => Promise.resolve(new Uint8Array([1, 2, 3, 4]).buffer) }),
      }));
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({
        api, persistence, cryptoObject, now: () => Date.now(),
        delay: () => Promise.resolve(), maxActive: 9, chunkSize: 4,
      });
      coordinator.enqueueFiles(files);
    """)
    drain_jobs(context)
    assert context.eval("peakFiles") == 9
    assert context.eval("duplicatePart") is False
    statuses = json.loads(context.eval(
        "JSON.stringify(coordinator.getSnapshot().map(task => task.status))"
    ))
    assert statuses.count("uploading") == 9
    assert statuses.count("queued") == 2

    context.eval(r"""
      pendingParts.splice(0).forEach(part => part.resolve({
        status: 'uploading', confirmed_parts: [part.index], confirmed_bytes: 4,
      }));
    """)
    drain_jobs(context)
    assert context.eval("pendingParts.length") == 2

    context.eval(r"""
      pendingParts.splice(0).forEach(part => part.resolve({
        status: 'uploading', confirmed_parts: [part.index], confirmed_bytes: 4,
      }));
    """)
    drain_jobs(context)
    assert context.eval("coordinator.getSnapshot().every(task => task.status === 'completed')") is True
    assert context.eval("Object.values(activeParts).every(count => count === 0)") is True
    assert context.eval("pendingParts.length") == 0
    assert context.eval("peakFiles") == 9
    assert context.eval("coordinator.getSnapshot().every(Object.isFrozen)") is True


def test_upload_coordinator_controls_missing_retry_priority_size_and_eta() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.__modules['./api.js'] = {};
      globalThis.__modules['./upload-persistence.js'] = {};
      globalThis.AbortController = class AbortController {
        constructor() { this.signal = { aborted: false }; }
        abort() { this.signal.aborted = true; }
      };
      globalThis.clock = 0;
      globalThis.created = [];
      globalThis.sent = [];
      globalThis.pending = [];
      globalThis.delays = [];
      globalThis.uuidIndex = 0;
      globalThis.cryptoObject = {
        randomUUID: () => `id-${++uuidIndex}`,
        subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) },
      };
      globalThis.api = {
        createUploadSession(metadata) {
          created.push(metadata.name);
          return Promise.resolve({ upload_id: metadata.clientRequestId, confirmed_parts: metadata.name === 'missing' ? [0] : [] });
        },
        uploadPart(id, index, _blob, _metadata, onProgress) {
          sent.push([id, index]);
          onProgress(2, 4);
          if (id === 'id-4') return Promise.reject(new Error('offline'));
          if (id === 'id-2') return new Promise((resolve, reject) => pending.push({ id, index, resolve, reject }));
          clock += 2500;
          return Promise.resolve({ confirmed_parts: id === 'id-3' ? [0, index] : [index], confirmed_bytes: (index + 1) * 4 });
        },
        completeUpload: id => Promise.resolve({ upload_id: id }),
        getUploadSession: id => Promise.resolve({ upload_id: id, confirmed_parts: id === 'id-3' ? [0] : [] }),
        controlUpload: () => Promise.resolve({}), cancelUpload: () => Promise.resolve({}),
      };
      globalThis.persistence = { put: () => Promise.resolve(), getAll: () => Promise.resolve([]), remove: () => Promise.resolve(), close: () => {} };
      globalThis.makeFile = (name, size) => ({
        name, size, type: '', lastModified: 1,
        slice(start, end) { return { size: end - start, arrayBuffer: () => Promise.resolve(new Uint8Array(end - start).buffer) }; },
      });
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({
        api, persistence, cryptoObject, now: () => clock,
        delay: ms => { delays.push(ms); return Promise.resolve(); }, maxActive: 1, chunkSize: 4,
      });
      coordinator.enqueueFiles([makeFile('oversized', 513 * 1024 * 1024)]);
      coordinator.enqueueFiles([makeFile('blocked', 8), makeFile('missing', 8), makeFile('offline', 4)]);
      coordinator.pause('id-2');
    """)
    drain_jobs(context)
    assert "oversized" not in json.loads(context.eval("JSON.stringify(created)"))
    assert context.eval("coordinator.getSnapshot()[0].errorCode") == "file-too-large"
    assert context.eval("sent.some(item => item[0] === 'id-3' && item[1] === 0)") is False
    assert context.eval("sent.some(item => item[0] === 'id-3' && item[1] === 1)") is True
    assert context.eval("coordinator.getSnapshot().find(task => task.uploadId === 'id-3').etaSeconds !== null") is True
    assert json.loads(context.eval("JSON.stringify(delays)"))[-5:] == [500, 1000, 2000, 4000, 8000]

    context.eval(r"""
      coordinator.resume('id-2');
    """)
    drain_jobs(context)
    context.eval(r"""
      coordinator.pause('id-2');
      const blocked = pending.find(item => item.id === 'id-2');
      if (blocked) blocked.resolve({ confirmed_parts: [0], confirmed_bytes: 4 });
    """)
    drain_jobs(context)
    assert context.eval("sent.filter(item => item[0] === 'id-2').length") == 1
    assert context.eval("coordinator.getSnapshot().find(task => task.uploadId === 'id-2').status") == "paused"


def test_upload_coordinator_prioritize_reorders_queued_tasks_only() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.AbortController = class AbortController {
        constructor() { this.signal = { aborted: false }; }
        abort() { this.signal.aborted = true; }
      };
      globalThis.uuidIndex = 0;
      globalThis.cryptoObject = {
        randomUUID: () => `priority-${++uuidIndex}`,
        subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) },
      };
      globalThis.firstPart = null;
      globalThis.api = {
        createUploadSession: metadata => Promise.resolve({ upload_id: metadata.clientRequestId, confirmed_parts: [] }),
        uploadPart: id => id === 'priority-1'
          ? new Promise(resolve => { firstPart = resolve; })
          : Promise.resolve({ confirmed_parts: [0], confirmed_bytes: 4 }),
        completeUpload: id => Promise.resolve({ upload_id: id }),
        controlUpload: () => Promise.resolve({}), cancelUpload: () => Promise.resolve({}),
      };
      globalThis.persistence = { put: () => Promise.resolve(), getAll: () => Promise.resolve([]), remove: () => Promise.resolve(), close: () => {} };
      globalThis.files = ['first', 'second', 'third'].map(name => ({
        name, size: 4, type: '', lastModified: 1,
        slice: () => ({ size: 4, arrayBuffer: () => Promise.resolve(new Uint8Array(4).buffer) }),
      }));
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({
        api, persistence, cryptoObject, delay: () => Promise.resolve(), maxActive: 1, chunkSize: 4,
      });
      coordinator.enqueueFiles(files);
    """)
    drain_jobs(context)
    context.eval("coordinator.prioritize('priority-3')")
    order = json.loads(context.eval(
        "JSON.stringify(coordinator.getSnapshot().filter(task => task.status === 'queued').map(task => task.uploadId))"
    ))
    assert order == ["priority-3", "priority-2"]
    assert context.eval("coordinator.getSnapshot()[0].status") == "uploading"


def test_upload_persistence_waits_for_commit_and_rejects_abort_or_error() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.transactions = [];
      globalThis.mode = 'success';
      globalThis.database = {
        transaction() {
          const transaction = {
            error: null,
            objectStore() {
              return {
                getAll() {
                  const request = {};
                  Promise.resolve().then(() => {
                    request.result = [{ uploadId: 'cached' }];
                    request.onsuccess();
                  });
                  return request;
                },
              };
            },
          };
          transactions.push(transaction);
          return transaction;
        },
        close() {},
      };
      globalThis.indexedDB = {
        open() {
          const request = {};
          Promise.resolve().then(() => { request.result = database; request.onsuccess(); });
          return request;
        },
      };
    """)
    load_js_module(context, "./upload-persistence.js", read_web("js/upload-persistence.js"))
    context.eval(r"""
      globalThis.persistence = __modules['./upload-persistence.js'].createUploadPersistence({ indexedDB });
      globalThis.rows = null;
      globalThis.failure = null;
      persistence.getAll().then(value => { rows = value; }).catch(error => { failure = error.message; });
    """)
    drain_jobs(context)
    assert context.eval("rows === null") is True
    context.eval("transactions[0].oncomplete()")
    drain_jobs(context)
    assert json.loads(context.eval("JSON.stringify(rows)")) == [{"uploadId": "cached"}]

    context.eval(r"""
      rows = null; failure = null;
      persistence.getAll().then(value => { rows = value; }).catch(error => { failure = error.message; });
    """)
    drain_jobs(context)
    context.eval(r"""
      transactions[1].error = new Error('transaction aborted');
      transactions[1].onabort();
    """)
    drain_jobs(context)
    assert context.eval("failure") == "transaction aborted"

    context.eval(r"""
      failure = null;
      persistence.getAll().catch(error => { failure = error.message; });
    """)
    drain_jobs(context)
    context.eval(r"""
      transactions[2].error = new Error('transaction error');
      transactions[2].onerror();
    """)
    drain_jobs(context)
    assert context.eval("failure") == "transaction error"


def test_upload_persistence_clone_fallback_starts_after_failed_transaction() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.transactions = [];
      globalThis.records = [];
      globalThis.database = {
        transaction() {
          const index = transactions.length;
          const transaction = {
            error: null,
            objectStore() {
              return {
                put(record) {
                  records.push({ ...record });
                  const request = {};
                  Promise.resolve().then(() => {
                    if (index === 0) {
                      request.onerror && request.onerror();
                      transaction.error = { name: 'DataCloneError', message: 'cannot clone handle' };
                      transaction.onabort();
                    } else {
                      request.result = record.uploadId;
                      request.onsuccess();
                      transaction.oncomplete();
                    }
                  });
                  return request;
                },
              };
            },
          };
          transactions.push(transaction);
          return transaction;
        },
        close() {},
      };
      globalThis.indexedDB = { open() { const request = {}; Promise.resolve().then(() => { request.result = database; request.onsuccess(); }); return request; } };
    """)
    load_js_module(context, "./upload-persistence.js", read_web("js/upload-persistence.js"))
    context.eval(r"""
      globalThis.result = null;
      const persistence = __modules['./upload-persistence.js'].createUploadPersistence({ indexedDB });
      persistence.put({ uploadId: 'clone', name: 'x', fileHandle: { kind: 'file' } })
        .then(value => { result = value; });
    """)
    drain_jobs(context)
    assert context.eval("transactions.length") == 2
    assert context.eval("records[0].fileHandle.kind") == "file"
    assert context.eval("Object.prototype.hasOwnProperty.call(records[1], 'fileHandle')") is False
    assert context.eval("result") == "clone"


def test_upload_persistence_close_is_permanent_during_open_and_transaction() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.openCount = 0;
      globalThis.openRequest = null;
      globalThis.transactionCount = 0;
      globalThis.databaseClosed = 0;
      globalThis.transaction = null;
      globalThis.database = {
        transaction() {
          transactionCount += 1;
          transaction = {
            objectStore() {
              return { getAll() { const request = {}; Promise.resolve().then(() => { request.result = []; request.onsuccess(); }); return request; } };
            },
          };
          return transaction;
        },
        close() { databaseClosed += 1; },
      };
      globalThis.indexedDB = { open() { openCount += 1; openRequest = {}; return openRequest; } };
    """)
    load_js_module(context, "./upload-persistence.js", read_web("js/upload-persistence.js"))
    context.eval(r"""
      globalThis.persistence = __modules['./upload-persistence.js'].createUploadPersistence({ indexedDB });
      globalThis.firstError = null;
      globalThis.secondError = null;
      persistence.getAll().catch(error => { firstError = [error.name, error.message]; });
      persistence.close();
      openRequest.result = database;
      openRequest.onsuccess();
      persistence.getAll().catch(error => { secondError = [error.name, error.message]; });
    """)
    drain_jobs(context)
    assert json.loads(context.eval("JSON.stringify(firstError)"))[0] == "ClosedError"
    assert json.loads(context.eval("JSON.stringify(secondError)"))[0] == "ClosedError"
    assert context.eval("openCount") == 1
    assert context.eval("transactionCount") == 0
    assert context.eval("databaseClosed") == 1

    context = create_js_context()
    context.eval(r"""
      globalThis.transaction = null;
      globalThis.database = {
        transaction() {
          transaction = {
            objectStore() { return { getAll() { const request = {}; Promise.resolve().then(() => { request.result = ['late']; request.onsuccess(); }); return request; } }; },
          };
          return transaction;
        },
        close() {},
      };
      globalThis.indexedDB = { open() { const request = {}; Promise.resolve().then(() => { request.result = database; request.onsuccess(); }); return request; } };
    """)
    load_js_module(context, "./upload-persistence.js", read_web("js/upload-persistence.js"))
    context.eval(r"""
      globalThis.failure = null;
      globalThis.value = null;
      globalThis.persistence = __modules['./upload-persistence.js'].createUploadPersistence({ indexedDB });
      persistence.getAll().then(result => { value = result; }).catch(error => { failure = error.name; });
    """)
    drain_jobs(context)
    context.eval("persistence.close(); transaction.oncomplete();")
    drain_jobs(context)
    assert context.eval("failure") == "ClosedError"
    assert context.eval("value === null") is True


def test_upload_persistence_close_rejects_permanently_blocked_open() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.openCount = 0;
      globalThis.openRequest = null;
      globalThis.indexedDB = {
        open() {
          openCount += 1;
          openRequest = {};
          Promise.resolve().then(() => openRequest.onblocked && openRequest.onblocked());
          return openRequest;
        },
      };
    """)
    load_js_module(context, "./upload-persistence.js", read_web("js/upload-persistence.js"))
    context.eval(r"""
      globalThis.failure = null;
      globalThis.secondFailure = null;
      globalThis.persistence = __modules['./upload-persistence.js'].createUploadPersistence({ indexedDB });
      persistence.getAll().catch(error => { failure = [error.name, error.message]; });
    """)
    drain_jobs(context)
    assert context.eval("failure === null") is True
    context.eval(r"""
      persistence.close();
      persistence.getAll().catch(error => { secondFailure = [error.name, error.message]; });
    """)
    drain_jobs(context)
    assert json.loads(context.eval("JSON.stringify(failure)"))[0] == "ClosedError"
    assert json.loads(context.eval("JSON.stringify(secondFailure)"))[0] == "ClosedError"
    assert context.eval("openCount") == 1


def test_resumable_api_xhr_settles_once_and_removes_abort_listener() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.added = 0;
      globalThis.removed = 0;
      globalThis.abortHandler = null;
      globalThis.signal = {
        aborted: false,
        addEventListener(_type, handler) { added += 1; abortHandler = handler; },
        removeEventListener(_type, handler) { if (handler === abortHandler) removed += 1; },
      };
      globalThis.XMLHttpRequest = class XMLHttpRequest {
        constructor() { this.upload = {}; this.status = 200; this.responseText = '{}'; globalThis.xhr = this; }
        open() {}
        setRequestHeader() {}
        send() { this.onload(); }
        abort() { this.onabort(); }
      };
    """)
    load_js_module(context, "./api.js", read_web("js/api.js"))
    context.eval(r"""
      globalThis.settles = 0;
      __modules['./api.js'].uploadPart('u', 0, {}, { start: 0, end: 0, total: 1, sha256: 'x' }, null, signal)
        .then(() => { settles += 1; }, () => { settles += 1; });
    """)
    drain_jobs(context)
    context.eval("xhr.onerror(); xhr.onabort();")
    drain_jobs(context)
    assert context.eval("added") == 1
    assert context.eval("removed") == 1
    assert context.eval("settles") == 1


def test_upload_coordinator_unions_out_of_order_confirmations_and_rejects_stale_events() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.AbortController = class AbortController { constructor() { this.signal = {}; } abort() {} };
      globalThis.lateProgress = null;
      globalThis.cryptoObject = {
        randomUUID: () => 'union-id',
        subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) },
      };
      globalThis.file = {
        name: 'union.bin', size: 8, type: '', lastModified: 1,
        slice: () => ({ size: 4, arrayBuffer: () => Promise.resolve(new Uint8Array(4).buffer) }),
      };
      globalThis.api = {
        createUploadSession: () => Promise.resolve({ upload_id: 'union-id', confirmed_parts: [1], confirmed_bytes: 4, updated_at: '2026-07-19T10:00:00Z', version: 1 }),
        uploadPart(_id, _index, _blob, _metadata, onProgress) {
          lateProgress = onProgress;
          return Promise.resolve({ confirmed_parts: [0], confirmed_bytes: 4, updated_at: '2026-07-19T10:00:01Z', version: 2 });
        },
        completeUpload: () => new Promise(() => {}),
        controlUpload: () => Promise.resolve({}), cancelUpload: () => Promise.resolve({}),
      };
      globalThis.persistence = { put: () => Promise.resolve(), getAll: () => Promise.resolve([]), remove: () => Promise.resolve(), close: () => {} };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({
        api, persistence, cryptoObject, delay: () => Promise.resolve(), maxActive: 1, chunkSize: 4,
      });
      coordinator.enqueueFiles([file]);
    """)
    drain_jobs(context)
    assert json.loads(context.eval("JSON.stringify(coordinator.getSnapshot()[0].confirmedParts)")) == [0, 1]
    context.eval("lateProgress(1, 4)")
    assert json.loads(context.eval("JSON.stringify(coordinator.getSnapshot()[0].confirmedParts)")) == [0, 1]
    context.eval(r"""
      coordinator.applyRemoteEvent({ upload_id: 'union-id', confirmed_parts: [0, 1], sequence: 10, updated_at: '2026-07-19T10:00:10Z', version: 10 });
      coordinator.applyRemoteEvent({ upload_id: 'union-id', confirmed_parts: [0], sequence: 9, updated_at: '2026-07-19T10:00:09Z', version: 9 });
    """)
    assert json.loads(context.eval("JSON.stringify(coordinator.getSnapshot()[0].confirmedParts)")) == [0, 1]


def test_upload_coordinator_snapshot_hides_runtime_references_and_is_deep_frozen() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.cryptoObject = { randomUUID: () => 'snapshot-id', subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) } };
      globalThis.file = { name: 'private.bin', size: 4, type: '', lastModified: 1, slice() {} };
      globalThis.handle = { kind: 'file', mutable: true };
      globalThis.api = { createUploadSession: () => new Promise(() => {}) };
      globalThis.persistence = { put: () => Promise.resolve(), getAll: () => Promise.resolve([]), remove: () => Promise.resolve(), close: () => {} };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({ api, persistence, cryptoObject });
      coordinator.enqueueFiles([{ file, fileHandle: handle }]);
    """)
    drain_jobs(context)
    context.eval("globalThis.snapshot = coordinator.getSnapshot()[0]")
    assert context.eval("Object.prototype.hasOwnProperty.call(snapshot, 'file')") is True
    assert context.eval("Object.prototype.hasOwnProperty.call(snapshot, 'fileHandle')") is True
    assert context.eval("snapshot.file === null") is True
    assert context.eval("snapshot.fileHandle === null") is True
    assert context.eval("Object.prototype.hasOwnProperty.call(snapshot, 'controller')") is False
    assert context.eval("Object.isFrozen(snapshot)") is True
    assert context.eval("Object.isFrozen(snapshot.confirmedParts)") is True
    assert context.eval("Object.isFrozen(snapshot.identity)") is True


def test_upload_coordinator_retry_uses_authoritative_missing_snapshot() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.AbortController = class AbortController { constructor() { this.signal = {}; } abort() {} };
      globalThis.phase = 'fail';
      globalThis.sent = [];
      globalThis.cryptoObject = {
        randomUUID: () => 'retry-authority',
        subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) },
      };
      globalThis.file = {
        name: 'retry.bin', size: 8, type: '', lastModified: 1,
        slice: () => ({ size: 4, arrayBuffer: () => Promise.resolve(new Uint8Array(4).buffer) }),
      };
      globalThis.api = {
        createUploadSession: () => Promise.resolve({ upload_id: 'retry-authority', confirmed_parts: [0], confirmed_bytes: 4 }),
        uploadPart(_id, index) {
          sent.push(index);
          return phase === 'fail'
            ? Promise.reject(new Error('offline'))
            : Promise.resolve({ confirmed_parts: [index], confirmed_bytes: 8 });
        },
        getUploadSession: () => Promise.resolve({ confirmed_parts: [1], confirmed_bytes: 4, updated_at: '2026-07-19T11:00:00Z', version: 5 }),
        completeUpload: () => new Promise(() => {}),
        controlUpload: () => Promise.resolve({}), cancelUpload: () => Promise.resolve({}),
      };
      globalThis.persistence = { put: () => Promise.resolve(), getAll: () => Promise.resolve([]), remove: () => Promise.resolve(), close: () => {} };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({
        api, persistence, cryptoObject, delay: () => Promise.resolve(), maxActive: 1, chunkSize: 4,
      });
      coordinator.enqueueFiles([file]);
    """)
    drain_jobs(context)
    assert context.eval("coordinator.getSnapshot()[0].status") == "failed"
    context.eval("phase = 'success'; sent = []; coordinator.retry('retry-authority')")
    drain_jobs(context)
    assert json.loads(context.eval("JSON.stringify(sent)"))[0] == 0


def test_upload_coordinator_unsubscribe_and_destroy_cleanup_listeners() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.cryptoObject = { randomUUID: () => 'listener-id', subtle: { digest: () => new Promise(() => {}) } };
      globalThis.api = {};
      globalThis.persistence = { put: () => Promise.resolve(), getAll: () => Promise.resolve([]), remove: () => Promise.resolve(), close: () => {} };
      globalThis.file = { name: 'listener.bin', size: 4, type: '', lastModified: 1 };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.firstCalls = 0;
      globalThis.secondCalls = 0;
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({ api, persistence, cryptoObject });
      const unsubscribe = coordinator.subscribe(() => { firstCalls += 1; });
      coordinator.subscribe(() => { secondCalls += 1; });
      unsubscribe();
      coordinator.enqueueFiles([file]);
      coordinator.destroy();
      coordinator.enqueueFiles([file]);
    """)
    assert context.eval("firstCalls") == 1
    assert context.eval("secondCalls") == 2


@pytest.mark.parametrize("method", ["start", "reconcile"])
def test_upload_coordinator_destroy_rejects_pending_public_operations(method: str) -> None:
    context = create_js_context()
    context.set("method", method)
    context.eval(r"""
      globalThis.closeCalls = 0;
      globalThis.deferred = {};
      globalThis.makePending = name => new Promise((resolve, reject) => { deferred[name] = { resolve, reject }; });
      globalThis.api = {
        listActiveUploads: () => makePending('reconcile'),
      };
      globalThis.persistence = {
        getAll: () => makePending('start'), put: () => Promise.resolve(), remove: () => Promise.resolve(),
        close: () => { closeCalls += 1; },
      };
      globalThis.cryptoObject = { randomUUID: () => 'race-id', subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) } };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.failure = null;
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({ api, persistence, cryptoObject });
      coordinator[method]().catch(error => { failure = [error.name, error.message]; });
      coordinator.destroy();
    """)
    drain_jobs(context)
    failure = json.loads(context.eval("JSON.stringify(failure)"))
    assert failure[0] == "CoordinatorDestroyedError"
    assert context.eval("closeCalls") == 1

    context.eval(r"""
      globalThis.lateHandled = true;
      deferred.reconcile.reject(new Error('late external rejection'));
    """)
    drain_jobs(context)
    assert context.eval("lateHandled") is True


def test_upload_coordinator_destroy_rejects_pending_retry_and_releases_task_payload() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.AbortController = class AbortController { constructor() { this.signal = {}; } abort() {} };
      globalThis.getReject = null;
      globalThis.cryptoObject = {
        randomUUID: () => 'retry-race',
        subtle: { digest: () => Promise.resolve(new Uint8Array(32).buffer) },
      };
      globalThis.file = {
        name: 'retry-race.bin', size: 4, type: '', lastModified: 1,
        slice: () => ({ size: 4, arrayBuffer: () => Promise.resolve(new Uint8Array(4).buffer) }),
      };
      globalThis.api = {
        createUploadSession: () => Promise.resolve({ upload_id: 'retry-race', confirmed_parts: [] }),
        uploadPart: () => Promise.reject(new Error('offline')),
        getUploadSession: () => new Promise((_resolve, reject) => { getReject = reject; }),
        controlUpload: () => Promise.resolve({}), cancelUpload: () => Promise.resolve({}),
      };
      globalThis.persistence = { put: () => Promise.resolve(), getAll: () => Promise.resolve([]), remove: () => Promise.resolve(), close: () => {} };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.failure = null;
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({
        api, persistence, cryptoObject, delay: () => Promise.resolve(), maxActive: 1, chunkSize: 4,
      });
      coordinator.enqueueFiles([file]);
    """)
    drain_jobs(context)
    context.eval(r"""
      coordinator.retry('retry-race').catch(error => { failure = error.name; });
      coordinator.destroy();
    """)
    drain_jobs(context)
    assert context.eval("failure") == "CoordinatorDestroyedError"
    context.eval("getReject(new Error('late retry rejection'))")
    drain_jobs(context)


@pytest.mark.parametrize("stage", ["identity", "create", "part", "complete", "reconcile"])
def test_upload_coordinator_destroy_blocks_each_late_await_stage(stage: str) -> None:
    context = create_js_context()
    context.set("stage", stage)
    context.eval(r"""
      globalThis.deferred = {};
      globalThis.makeDeferred = name => {
        let resolve;
        const promise = new Promise(done => { resolve = done; });
        deferred[name] = { promise, resolve };
        return promise;
      };
      globalThis.persistCalls = 0;
      globalThis.closeCalls = 0;
      globalThis.notifications = 0;
      globalThis.AbortController = class AbortController { constructor() { this.signal = {}; } abort() {} };
      globalThis.cryptoObject = {
        randomUUID: () => 'destroy-id',
        subtle: { digest: () => stage === 'identity' ? makeDeferred('identity') : Promise.resolve(new Uint8Array(32).buffer) },
      };
      globalThis.file = {
        name: 'destroy.bin', size: stage === 'complete' ? 0 : 4, type: '', lastModified: 1,
        slice: () => ({ size: 4, arrayBuffer: () => Promise.resolve(new Uint8Array(4).buffer) }),
      };
      globalThis.api = {
        createUploadSession: () => stage === 'create' ? makeDeferred('create') : Promise.resolve({ upload_id: 'destroy-id', confirmed_parts: [] }),
        uploadPart: () => stage === 'part' ? makeDeferred('part') : Promise.resolve({ confirmed_parts: [0], confirmed_bytes: 4 }),
        completeUpload: () => stage === 'complete' ? makeDeferred('complete') : new Promise(() => {}),
        listActiveUploads: () => stage === 'reconcile' ? makeDeferred('reconcile') : Promise.resolve([]),
        controlUpload: () => Promise.resolve({}), cancelUpload: () => Promise.resolve({}),
      };
      globalThis.persistence = {
        put: () => { persistCalls += 1; return Promise.resolve(); },
        getAll: () => Promise.resolve([]), remove: () => Promise.resolve(),
        close: () => { closeCalls += 1; },
      };
    """)
    load_js_module(context, "./upload-coordinator.js", read_web("js/upload-coordinator.js"))
    context.eval(r"""
      globalThis.coordinator = __modules['./upload-coordinator.js'].createUploadCoordinator({
        api, persistence, cryptoObject, delay: () => Promise.resolve(), maxActive: 1, chunkSize: 4,
      });
      coordinator.subscribe(() => { notifications += 1; });
      if (stage === 'reconcile') coordinator.start(); else coordinator.enqueueFiles([file]);
    """)
    drain_jobs(context)
    context.eval(r"""
      coordinator.destroy();
      coordinator.destroy();
      globalThis.afterDestroyPersist = persistCalls;
      globalThis.afterDestroyNotifications = notifications;
      if (deferred[stage]) {
        const values = {
          identity: new Uint8Array(32).buffer,
          create: { upload_id: 'destroy-id', confirmed_parts: [] },
          part: { confirmed_parts: [0], confirmed_bytes: 4 },
          complete: { upload_id: 'destroy-id' },
          reconcile: [{ upload_id: 'remote', confirmed_parts: [] }],
        };
        deferred[stage].resolve(values[stage]);
      }
    """)
    drain_jobs(context)
    assert context.eval("closeCalls") == 1
    assert context.eval("persistCalls") == context.eval("afterDestroyPersist")
    assert context.eval("notifications") == context.eval("afterDestroyNotifications")
    assert context.eval("coordinator.getSnapshot().some(task => task.uploadId === 'remote')") is False


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
                  "navigator.clipboard.writeText", "timeline.ensureMessageLoaded", "onLocate", "/api/storage"):
        assert token in source


def test_reconnect_and_responsive_contracts() -> None:
    api, app, config, css, html = read_web("js/api.js"), read_web("js/app.js"), read_web("js/config.js"), read_web("styles.css"), read_web("index.html")
    assert "RECONNECT_DELAYS" in api and "from './config.js'" in api
    assert "RECONNECT_DELAYS = [1_000, 2_000, 4_000, 8_000, 16_000, 30_000]" in config
    assert "transfer-last-sequence" in api and "event.sequence" in api
    assert "reconnecting" in api and "after=" in api
    assert "event.event_type === 'ready'" in app and "return true" in app
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
      container.listeners.click[0]({ target: container.querySelector('.timeline-delete-btn') });
    """)
    drain_jobs(context)
    context.eval(r"""
      const deleteContainer = document.getElementById('timelineDeleteContainer');
      deleteContainer.listeners.click[0]({ target: deleteContainer.querySelector('.timeline-undo-btn') });
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
      const appTimelineContainer = document.getElementById('timelineContainer');
      appTimelineContainer.listeners.click[0]({ target: appTimelineContainer.querySelector('.timeline-delete-btn') });
    """)
    drain_jobs(context)
    context.eval(r"""
      const appTimelineResultContainer = document.getElementById('timelineContainer');
      appTimelineResultContainer.listeners.click[0]({ target: appTimelineResultContainer.querySelector('.timeline-undo-btn') });
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
        disconnect() {}
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
        focusedElement: document.activeElement?.dataset.messageId || null,
        focusedTabIndex: document.activeElement?.getAttribute('tabindex'),
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
    assert result["focusedElement"] == oldest_id
    assert result["focusedTabIndex"] == "-1"


def test_timeline_focus_message_respects_reduced_motion_and_keeps_focus_state() -> None:
    context = create_js_context()
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    result = json.loads(context.eval(r"""
      const container = document.getElementById('timelineContainer');
      const timeline = __modules['./timeline.js'].createTimeline({
        container,
        newMessageButton: document.getElementById('newMessageButton'),
        api: () => Promise.resolve({ items: [], next_before: null }),
        onRestore: null,
      });
      timeline.upsert({
        id: 'motion-message',
        body: 'motion',
        created_at: '2026-07-19T00:00:00Z',
      });
      const target = container.querySelector('[data-message-id="motion-message"]');
      globalThis.__scrollIntoViewOptions = [];

      window.matchMedia = () => ({ matches: false });
      timeline.focusMessage('motion-message');
      const regular = __scrollIntoViewOptions[__scrollIntoViewOptions.length - 1].behavior;

      window.matchMedia = () => ({ matches: true });
      timeline.focusMessage('motion-message');
      const reduced = __scrollIntoViewOptions[__scrollIntoViewOptions.length - 1].behavior;

      delete window.matchMedia;
      timeline.focusMessage('motion-message');
      const unavailable = __scrollIntoViewOptions[__scrollIntoViewOptions.length - 1].behavior;

      JSON.stringify({
        regular,
        reduced,
        unavailable,
        focused: document.activeElement === target,
        tabIndex: target.getAttribute('tabindex'),
        highlighted: target.classList.contains('timeline-message-highlight'),
      });
    """))

    assert result == {
        "regular": "smooth",
        "reduced": "auto",
        "unavailable": "smooth",
        "focused": True,
        "tabIndex": "-1",
        "highlighted": True,
    }


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


def test_upload_abort_rejects_abort_error_and_composer_delegates_batch() -> None:
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
      globalThis.delegatedFiles = [];
      __modules['./composer.js'].createComposer({
        form: document.getElementById('composerForm'),
        textarea: document.getElementById('composerTextarea'),
        fileInput,
        dropTarget: document.getElementById('composerDropTarget'),
        api: null,
        timeline: null,
        uploadCoordinator: { enqueueFiles(files) { delegatedFiles.push(...files); } },
      });
      fileInput.listeners.change[0]({
        target: { files: [{ name: 'one.txt', size: 1 }, { name: 'two.txt', size: 1 }], value: '' },
      });
    """)
    drain_jobs(context)
    result = json.loads(context.eval("JSON.stringify({ abortName, delegatedCount: delegatedFiles.length })"))

    assert result == {"abortName": "AbortError", "delegatedCount": 2}


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


@pytest.mark.parametrize("failure_mode", ["false", "throw"])
def test_connect_events_replays_failed_generation_from_last_successful_cursor(
    failure_mode: str,
) -> None:
    context = create_js_context()
    set_json(context, "failureMode", failure_mode)
    context.eval(r"""
      globalThis.webSockets = [];
      globalThis.reconnectCallback = null;
      window.setTimeout = callback => { reconnectCallback = callback; return 1; };
      globalThis.WebSocket = class WebSocket {
        constructor(url) { this.url = url; this.closed = false; webSockets.push(this); }
        close() { this.closed = true; }
      };
      localStorage.setItem('transfer-last-sequence', '999');
      sessionStorage.setItem('transfer-last-sequence', '3');
    """)
    load_js_module(context, "./api.js", read_web("js/api.js"))
    context.eval(r"""
      globalThis.appliedSequences = [];
      globalThis.failedOnce = false;
      globalThis.connection = __modules['./api.js'].connectEvents({
        after: () => __modules['./api.js'].getLastSequence(),
        onEvent: event => {
          appliedSequences.push(event.sequence);
          if (event.sequence === 5 && !failedOnce) {
            failedOnce = true;
            if (failureMode === 'throw') throw new Error('projection failed');
            return false;
          }
          return true;
        },
        onStatus: () => {},
      });
      webSockets[0].onmessage({ data: JSON.stringify({ sequence: 4, event_type: 'message.created' }) });
      webSockets[0].onmessage({ data: JSON.stringify({ sequence: 5, event_type: 'message.created' }) });
    """)
    drain_jobs(context)
    context.eval(r"""
      globalThis.sequenceAfterFailure = sessionStorage.getItem('transfer-last-sequence');
      webSockets[0].onmessage({ data: JSON.stringify({ sequence: 6, event_type: 'message.created' }) });
      globalThis.firstSocketClosed = webSockets[0].closed;
      globalThis.attemptsBeforeReplay = appliedSequences.slice();
      webSockets[0].onclose({ code: 1006 });
      reconnectCallback();
      webSockets[1].onmessage({ data: JSON.stringify({ sequence: 5, event_type: 'message.created' }) });
    """)
    drain_jobs(context)
    context.eval(r"""
      globalThis.sequenceAfterReplay = sessionStorage.getItem('transfer-last-sequence');
    """)
    result = json.loads(context.eval(r"""
      JSON.stringify({
        initialUrl: webSockets[0].url,
        reconnectUrl: webSockets[1].url,
        sequenceAfterFailure,
        sequenceAfterReplay,
        legacySequence: localStorage.getItem('transfer-last-sequence'),
        firstSocketClosed,
        attemptsBeforeReplay,
        appliedSequences,
      });
    """))

    assert result == {
        "initialUrl": "ws://testserver/api/events?after=3",
        "reconnectUrl": "ws://testserver/api/events?after=4",
        "sequenceAfterFailure": "4",
        "sequenceAfterReplay": "5",
        "legacySequence": "999",
        "firstSocketClosed": True,
        "attemptsBeforeReplay": [4, 5],
        "appliedSequences": [4, 5, 5],
    }


def test_connect_events_serializes_async_events_and_resync_cursor() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.webSockets = [];
      globalThis.resolvers = [];
      globalThis.applied = [];
      globalThis.WebSocket = class WebSocket {
        constructor(url) { this.url = url; this.closed = false; webSockets.push(this); }
        close() { this.closed = true; }
      };
      sessionStorage.setItem('transfer-last-sequence', '2');
    """)
    load_js_module(context, "./api.js", read_web("js/api.js"))
    context.eval(r"""
      __modules['./api.js'].connectEvents({
        after: () => 2,
        onEvent: event => new Promise(resolve => {
          applied.push(`start-${event.event_type}`);
          resolvers.push(() => { applied.push(`end-${event.event_type}`); resolve(true); });
        }),
        onStatus: () => {},
      });
      webSockets[0].onmessage({ data: JSON.stringify({ event_type: 'resync_required', sequence: 7 }) });
      webSockets[0].onmessage({ data: JSON.stringify({ event_type: 'message.created', sequence: 8 }) });
    """)
    drain_jobs(context)
    assert json.loads(context.eval("JSON.stringify(applied)")) == ["start-resync_required"]
    assert context.eval("sessionStorage.getItem('transfer-last-sequence')") == "2"

    context.eval("resolvers.shift()();")
    drain_jobs(context)
    assert json.loads(context.eval("JSON.stringify(applied)")) == [
        "start-resync_required", "end-resync_required", "start-message.created"
    ]
    assert context.eval("sessionStorage.getItem('transfer-last-sequence')") == "7"

    context.eval("resolvers.shift()();")
    drain_jobs(context)
    assert context.eval("sessionStorage.getItem('transfer-last-sequence')") == "8"


def test_connect_events_retries_resync_failure_from_old_cursor() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.webSockets = [];
      globalThis.reconnectCallback = null;
      window.setTimeout = callback => { reconnectCallback = callback; return 1; };
      globalThis.WebSocket = class WebSocket {
        constructor(url) { this.url = url; this.closed = false; webSockets.push(this); }
        close() { this.closed = true; }
      };
      sessionStorage.setItem('transfer-last-sequence', '2');
    """)
    load_js_module(context, "./api.js", read_web("js/api.js"))
    context.eval(r"""
      __modules['./api.js'].connectEvents({
        after: () => 2,
        onEvent: () => Promise.reject(new Error('snapshot failed')),
        onStatus: () => {},
      });
      webSockets[0].onmessage({ data: JSON.stringify({ event_type: 'resync_required', sequence: 7 }) });
    """)
    drain_jobs(context)
    context.eval("webSockets[0].onclose({ code: 1006 }); reconnectCallback();")

    assert context.eval("webSockets[0].closed") is True
    assert context.eval("webSockets[1].url") == "ws://testserver/api/events?after=2"
    assert context.eval("sessionStorage.getItem('transfer-last-sequence')") == "2"


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
      globalThis.activeTimersAfterDuplicateClose = activeTimerIds.slice();
      runTimer(2);
      webSockets[normalSocketIndex].onclose({ code: 1006 });
      globalThis.activeTimersAfterStaleClose = activeTimerIds.slice();
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
              activeTimersAfterDuplicateClose,
              activeTimersAfterStaleClose,
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
        "clearedTimerIds": [1],
        "webSocketCountAfterStaleTimer": 1,
        "statusesAfterStaleTimer": ["connecting", "reconnecting"],
        "activeTimersAfterDuplicateClose": [2],
        "activeTimersAfterStaleClose": [],
        "reconnectStatuses": [
            "connecting",
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


def test_timeline_message_first_suppresses_later_terminal_upload_snapshot() -> None:
    context = create_js_context()
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    result = json.loads(context.eval(r"""
      const container = document.getElementById('messageFirstTimeline');
      const timeline = __modules['./timeline.js'].createTimeline({
        container,
        newMessageButton: null,
        api: () => Promise.resolve({ items: [], next_before: null }),
        onRestore: null,
      });
      timeline.upsert({
        id: 'message-first',
        upload_id: 'upload-first',
        client_request_id: 'request-first',
        created_at: '2026-07-19T10:00:00Z',
        file: { id: 'file-first', name: 'first.txt' },
      });
      timeline.upsertUpload({
        uploadId: 'upload-first',
        clientRequestId: 'request-first',
        createdAt: '2026-07-19T09:59:59Z',
        name: 'first.txt',
        status: 'completed',
      });
      JSON.stringify({
        messages: container.querySelectorAll('[data-message-id="message-first"]').length,
        uploads: container.querySelectorAll('[data-upload-id="upload-first"]').length,
        projectedItems: container.querySelectorAll('.timeline-message').length,
      });
    """))

    assert result == {"messages": 1, "uploads": 0, "projectedItems": 1}


def test_timeline_initial_and_older_pages_share_deduped_sorting_path() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.timelinePages = [
        {
          items: [
            { id: 'message-c', body: 'c', created_at: '2026-07-20T08:00:00Z' },
            { id: 'message-b', body: 'b', created_at: '2026-07-19T08:00:00Z' },
          ],
          next_before: 'older-page',
        },
        {
          items: [
            { id: 'message-b', body: 'b updated', created_at: '2026-07-19T08:00:00Z' },
            { id: 'message-a', body: 'a', created_at: '2026-07-19T07:00:00Z' },
          ],
          next_before: null,
        },
      ];
    """)
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      const container = document.getElementById('pagedTimeline');
      globalThis.pagedTimeline = __modules['./timeline.js'].createTimeline({
        container,
        newMessageButton: null,
        api: () => Promise.resolve(timelinePages.shift()),
        onRestore: null,
      });
      pagedTimeline.loadInitial();
    """)
    drain_jobs(context)
    context.eval("pagedTimeline.loadOlder();")
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      const resultContainer = document.getElementById('pagedTimeline');
      JSON.stringify({
        ids: resultContainer.querySelectorAll('.timeline-message').map(
          element => element.dataset.messageId || element.dataset.uploadId
        ),
        dates: resultContainer.querySelectorAll('.timeline-date-separator').map(
          element => element.dataset.date
        ),
        duplicateCount: resultContainer.querySelectorAll('[data-message-id="message-b"]').length,
      });
    """))

    assert result == {
        "ids": ["message-a", "message-b", "message-c"],
        "dates": ["2026-07-19", "2026-07-20"],
        "duplicateCount": 1,
    }


def test_composer_destroy_removes_every_input_handler_and_allows_clean_reinit() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.__modules['./api.js'] = {
        sendText: () => Promise.resolve({ id: 'sent-message', created_at: '2026-07-20T00:00:00Z' }),
      };
      globalThis.enqueueCalls = 0;
      globalThis.upsertCalls = 0;
    """)
    load_js_module(context, "./composer.js", read_web("js/composer.js"))
    result = json.loads(context.eval(r"""
      const form = document.getElementById('destroyComposerForm');
      const textarea = document.getElementById('destroyComposerTextarea');
      const fileInput = document.getElementById('destroyComposerInput');
      const dropTarget = document.getElementById('destroyComposerDrop');
      const options = {
        form,
        textarea,
        fileInput,
        dropTarget,
        api: null,
        timeline: { upsert() { upsertCalls += 1; } },
        uploadCoordinator: { enqueueFiles() { enqueueCalls += 1; } },
      };
      const first = __modules['./composer.js'].createComposer(options);
      first.destroy();
      first.destroy();
      const afterDestroy = {
        keydown: (textarea.listeners.keydown || []).length,
        submit: (form.listeners.submit || []).length,
        change: (fileInput.listeners.change || []).length,
        dragenter: (dropTarget.listeners.dragenter || []).length,
        dragover: (dropTarget.listeners.dragover || []).length,
        dragleave: (dropTarget.listeners.dragleave || []).length,
        drop: (dropTarget.listeners.drop || []).length,
        paste: (document.listeners.paste || []).length,
        dragoverClass: dropTarget.classList.contains('dragover'),
      };
      const second = __modules['./composer.js'].createComposer(options);
      textarea.value = 'once';
      form.listeners.submit[0]({ preventDefault() {} });
      second.destroy();
      JSON.stringify({ afterDestroy, remainingSubmit: (form.listeners.submit || []).length });
    """))
    drain_jobs(context)

    assert result == {
        "afterDestroy": {
            "keydown": 0,
            "submit": 0,
            "change": 0,
            "dragenter": 0,
            "dragover": 0,
            "dragleave": 0,
            "drop": 0,
            "paste": 0,
            "dragoverClass": False,
        },
        "remainingSubmit": 0,
    }
    assert context.eval("upsertCalls") == 0


def test_timeline_destroy_disconnects_observer_listeners_and_timers() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.observerDisconnects = 0;
      globalThis.clearedTimelineTimers = [];
      globalThis.nextTimelineTimer = 0;
      globalThis.IntersectionObserver = class IntersectionObserver {
        observe() {}
        disconnect() { observerDisconnects += 1; }
      };
      globalThis.setTimeout = window.setTimeout = () => ++nextTimelineTimer;
      globalThis.clearTimeout = window.clearTimeout = id => clearedTimelineTimers.push(id);
    """)
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    result = json.loads(context.eval(r"""
      const container = document.getElementById('destroyTimeline');
      const button = document.getElementById('destroyTimelineButton');
      const timeline = __modules['./timeline.js'].createTimeline({
        container,
        newMessageButton: button,
        api: () => Promise.resolve({ items: [], next_before: null }),
        onRestore: null,
      });
      timeline.upsert({ id: 'destroy-message', body: 'destroy', created_at: '2026-07-20T00:00:00Z' });
      timeline.focusMessage('destroy-message');
      timeline.destroy();
      timeline.destroy();
      JSON.stringify({
        scrollListeners: (container.listeners.scroll || []).length,
        clickListeners: (button.listeners.click || []).length,
        observerDisconnects,
        clearedTimelineTimers,
        eventApplied: timeline.mergeEvent({
          sequence: 1,
          event_type: 'message.created',
          entity_id: 'after-destroy',
          payload: { created_at: '2026-07-20T00:00:01Z' },
        }),
      });
    """))

    assert result == {
        "scrollListeners": 0,
        "clickListeners": 0,
        "observerDisconnects": 1,
        "clearedTimelineTimers": [1],
        "eventApplied": False,
    }


def test_app_reinitialization_tears_down_previous_modules_and_submit_handler() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.submitCalls = 0;
      globalThis.destroyCounts = { composer: 0, timeline: 0, coordinator: 0, library: 0, navigation: 0, unsubscribe: 0 };
      globalThis.__modules['./api.js'] = {
        request: () => Promise.resolve({}),
        unlock: () => Promise.resolve({}),
        logout: () => Promise.resolve({}),
        getSession: () => Promise.reject(new Error('locked')),
        ApiError: class ApiError extends Error {},
        connectEvents: () => ({ close() {} }),
        getLastSequence: () => 0,
      };
      globalThis.__modules['./timeline.js'] = { createTimeline: () => ({
        loadInitial: () => Promise.resolve(), upsertUpload() {}, mergeEvent() {}, focusMessage() {},
        destroy() { destroyCounts.timeline += 1; },
      }) };
      globalThis.__modules['./composer.js'] = { createComposer: ({ form }) => {
        const onSubmit = event => { event.preventDefault(); submitCalls += 1; };
        form.addEventListener('submit', onSubmit);
        return { destroy() { form.removeEventListener('submit', onSubmit); destroyCounts.composer += 1; } };
      } };
      globalThis.__modules['./upload-coordinator.js'] = { createUploadCoordinator: () => ({
        start: () => Promise.resolve(), reconcile: () => Promise.resolve(), getSnapshot: () => [],
        subscribe() { return () => { destroyCounts.unsubscribe += 1; }; },
        pauseAll() {}, resumeAll() {}, cancelAll() {}, applyRemoteEvent() {},
        destroy() { destroyCounts.coordinator += 1; },
      }) };
      globalThis.__modules['./upload-persistence.js'] = { createUploadPersistence: () => ({}) };
      globalThis.__modules['./library.js'] = { createLibrary: () => ({
        load: () => Promise.resolve(), clearSelection() {}, applyEvent() {},
        destroy() { destroyCounts.library += 1; },
      }) };
      globalThis.__modules['./navigation.js'] = { createNavigation: () => ({
        start() {}, navigate: () => Promise.resolve(),
        destroy() { destroyCounts.navigation += 1; },
      }) };
    """)
    app_source = read_web("js/app.js")
    load_js_module(context, "./app.js", app_source)
    drain_jobs(context)
    load_js_module(context, "./app.js", app_source)
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      const form = document.getElementById('composerForm');
      form.listeners.submit[0]({ preventDefault() {} });
      window.dispatchEvent(new CustomEvent('pagehide'));
      window.dispatchEvent(new CustomEvent('beforeunload'));
      JSON.stringify({
        submitCalls,
        submitListeners: (form.listeners.submit || []).length,
        destroyCounts,
      });
    """))

    assert result == {
        "submitCalls": 1,
        "submitListeners": 0,
        "destroyCounts": {
            "composer": 2,
            "timeline": 2,
            "coordinator": 2,
            "library": 2,
            "navigation": 2,
            "unsubscribe": 2,
        },
    }


def test_app_bfcache_pagehide_preserves_instance_and_pageshow_resumes() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.lifecycle = {
        sessions: 0, timelineLoads: 0, starts: 0, reconciles: 0,
        connections: 0, closes: 0, destroys: 0,
      };
      globalThis.__modules['./api.js'] = {
        request: path => Promise.resolve(path === '/api/health' ? {} : { events: [] }),
        unlock: () => Promise.resolve({}),
        logout: () => Promise.resolve({}),
        getSession: () => { lifecycle.sessions += 1; return Promise.resolve({}); },
        ApiError: class ApiError extends Error {},
        connectEvents: () => {
          lifecycle.connections += 1;
          return { close() { lifecycle.closes += 1; } };
        },
        getLastSequence: () => 0,
      };
      globalThis.__modules['./timeline.js'] = { createTimeline: () => ({
        loadInitial: () => { lifecycle.timelineLoads += 1; return Promise.resolve(); },
        upsertUpload() {}, mergeEvent() {}, focusMessage() {},
        destroy() { lifecycle.destroys += 1; },
      }) };
      globalThis.__modules['./composer.js'] = { createComposer: () => ({
        destroy() { lifecycle.destroys += 1; },
      }) };
      globalThis.__modules['./upload-coordinator.js'] = { createUploadCoordinator: () => ({
        start: () => { lifecycle.starts += 1; return Promise.resolve(); },
        reconcile: () => { lifecycle.reconciles += 1; return Promise.resolve(); },
        getSnapshot: () => [], subscribe: () => () => {},
        pauseAll() {}, resumeAll() {}, cancelAll() {}, applyRemoteEvent() {},
        destroy() { lifecycle.destroys += 1; },
      }) };
      globalThis.__modules['./upload-persistence.js'] = { createUploadPersistence: () => ({}) };
      globalThis.__modules['./library.js'] = { createLibrary: () => ({
        load: () => Promise.resolve(), clearSelection() {}, applyEvent() {},
        destroy() { lifecycle.destroys += 1; },
      }) };
      globalThis.__modules['./navigation.js'] = { createNavigation: () => ({
        start() {}, navigate: () => Promise.resolve(),
        destroy() { lifecycle.destroys += 1; },
      }) };
    """)
    load_js_module(context, "./app.js", read_web("js/app.js"))
    drain_jobs(context)
    context.eval(r"""
      window.dispatchEvent({ type: 'pagehide', persisted: true });
      globalThis.afterPersistedHide = JSON.stringify(lifecycle);
      window.dispatchEvent({ type: 'pageshow', persisted: true });
    """)
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      const afterResume = { ...lifecycle };
      const listenerCounts = {
        beforeunload: (window.listeners.beforeunload || []).length,
        pagehide: (window.listeners.pagehide || []).length,
        pageshow: (window.listeners.pageshow || []).length,
      };
      window.dispatchEvent({ type: 'beforeunload' });
      const afterBeforeUnload = { ...lifecycle };
      window.dispatchEvent({ type: 'pagehide', persisted: false });
      window.dispatchEvent({ type: 'pageshow', persisted: true });
      JSON.stringify({
        afterPersistedHide: JSON.parse(afterPersistedHide),
        afterResume,
        afterBeforeUnload,
        afterFinalHide: lifecycle,
        listenerCounts,
      });
    """))
    drain_jobs(context)

    assert result["afterPersistedHide"]["destroys"] == 0
    assert result["afterResume"] == {
        "sessions": 2,
        "timelineLoads": 2,
        "starts": 1,
        "reconciles": 1,
        "connections": 2,
        "closes": 1,
        "destroys": 0,
    }
    assert result["listenerCounts"] == {
        "beforeunload": 0,
        "pagehide": 1,
        "pageshow": 1,
    }
    assert result["afterBeforeUnload"]["destroys"] == 0
    assert result["afterFinalHide"]["destroys"] == 5
    assert context.eval("lifecycle.sessions") == 2


def test_timeline_destroy_disables_all_existing_delegated_action_buttons() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.restCalls = [];
      globalThis.uploadActionCalls = 0;
      globalThis.clipboardCalls = 0;
      navigator.clipboard.writeText = () => { clipboardCalls += 1; return Promise.resolve(); };
    """)
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      const container = document.getElementById('delegatedTimeline');
      globalThis.delegatedTimeline = __modules['./timeline.js'].createTimeline({
        container,
        newMessageButton: null,
        api: (path, options = {}) => {
          restCalls.push({ path, method: options.method || 'GET' });
          return Promise.resolve({
            id: 'delegated-message', body: 'delegated',
            created_at: '2026-07-20T00:00:00Z', deleted_at: '2026-07-20T00:00:00Z',
          });
        },
        onRestore: null,
        onUploadAction: () => { uploadActionCalls += 1; },
      });
      delegatedTimeline.upsert({
        id: 'delegated-message', body: 'delegated', created_at: '2026-07-20T00:00:00Z',
      });
      delegatedTimeline.upsertUpload({
        uploadId: 'delegated-upload', name: 'upload.txt', status: 'queued',
        createdAt: '2026-07-20T00:00:01Z', isSourceDevice: true,
      });
      globalThis.delegatedHandler = container.listeners.click[0];
      globalThis.deleteButton = container.querySelector('.timeline-delete-btn');
      delegatedHandler({ target: deleteButton });
    """)
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      const resultContainer = document.getElementById('delegatedTimeline');
      const undoButton = resultContainer.querySelector('.timeline-undo-btn');
      const uploadButton = resultContainer.querySelector('[data-upload-action="pause"]');
      const copyButton = deleteButton.parentNode.querySelector('.timeline-copy-btn');
      delegatedTimeline.destroy();
      delegatedTimeline.destroy();
      [copyButton, deleteButton, uploadButton, undoButton].forEach(target => {
        delegatedHandler({ target });
      });
      JSON.stringify({
        restCalls,
        uploadActionCalls,
        clipboardCalls,
        containerClickListeners: (resultContainer.listeners.click || []).length,
        buttonListenerCounts: [copyButton, deleteButton, uploadButton, undoButton]
          .map(button => (button.listeners.click || []).length),
      });
    """))
    drain_jobs(context)

    assert result == {
        "restCalls": [
            {"path": "/api/messages/delegated-message", "method": "DELETE"}
        ],
        "uploadActionCalls": 0,
        "clipboardCalls": 0,
        "containerClickListeners": 0,
        "buttonListenerCounts": [0, 0, 0, 0],
    }


def test_timeline_older_page_preserves_first_visible_stable_anchor() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.__rects = {
        anchorTimeline: { top: 0, bottom: 100, left: 0, right: 100, width: 100, height: 100 },
        'anchor-message': { top: 10, bottom: 40, left: 0, right: 100, width: 100, height: 30 },
        'later-message': { top: 50, bottom: 80, left: 0, right: 100, width: 100, height: 30 },
      };
    """)
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      const container = document.getElementById('anchorTimeline');
      container.scrollTop = 100;
      container.scrollHeight = 300;
      globalThis.anchorTimeline = __modules['./timeline.js'].createTimeline({
        container,
        newMessageButton: null,
        api: () => {
          __rects['anchor-message'] = { top: 55, bottom: 105, left: 0, right: 100, width: 100, height: 50 };
          container.scrollHeight = 900;
          return Promise.resolve({
            items: [
              { id: 'anchor-message', body: 'taller duplicate', created_at: '2026-07-20T01:00:00Z' },
              { id: 'older-message', body: 'older', created_at: '2026-07-19T01:00:00Z' },
            ],
            next_before: null,
          });
        },
        onRestore: null,
      });
      anchorTimeline.upsert({ id: 'anchor-message', body: 'anchor', created_at: '2026-07-20T01:00:00Z' });
      anchorTimeline.upsert({ id: 'later-message', body: 'later', created_at: '2026-07-20T02:00:00Z' });
      anchorTimeline.loadOlder();
    """)
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      const resultContainer = document.getElementById('anchorTimeline');
      JSON.stringify({
        scrollTop: resultContainer.scrollTop,
        ids: resultContainer.querySelectorAll('.timeline-message').map(
          element => element.dataset.messageId || element.dataset.uploadId
        ),
        duplicateCount: resultContainer.querySelectorAll('[data-message-id="anchor-message"]').length,
      });
    """))

    assert result == {
        "scrollTop": 145,
        "ids": ["older-message", "anchor-message", "later-message"],
        "duplicateCount": 1,
    }


def test_timeline_repeated_initial_load_observes_only_latest_sentinel() -> None:
    context = create_js_context()
    context.eval(r"""
      globalThis.observerInstances = [];
      globalThis.IntersectionObserver = class IntersectionObserver {
        constructor(callback) { this.callback = callback; this.targets = []; this.disconnects = 0; observerInstances.push(this); }
        observe(target) { this.targets.push(target); }
        disconnect() { this.disconnects += 1; this.targets = []; }
      };
    """)
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      const container = document.getElementById('sentinelTimeline');
      globalThis.sentinelTimeline = __modules['./timeline.js'].createTimeline({
        container,
        newMessageButton: null,
        api: () => Promise.resolve({ items: [], next_before: null }),
        onRestore: null,
      });
      sentinelTimeline.loadInitial();
    """)
    drain_jobs(context)
    context.eval("sentinelTimeline.loadInitial();")
    drain_jobs(context)
    result = json.loads(context.eval(r"""
      const observer = observerInstances[0];
      const resultContainer = document.getElementById('sentinelTimeline');
      JSON.stringify({
        observerCount: observerInstances.length,
        disconnects: observer.disconnects,
        activeTargets: observer.targets.length,
        activeIsCurrent: observer.targets[0] === resultContainer.querySelector('.timeline-sentinel'),
        sentinelCount: resultContainer.querySelectorAll('.timeline-sentinel').length,
      });
    """))

    assert result == {
        "observerCount": 1,
        "disconnects": 2,
        "activeTargets": 1,
        "activeIsCurrent": True,
        "sentinelCount": 1,
    }


def test_timeline_initial_load_discards_older_pending_generation() -> None:
    context = create_js_context()
    load_js_module(context, "./timeline.js", read_web("js/timeline.js"))
    context.eval(r"""
      globalThis.pendingPages = [];
      globalThis.pagePaths = [];
      const container = document.getElementById('generationTimeline');
      globalThis.generationTimeline = __modules['./timeline.js'].createTimeline({
        container,
        newMessageButton: null,
        api: path => {
          pagePaths.push(path);
          return new Promise(resolve => pendingPages.push({ path, resolve }));
        },
        onRestore: null,
      });
      globalThis.olderResult = null;
      generationTimeline.loadOlder().then(result => { olderResult = result; });
      generationTimeline.loadInitial();
    """)
    assert context.eval("pendingPages.length") == 2

    context.eval(r"""
      pendingPages[1].resolve({
        items: [{ id: 'fresh-message', body: 'fresh', created_at: '2026-07-20T02:00:00Z' }],
        next_before: 'fresh-cursor',
      });
    """)
    drain_jobs(context)
    context.eval("generationTimeline.loadOlder();")
    assert context.eval("pendingPages.length") == 3
    context.eval(r"""
      pendingPages[0].resolve({
        items: [{ id: 'stale-message', body: 'stale', created_at: '2026-07-19T02:00:00Z' }],
        next_before: null,
      });
    """)
    drain_jobs(context)
    context.eval("generationTimeline.loadOlder();")

    result = json.loads(context.eval(r"""
      const resultContainer = document.getElementById('generationTimeline');
      JSON.stringify({
        paths: pagePaths,
        ids: resultContainer.querySelectorAll('.timeline-message')
          .map(element => element.dataset.messageId),
        olderResult,
        pendingCount: pendingPages.length,
      });
    """))

    assert result == {
        "paths": [
            "/api/messages?limit=50",
            "/api/messages?limit=50",
            "/api/messages?limit=50&before=fresh-cursor",
        ],
        "ids": ["fresh-message"],
        "olderResult": False,
        "pendingCount": 3,
    }
