# Navigation And Workspace Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the five-button panel mapping with three hash-routed destinations and make the transfer timeline the primary responsive workspace.

**Architecture:** Add a focused `navigation.js` controller that owns hash normalization, route metadata, active page state, focus, and scroll restoration. Recompose existing composer, timeline, library, connection, and storage elements into Transfer, Files, and Manage route pages while preserving all API and module contracts.

**Tech Stack:** HTML5, CSS custom properties, native ES Modules, QuickJS frontend tests, Playwright Chromium E2E, pytest.

## Global Constraints

- Preserve FastAPI routes, session protocol, WebSocket event model, upload/download behavior, soft-delete window, and persistence model.
- Use exactly three routes: `#transfer`, `#files`, and `#manage`.
- Keep desktop sidebar and mobile bottom navigation semantically identical.
- Preserve unsent composer text, upload queue, timeline position, file filters, and file view state across route changes.
- Keep runtime dependencies unchanged.
- Maintain CSP compliance: no inline script/style and no `element.style` assignments.
- Maintain 44px minimum touch targets and `prefers-reduced-motion` support.
- Use TDD and commit each independently testable task.

---

### Task 1: Hash Navigation Controller

**Files:**
- Create: `web/js/navigation.js`
- Modify: `tests/test_frontend_contract.py`

**Interfaces:**
- Produces: `ROUTES`, `normalizeRoute(hash)`, and `createNavigation(options)`.
- `createNavigation(options)` returns `{ start(), navigate(route), getCurrentRoute(), destroy() }`.
- Consumes DOM nodes carrying `data-route`, `data-route-page`, `data-route-title`, and `data-route-heading`.

- [ ] **Step 1: Write failing normalization and module contract tests**

Add tests that load `navigation.js` in QuickJS and assert exact normalization:

```python
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
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k navigation`

Expected: FAIL because `web/js/navigation.js` does not exist.

- [ ] **Step 3: Implement the route metadata and controller**

Create `navigation.js` with this public shape:

```javascript
export const ROUTES = Object.freeze({
  transfer: { hash: '#transfer', label: '传输', title: '传输工作台' },
  files: { hash: '#files', label: '文件', title: '全部文件' },
  manage: { hash: '#manage', label: '管理', title: '管理与设置' },
});

export function normalizeRoute(hash) {
  const route = String(hash || '').replace(/^#/, '');
  return Object.prototype.hasOwnProperty.call(ROUTES, route) ? route : 'transfer';
}

export function createNavigation({ windowObject, documentObject, onRouteChange = () => {} }) {
  let currentRoute = null;
  const scrollPositions = new Map();
  let focusNextRoute = false;

  function applyRoute(route) {
    const normalized = normalizeRoute(`#${route}`);
    if (currentRoute) scrollPositions.set(currentRoute, windowObject.scrollY || 0);
    documentObject.querySelectorAll('[data-route-page]').forEach(page => {
      page.hidden = page.dataset.routePage !== normalized;
    });
    documentObject.querySelectorAll('[data-route]').forEach(button => {
      const active = button.dataset.route === normalized;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    const config = ROUTES[normalized];
    const title = documentObject.querySelector('[data-route-title]');
    if (title) title.textContent = config.title;
    documentObject.title = `${config.title} · MonkeyCode`;
    currentRoute = normalized;
    onRouteChange(normalized);
    windowObject.scrollTo(0, scrollPositions.get(normalized) || 0);
    if (focusNextRoute) {
      documentObject.querySelector(`[data-route-heading="${normalized}"]`)?.focus();
      focusNextRoute = false;
    }
  }

  function handleHashChange() {
    applyRoute(normalizeRoute(windowObject.location.hash));
  }

  function navigate(route) {
    const normalized = normalizeRoute(`#${route}`);
    focusNextRoute = true;
    if (windowObject.location.hash === ROUTES[normalized].hash) applyRoute(normalized);
    else windowObject.location.hash = ROUTES[normalized].hash;
  }

  function start() {
    documentObject.querySelectorAll('[data-route]').forEach(button => {
      button.addEventListener('click', () => navigate(button.dataset.route));
    });
    windowObject.addEventListener('hashchange', handleHashChange);
    const route = normalizeRoute(windowObject.location.hash);
    if (windowObject.location.hash !== ROUTES[route].hash) {
      windowObject.history.replaceState(null, '', ROUTES[route].hash);
    }
    applyRoute(route);
  }

  function destroy() {
    windowObject.removeEventListener('hashchange', handleHashChange);
  }

  return { start, navigate, getCurrentRoute: () => currentRoute, destroy };
}
```

- [ ] **Step 4: Run focused tests and verify pass**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k navigation`

Expected: PASS.

- [ ] **Step 5: Commit the controller**

```bash
git add web/js/navigation.js tests/test_frontend_contract.py
git commit -m "feat(nav): add hash navigation controller"
```

---

### Task 2: Three Route Page Structure

**Files:**
- Modify: `web/index.html`
- Modify: `web/js/app.js`
- Modify: `tests/test_frontend_contract.py`

**Interfaces:**
- Consumes: `createNavigation()` from Task 1.
- Produces: `#transferPage`, `#filesPage`, and `#managePage` with route headings.
- Preserves every existing functional element ID used by composer, timeline, and library modules.

- [ ] **Step 1: Write failing markup contract tests**

```python
def test_shell_has_three_matching_desktop_and_mobile_routes() -> None:
    html = read_web("index.html")
    for route in ("transfer", "files", "manage"):
        assert html.count(f'data-route="{route}"') == 2
        assert f'data-route-page="{route}"' in html
        assert f'data-route-heading="{route}"' in html
    assert 'data-section=' not in html
    assert 'data-route="activity"' not in html
    assert 'data-route="devices"' not in html


def test_app_starts_navigation_and_clears_file_selection_on_route_exit() -> None:
    source = read_web("js/app.js")
    assert "from './navigation.js'" in source
    assert "createNavigation" in source
    assert "navigation.start()" in source
    assert "route !== 'files'" in source


def test_skip_link_preserves_application_route_hash() -> None:
    source = read_web("js/app.js")
    handler = source[source.index("if (skipLink && mainContent)"):source.index("async function checkSession")]
    assert "mainContent.focus()" in handler
    assert "location.hash = 'mainContent'" not in handler
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k "three_matching or starts_navigation"`

Expected: FAIL against the five `data-section` buttons.

- [ ] **Step 3: Replace navigation markup and add route containers**

Use three buttons in both navigation variants:

```html
<button class="nav-item active" type="button" data-route="transfer" aria-current="page"><span>传输</span></button>
<button class="nav-item" type="button" data-route="files"><span>文件</span><b class="nav-badge" id="navCount">0</b></button>
<button class="nav-item" type="button" data-route="manage"><span>管理</span></button>
```

Wrap and move existing elements by ID into these exact page containers:

```html
<section class="route-page transfer-page" id="transferPage" data-route-page="transfer">
  <h1 class="route-heading visually-hidden" data-route-heading="transfer" tabindex="-1">传输工作台</h1>
</section>
<section class="route-page files-page" id="filesPage" data-route-page="files" hidden>
  <h1 class="route-heading" data-route-heading="files" tabindex="-1">全部文件</h1>
</section>
<section class="route-page manage-page" id="managePage" data-route-page="manage" hidden>
  <h1 class="route-heading" data-route-heading="manage" tabindex="-1">管理与设置</h1>
</section>
```

Keep `composerPanel` and `timelinePanel` inside `transferPage`, `libraryView` inside `filesPage`, and `connectionPanel` plus `operationsPanel` inside `managePage`.

- [ ] **Step 4: Replace `setupNavigation()` with the controller integration**

Import and start the controller in `app.js`:

```javascript
import { createNavigation } from './navigation.js';

const navigation = createNavigation({
  windowObject: window,
  documentObject: document,
  onRouteChange(route) {
    if (route !== 'files') {
      document.getElementById('clearSelectionBtn')?.click();
    }
  },
});
navigation.start();
```

Remove the five-section `sectionPanels` mapping and `setupNavigation()` implementation.

Keep the skip link focused on `mainContent` without changing the application route:

```javascript
if (skipLink && mainContent) {
  skipLink.addEventListener('click', event => {
    event.preventDefault();
    mainContent.focus();
  });
}
```

- [ ] **Step 5: Run frontend tests**

Run: `python3 -m pytest -q tests/test_frontend_contract.py`

Expected: PASS after updating layout-specific assertions from `data-section` to `data-route`.

- [ ] **Step 6: Commit the route structure**

```bash
git add web/index.html web/js/app.js tests/test_frontend_contract.py
git commit -m "refactor(ui): split shell into three route pages"
```

---

### Task 3: Timeline-First Transfer Workspace

**Files:**
- Modify: `web/index.html`
- Modify: `web/styles.css`
- Modify: `tests/test_frontend_contract.py`

**Interfaces:**
- Consumes unchanged IDs: `timelinePanel`, `timelineContainer`, `composerPanel`, `composerForm`, `composerQueue`, and connection route IDs.
- Produces: `.transfer-workspace`, `.transfer-status`, and `.composer-dock` layout classes.

- [ ] **Step 1: Write failing transfer hierarchy tests**

```python
def test_transfer_page_is_timeline_first_and_composer_is_docked() -> None:
    html = read_web("index.html")
    transfer = html[html.index('id="transferPage"'):html.index('id="filesPage"')]
    assert 'class="transfer-workspace"' in transfer
    assert transfer.index('id="timelinePanel"') < transfer.index('id="composerPanel"')
    assert 'class="panel transfer-panel composer-dock"' in transfer
    assert 'class="transfer-status"' in transfer
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k timeline_first`

Expected: FAIL because composer currently precedes the timeline.

- [ ] **Step 3: Recompose Transfer without changing functional IDs**

Use this structure:

```html
<div class="transfer-workspace">
  <header class="transfer-status" aria-label="传输状态">
    <div><span>当前设备</span><strong id="routeSource">本机</strong></div>
    <div class="route-link"><span>SHA-256</span></div>
    <div><span>目标设备</span><strong id="routeTarget">已连接设备</strong></div>
    <span id="connectionStatus" class="pill" aria-live="polite">未连接</span>
  </header>
  <section class="panel timeline-panel" id="timelinePanel"></section>
  <section class="panel transfer-panel composer-dock" id="composerPanel"></section>
</div>
```

Move the complete existing children of `timelinePanel` and `composerPanel` into their corresponding elements. Remove the duplicate inner `.transfer-route` block after its route fields move into `.transfer-status`.

- [ ] **Step 4: Add timeline-first responsive layout CSS**

```css
.transfer-workspace {
  min-height: calc(100dvh - var(--topbar-height) - 60px);
  display: grid;
  grid-template-rows: auto minmax(320px, 1fr) auto;
  gap: 14px;
}
.transfer-status {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
}
.timeline-panel { min-height: 0; display: flex; flex-direction: column; }
.timeline-container { flex: 1; min-height: 280px; max-height: none; }
.composer-dock { position: sticky; bottom: 16px; z-index: 6; }
```

At `max-width: 720px`, set `bottom: var(--mobile-fixed-offset)` and collapse `.transfer-status` to two columns while keeping the status label visible.

- [ ] **Step 5: Run composer and timeline tests**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k "timeline or composer or transfer"`

Expected: PASS.

- [ ] **Step 6: Commit the Transfer workspace**

```bash
git add web/index.html web/styles.css tests/test_frontend_contract.py
git commit -m "refactor(ui): make timeline the primary transfer workspace"
```

---

### Task 4: Files Empty State And Batch Toolbar Safety

**Files:**
- Modify: `web/index.html`
- Modify: `web/styles.css`
- Modify: `web/js/app.js`
- Modify: `web/js/library.js`
- Modify: `tests/test_frontend_contract.py`

**Interfaces:**
- Consumes: `navigation.navigate('transfer')` and existing library selection events.
- Changes `createLibrary({ root, api, timeline })` to `createLibrary({ root, api, timeline, onAttach })`.
- Produces: `#emptyFilesAction` and safe inactive `.batch-toolbar` behavior.

- [ ] **Step 1: Write failing Files interaction tests**

```python
def test_batch_toolbar_cannot_intercept_pointer_events_while_inactive() -> None:
    css = read_web("styles.css")
    base = css[css.index(".batch-toolbar {"):css.index(".batch-toolbar.visible {")]
    visible = css[css.index(".batch-toolbar.visible {"):css.index(".batch-toolbar .pill")]
    assert "visibility: hidden" in base
    assert "pointer-events: none" in base
    assert "visibility: visible" in visible
    assert "pointer-events: auto" in visible


def test_files_empty_state_has_transfer_action() -> None:
    source = read_web("js/library.js")
    assert 'id="emptyFilesAction"' in source
    assert 'data-file-action="empty-attach"' in source
    assert "onAttach" in source
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k "batch_toolbar_cannot or files_empty_state"`

Expected: FAIL because inactive toolbar still accepts pointer interaction and the action is absent.

- [ ] **Step 3: Make inactive toolbar noninteractive**

```css
.batch-toolbar {
  visibility: hidden;
  pointer-events: none;
  opacity: 0;
  transform: translate(-50%, 120px);
}
.batch-toolbar.visible {
  visibility: visible;
  pointer-events: auto;
  opacity: 1;
  transform: translate(-50%, 0);
}
```

- [ ] **Step 4: Render and wire the Files empty-state action**

Extend the `createLibrary` options by replacing its declaration, then render the action from `renderFiles()`:

```diff
-export function createLibrary({ root, api, timeline }) {
+export function createLibrary({ root, api, timeline, onAttach = () => {} }) {
```

```javascript
fileListEl.innerHTML = `
  <div class="empty">
    <div>
      <strong>还没有文件</strong>
      <p>选择文件后，它会显示在这里。</p>
      <button class="btn btn-primary" id="emptyFilesAction" type="button" data-file-action="empty-attach">选择文件</button>
    </div>
  </div>`;
```

Handle `empty-attach` at the start of the existing delegated `handleFileAction(event)` function:

```javascript
const action = event.target.closest('[data-file-action]');
if (!action) return;
if (action.dataset.fileAction === 'empty-attach') {
  onAttach();
  return;
}
```

Pass the callback from `app.js`. The file input click remains in the same trusted event task:

```javascript
const library = createLibrary({
  root: document,
  api: (url, options) => apiRequest(url, options),
  timeline,
  onAttach() {
    navigation.navigate('transfer');
    composerFileInput?.click();
  },
});
```

- [ ] **Step 5: Run library-focused tests**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k "library or batch or empty"`

Expected: PASS.

- [ ] **Step 6: Commit Files interaction fixes**

```bash
git add web/index.html web/styles.css web/js/app.js web/js/library.js tests/test_frontend_contract.py
git commit -m "fix(ui): make file batch actions safe and contextual"
```

---

### Task 5: Manage Page And Responsive Consolidation

**Files:**
- Modify: `web/index.html`
- Modify: `web/styles.css`
- Modify: `web/js/app.js`
- Modify: `tests/test_frontend_contract.py`

**Interfaces:**
- Consumes existing connection and storage element IDs.
- Produces: `#logoutButton`, `.manage-grid`, `--mobile-nav-height`, and `--mobile-fixed-offset`.

- [ ] **Step 1: Write failing management and responsive tests**

```python
def test_manage_page_groups_connection_storage_appearance_and_session() -> None:
    html = read_web("index.html")
    manage = html[html.index('id="managePage"'):html.index('class="mobile-nav"')]
    for token in ('id="connectionPanel"', 'id="operationsPanel"', 'id="appearancePanel"', 'id="sessionPanel"', 'id="logoutButton"'):
        assert token in manage


def test_mobile_fixed_elements_share_offset_and_430_rule_is_single() -> None:
    css = read_web("styles.css")
    assert "--mobile-nav-height: 66px" in css
    assert "--mobile-fixed-offset:" in css
    assert css.count("@media (max-width: 430px)") == 1
    assert "bottom: var(--mobile-fixed-offset)" in css
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k "manage_page_groups or mobile_fixed_elements"`

Expected: FAIL because appearance/session groups and shared tokens are absent.

- [ ] **Step 3: Add Manage groups and logout behavior**

Add these panels after the existing connection and storage panels:

```html
<section class="panel manage-panel" id="appearancePanel">
  <div class="panel-head"><div><h2>外观</h2><p>切换工作台显示主题</p></div></div>
  <button class="btn btn-soft" id="manageThemeToggle" type="button">切换深色模式</button>
</section>
<section class="panel manage-panel session-panel" id="sessionPanel">
  <div class="panel-head"><div><h2>当前会话</h2><p>退出后需要访问令牌重新解锁</p></div></div>
  <button class="btn btn-danger" id="logoutButton" type="button">退出当前会话</button>
</section>
```

Wire controls in `app.js`:

```javascript
document.getElementById('manageThemeToggle')?.addEventListener('click', () => themeToggle?.click());
document.getElementById('logoutButton')?.addEventListener('click', () => {
  window.dispatchEvent(new CustomEvent('session-logout'));
});
```

- [ ] **Step 4: Consolidate responsive fixed-element tokens**

Add root tokens:

```css
:root {
  --topbar-height: 68px;
  --mobile-nav-height: 66px;
  --mobile-fixed-offset: calc(var(--mobile-nav-height) + env(safe-area-inset-bottom) + 12px);
}
```

Use `var(--topbar-height)` for `.topbar`, `var(--mobile-nav-height)` for `.mobile-nav`, and `var(--mobile-fixed-offset)` for mobile composer, toast, and batch toolbar. Merge both existing 430px media blocks into one and keep `.filter-grid { grid-template-columns: 1fr; }` at that width.

- [ ] **Step 5: Run responsive and session tests**

Run: `python3 -m pytest -q tests/test_frontend_contract.py -k "responsive or session or theme or manage"`

Expected: PASS.

- [ ] **Step 6: Commit Manage and responsive behavior**

```bash
git add web/index.html web/styles.css web/js/app.js tests/test_frontend_contract.py
git commit -m "feat(ui): add consolidated manage destination"
```

---

### Task 6: Browser Navigation And Mobile Regression Coverage

**Files:**
- Modify: `tests/test_browser_e2e.py`
- Modify: `tests/test_frontend_contract.py`

**Interfaces:**
- Consumes completed route markup and navigation controller.
- Produces end-to-end evidence for history, focus, fixed-element safety, and overflow.

- [ ] **Step 1: Add the desktop and mobile route E2E test**

```python
def test_three_route_navigation_history_focus_and_mobile_safety(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    page.set_viewport_size({"width": 390, "height": 844})
    _open_locked_application(browser_session)
    _unlock(page)

    expect(page.locator("#transferPage")).to_be_visible()
    page.locator('.mobile-nav [data-route="files"]').click()
    expect(page.locator("#filesPage")).to_be_visible()
    expect(page.locator('[data-route-heading="files"]')).to_be_focused()
    assert page.evaluate("location.hash") == "#files"

    page.locator('.mobile-nav [data-route="manage"]').click()
    expect(page.locator("#managePage")).to_be_visible()
    expect(page.locator('[data-route-heading="manage"]')).to_be_focused()
    page.go_back()
    expect(page.locator("#filesPage")).to_be_visible()

    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    assert page.locator("#batchToolbar").evaluate(
        "element => getComputedStyle(element).pointerEvents"
    ) == "none"
    _assert_browser_clean(browser_session)
```

- [ ] **Step 2: Add the empty Files action E2E test**

Use Playwright's file chooser expectation to prove the action navigates to Transfer and opens the picker:

```python
def test_empty_files_action_returns_to_transfer_and_opens_picker(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    _open_locked_application(browser_session)
    _unlock(page)
    page.locator('.sidebar [data-route="files"]').click()
    with page.expect_file_chooser():
        page.locator("#emptyFilesAction").click()
    expect(page.locator("#transferPage")).to_be_visible()
    assert page.evaluate("location.hash") == "#transfer"
    _assert_browser_clean(browser_session)
```

Update the existing skip-link E2E assertions to expect the active route hash to remain `#transfer` after activation while `#mainContent` receives focus.

- [ ] **Step 3: Run browser E2E tests and address deterministic regressions**

Run: `python3 -m pytest -q tests/test_browser_e2e.py`

Expected: PASS with no page errors, unexpected console output, CSP violations, pointer interception, or horizontal overflow.

- [ ] **Step 4: Run full verification**

Run:

```bash
python3 -m pytest -q
python3 -m compileall -q app server.py tests
git diff --check
```

Expected: all tests pass, compile check exits 0, and `git diff --check` produces no output.

- [ ] **Step 5: Commit final regression coverage**

```bash
git add tests/test_browser_e2e.py tests/test_frontend_contract.py
git commit -m "test(ui): cover routed workspace navigation"
```

---

## Final Review Checklist

- Confirm every design requirement maps to a task above.
- Confirm the three route names and data attributes are identical across HTML, JavaScript, CSS, and tests.
- Confirm all existing functional IDs remain unique.
- Confirm inactive pages, batch toolbar, drawer, and session overlay have correct accessibility and pointer semantics.
- Confirm desktop 1440px and mobile 390px layouts preserve the transfer timeline and composer without fixed-element overlap.
- Confirm visual acceptance uses model-native image understanding and does not invoke MCP image tools.
