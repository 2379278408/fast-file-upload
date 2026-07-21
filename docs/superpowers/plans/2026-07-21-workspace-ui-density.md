# Workspace UI Density Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebalance the Transfer, Files, and Manage routes so their content is readable and responsive without changing the existing MonkeyCode visual language or application behavior.

**Architecture:** Keep the existing HTML structure, IDs, JavaScript modules, and design tokens as the behavioral foundation. Add a small shared density layer in `web/styles.css`, make only the semantic wrapper changes that CSS cannot express in `web/index.html`, and verify route behavior through source contracts plus real-browser viewport tests.

**Tech Stack:** HTML5, CSS custom properties and Grid/Flexbox, native ES modules, FastAPI static delivery, pytest, QuickJS contract tests, Playwright browser E2E.

## Global Constraints

- Preserve the existing colors, typography, radii, controls, dark theme, and interaction model.
- Keep all existing DOM IDs consumed by JavaScript and tests stable.
- Add no runtime dependency or UI framework.
- Use a shared `8 / 12 / 16 / 24px` spacing scale.
- Keep every interactive target at least 44px.
- Preserve keyboard navigation, focus visibility, live regions, route focus, skip links, and `prefers-reduced-motion` behavior.
- Prevent horizontal document scrolling at 375px and every wider supported viewport.
- Verify 1440x900, 1024x768, 390x844, and 375x667 viewports.
- Keep application, upload, file, session, and storage behavior unchanged.

---

### Task 1: Shared Density And Responsive Foundation

**Files:**
- Modify: `web/styles.css:2-33, 79-95, 218-235, 329-396, 1554-1710`
- Test: `tests/test_frontend_contract.py`

**Interfaces:**
- Consumes: Existing `.app-shell`, `.workspace`, `.topbar`, `.page`, `.panel`, `.panel-head`, `.btn`, `.pill`, and route breakpoint selectors.
- Produces: CSS variables `--space-1`, `--space-2`, `--space-3`, `--space-4`, `--page-gutter`, and `--content-max`; consistent page gutters and panel hierarchy used by Tasks 2-4.

- [ ] **Step 1: Add failing shared-layout contract tests**

Add assertions to `tests/test_frontend_contract.py`:

```python
def test_workspace_density_tokens_and_content_width_contract() -> None:
    css = read_web("styles.css")
    for declaration in (
        "--space-1: 8px",
        "--space-2: 12px",
        "--space-3: 16px",
        "--space-4: 24px",
        "--content-max: 1440px",
    ):
        assert declaration in css
    assert "max-width: var(--content-max)" in css
    assert "padding-inline: var(--page-gutter)" in css


def test_workspace_density_keeps_accessible_targets_and_compact_breakpoints() -> None:
    css = read_web("styles.css")
    assert "min-height: 44px" in css
    assert "@media (max-width: 1024px)" in css
    assert "@media (max-width: 720px)" in css
    assert "@media (max-width: 430px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
```

- [ ] **Step 2: Run the shared-layout tests and verify failure**

Run:

```bash
python3 -m pytest tests/test_frontend_contract.py -q -k "workspace_density_tokens or workspace_density_keeps"
```

Expected: both new tests fail because the shared density variables and 1024px breakpoint are absent.

- [ ] **Step 3: Add the shared CSS density layer**

Extend `:root` and the route container rules in `web/styles.css`:

```css
:root {
    --space-1: 8px;
    --space-2: 12px;
    --space-3: 16px;
    --space-4: 24px;
    --page-gutter: clamp(16px, 2.5vw, 32px);
    --content-max: 1440px;
}

.page {
    width: 100%;
    max-width: var(--content-max);
    margin-inline: auto;
    padding-inline: var(--page-gutter);
}

.panel-head {
    gap: var(--space-3);
    padding: var(--space-3) 20px;
}

@media (max-width: 1024px) {
    :root { --page-gutter: 16px; }
}

@media (max-width: 720px) {
    :root { --page-gutter: 12px; }
}
```

Merge these declarations into existing selectors and media blocks so each property has one authoritative rule at a given breakpoint.

- [ ] **Step 4: Run shared and existing frontend contracts**

Run:

```bash
python3 -m pytest tests/test_frontend_contract.py -q
```

Expected: all frontend contract tests pass.

- [ ] **Step 5: Commit the shared foundation**

```bash
git add web/styles.css tests/test_frontend_contract.py
git commit -m "style(ui): establish responsive density foundation"
```

---

### Task 2: Transfer Route Space And Timeline Hierarchy

**Files:**
- Modify: `web/index.html:93-164`
- Modify: `web/styles.css:237-285, 457-464, 1585-1615, 1675-1686, 1760-1963, 2057-2066`
- Test: `tests/test_frontend_contract.py`
- Test: `tests/test_browser_e2e.py`

**Interfaces:**
- Consumes: Task 1 spacing and page-width variables; existing `#timelineContainer`, `#newMessageButton`, `#composerPanel`, and upload summary IDs.
- Produces: `.transfer-status-strip`, `.timeline-scroll-region`, and safe-area behavior for `.timeline-new-btn`; no JavaScript API changes.

- [ ] **Step 1: Add failing transfer layout contracts**

Add to `tests/test_frontend_contract.py`:

```python
def test_transfer_density_layout_reserves_timeline_notice_space() -> None:
    html = read_web("index.html")
    css = read_web("styles.css")
    assert 'class="transfer-status transfer-status-strip"' in html
    assert "--timeline-notice-space:" in css
    assert "padding-bottom: var(--timeline-notice-space)" in css
    assert ".timeline-new-btn" in css
    assert "max-height: clamp(160px, 42dvh, 620px)" not in css


def test_transfer_short_viewport_uses_document_flow_for_composer() -> None:
    css = read_web("styles.css")
    assert "@media (max-height: 700px)" in css
    assert ".composer-dock" in css
    assert "position: static" in css
```

- [ ] **Step 2: Run transfer contracts and verify failure**

Run:

```bash
python3 -m pytest tests/test_frontend_contract.py -q -k "transfer_density_layout or transfer_short_viewport"
```

Expected: tests fail because the status-strip class, notice safe area, and short-height mode are absent.

- [ ] **Step 3: Add the transfer semantic class and rebalance grid rows**

Change the existing status header in `web/index.html` without changing IDs:

```html
<header class="transfer-status transfer-status-strip" aria-label="传输状态">
```

Update the transfer layout in `web/styles.css`:

```css
.transfer-workspace {
    --timeline-notice-space: 64px;
    min-height: calc(100dvh - var(--topbar-height) - 40px);
    height: auto;
    max-height: none;
    grid-template-rows: auto minmax(320px, 1fr) auto;
    gap: var(--space-2);
}

.transfer-status-strip {
    padding: var(--space-1);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    background: var(--surface);
}

.transfer-status-strip > div:not(.route-link) {
    padding: 4px 8px;
    border: 0;
    background: transparent;
}

.timeline-container {
    max-height: none;
    padding-bottom: var(--timeline-notice-space);
}

@media (max-height: 700px) {
    .transfer-workspace { display: block; min-height: 0; }
    .transfer-workspace > * + * { margin-top: var(--space-2); }
    .composer-dock { position: static; max-height: none; }
    .timeline-panel { min-height: 320px; }
}
```

- [ ] **Step 4: Reduce composer nesting and improve content hierarchy**

Merge existing composer and message declarations into these outcomes:

```css
.composer-form { padding: var(--space-2); }
.composer-drop-target { gap: var(--space-2); padding: var(--space-2); border: 0; }
.composer-textarea { min-height: 64px; max-height: 160px; }
.composer-actions { gap: var(--space-1); }

.timeline-message-body,
.timeline-message-body a,
.upload-card-name {
    overflow-wrap: anywhere;
}

.upload-card-metrics {
    display: flex;
    flex-wrap: wrap;
    gap: 4px var(--space-2);
}
```

Keep `.btn` targets at 44px or greater. Keep the new-message button sticky and inside the reserved safe area.

- [ ] **Step 5: Add a populated transfer browser regression**

Add a browser test using the existing `live_server` and authenticated page helpers in `tests/test_browser_e2e.py`:

```python
def test_transfer_long_content_and_new_message_notice_do_not_overlap(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    page.set_viewport_size({"width": 390, "height": 844})
    _open_locked_application(browser_session)
    _unlock(page)
    for index in range(12):
        response = browser_session.context.request.post(
            f"{browser_session.base_url}/api/messages",
            data={
                "body": "https://example.com/" + "long-segment-" * 30,
                "client_request_id": f"density-message-{index}",
            },
        )
        assert response.ok
    page.locator("#timelineContainer").evaluate("node => { node.scrollTop = 0; }")
    response = browser_session.context.request.post(
        f"{browser_session.base_url}/api/messages",
        data={"body": "new message", "client_request_id": "density-new-message"},
    )
    assert response.ok
    page.locator("#newMessageButton").wait_for(state="visible")
    message_box = page.locator(".timeline-message").last.bounding_box()
    notice_box = page.locator("#newMessageButton").bounding_box()
    assert message_box is not None and notice_box is not None
    assert message_box["y"] + message_box["height"] <= notice_box["y"] or message_box["y"] >= notice_box["y"] + notice_box["height"]
    _assert_no_horizontal_overflow(page)
    _assert_browser_clean(browser_session)
```

- [ ] **Step 6: Run transfer contracts and browser test**

Run:

```bash
python3 -m pytest tests/test_frontend_contract.py -q
python3 -m pytest tests/test_browser_e2e.py -q -k "transfer_long_content or upload_refresh or resumable_40mb"
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit the transfer route**

```bash
git add web/index.html web/styles.css tests/test_frontend_contract.py tests/test_browser_e2e.py
git commit -m "style(transfer): rebalance timeline and composer"
```

---

### Task 3: Files Route Tool And Result Hierarchy

**Files:**
- Modify: `web/index.html:167-253`
- Modify: `web/styles.css` selectors for `.library-panel`, `.library-tools`, `.filter-grid`, `.control-row`, `.bulk-actions`, `.file-list`, `.file-card`, `.list-mode`, and related media queries.
- Test: `tests/test_frontend_contract.py`
- Test: `tests/test_browser_e2e.py`

**Interfaces:**
- Consumes: Existing library IDs and Task 1 shared spacing; existing `grid-mode` and `list-mode` class behavior from `web/js/library.js`.
- Produces: `.library-primary-tools`, `.library-secondary-tools`, and responsive summary-list behavior. All search, filter, selection, preview, and batch action IDs remain unchanged.

- [ ] **Step 1: Add failing files-route contracts**

Add to `tests/test_frontend_contract.py`:

```python
def test_files_density_uses_two_level_tools_and_auto_fit_results() -> None:
    html = read_web("index.html")
    css = read_web("styles.css")
    assert 'class="library-tools library-primary-tools"' in html
    assert 'class="control-row library-secondary-tools"' in html
    assert "repeat(auto-fit, minmax(min(100%, 240px), 1fr))" in css
    assert "overflow-x: auto" not in css[css.index(".file-list"):css.index(".file-list") + 800]


def test_files_mobile_list_becomes_summary_cards() -> None:
    css = read_web("styles.css")
    assert "@media (max-width: 720px)" in css
    assert ".file-list.list-mode" in css
    assert "grid-template-columns: 1fr" in css
```

- [ ] **Step 2: Run files-route contracts and verify failure**

Run:

```bash
python3 -m pytest tests/test_frontend_contract.py -q -k "files_density or files_mobile_list"
```

Expected: tests fail because the two-level classes and auto-fit rule are absent.

- [ ] **Step 3: Add semantic toolbar classes without changing behavior**

Update class attributes in `web/index.html`:

```html
<div class="library-tools library-primary-tools">
...
<div class="control-row library-secondary-tools">
```

Keep all child IDs and DOM order unchanged.

- [ ] **Step 4: Implement responsive tools and results**

Add or merge these rules in `web/styles.css`:

```css
.library-primary-tools {
    display: grid;
    grid-template-columns: minmax(220px, 1fr) auto auto;
    gap: var(--space-1);
}

.library-secondary-tools {
    justify-content: space-between;
    padding: 0 20px var(--space-3);
}

.file-list.grid-mode {
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
    gap: var(--space-3);
}

@media (max-width: 720px) {
    .library-primary-tools { grid-template-columns: minmax(0, 1fr) auto; }
    .library-primary-tools .search-wrap { grid-column: 1 / -1; }
    .library-secondary-tools { align-items: stretch; flex-direction: column; }
    .file-list.list-mode { display: grid; grid-template-columns: 1fr; }
    .file-list.list-mode .file-card { grid-template-columns: minmax(0, 1fr) auto; }
}
```

Ensure long file names use `overflow-wrap: anywhere`; place metadata below the name and preserve a stable action area.

- [ ] **Step 5: Add files route browser density coverage**

Add a parametrized assertion to the existing route viewport test or a focused test:

```python
@pytest.mark.parametrize("viewport", [
    {"width": 1024, "height": 768},
    {"width": 390, "height": 844},
    {"width": 375, "height": 667},
])
def test_files_tools_and_results_have_no_horizontal_overflow(
    browser_session: BrowserSession, viewport: dict[str, int]
) -> None:
    page = browser_session.page
    page.set_viewport_size(viewport)
    _open_locked_application(browser_session)
    _unlock(page)
    nav_selector = ".sidebar" if viewport["width"] > 720 else ".mobile-nav"
    page.locator(f'{nav_selector} [data-route="files"]').click()
    page.locator("#librarySearch").fill("long-file-name")
    page.locator("#filterToggleBtn").click()
    _assert_no_horizontal_overflow(page)
    for selector in ("#librarySearch", "#filterToggleBtn", "#gridViewBtn", "#listViewBtn"):
        box = page.locator(selector).bounding_box()
        assert box is not None and box["height"] >= 44
    _assert_browser_clean(browser_session)
```

- [ ] **Step 6: Run files contracts and browser coverage**

Run:

```bash
python3 -m pytest tests/test_frontend_contract.py -q
python3 -m pytest tests/test_browser_e2e.py -q -k "files_tools or files_locate or route_navigation"
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit the files route**

```bash
git add web/index.html web/styles.css tests/test_frontend_contract.py tests/test_browser_e2e.py
git commit -m "style(files): clarify tools and result hierarchy"
```

---

### Task 4: Manage Route Card Balance

**Files:**
- Modify: `web/index.html:257-323`
- Modify: `web/styles.css` selectors for `.health`, `.health-item`, `.manage-grid`, `.manage-panel`, `.manage-panel-body`, `.connection-grid`, `.ops-grid`, `.appearance-panel`, `.session-panel`, and related media queries.
- Test: `tests/test_frontend_contract.py`
- Test: `tests/test_browser_e2e.py`

**Interfaces:**
- Consumes: Existing health, connection, storage, appearance, and session IDs plus Task 1 spacing variables.
- Produces: `.manage-primary-panel` and `.manage-setting-panel` hierarchy with adaptive KPI and settings layouts.

- [ ] **Step 1: Add failing manage-route contracts**

Add to `tests/test_frontend_contract.py`:

```python
def test_manage_density_distinguishes_primary_and_setting_panels() -> None:
    html = read_web("index.html")
    css = read_web("styles.css")
    assert 'class="panel manage-panel connection-panel manage-primary-panel"' in html
    assert 'class="panel manage-panel operations-panel manage-primary-panel"' in html
    assert 'class="panel manage-panel manage-setting-panel" id="appearancePanel"' in html
    assert 'class="panel manage-panel session-panel manage-setting-panel"' in html
    assert ".manage-setting-panel .manage-panel-body" in css


def test_manage_health_grid_collapses_without_overflow() -> None:
    css = read_web("styles.css")
    assert "repeat(3, minmax(0, 1fr))" in css
    assert "repeat(2, minmax(0, 1fr))" in css
    assert ".health" in css
```

- [ ] **Step 2: Run manage-route contracts and verify failure**

Run:

```bash
python3 -m pytest tests/test_frontend_contract.py -q -k "manage_density or manage_health_grid"
```

Expected: tests fail because panel hierarchy classes and adaptive KPI rules are absent.

- [ ] **Step 3: Add manage panel hierarchy classes**

Update only class attributes in `web/index.html`:

```html
<section class="panel manage-panel connection-panel manage-primary-panel" ...>
<section class="panel manage-panel operations-panel manage-primary-panel" ...>
<section class="panel manage-panel manage-setting-panel" id="appearancePanel">
<section class="panel manage-panel session-panel manage-setting-panel" id="sessionPanel">
```

- [ ] **Step 4: Balance KPIs, primary panels, and settings rows**

Merge these outcomes into `web/styles.css`:

```css
.health {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: var(--space-2);
}

.manage-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--space-3);
}

.manage-primary-panel { min-width: 0; }

.manage-setting-panel .manage-panel-body {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    min-height: 76px;
}

@media (max-width: 1024px) {
    .health { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}

@media (max-width: 720px) {
    .health, .manage-grid { grid-template-columns: 1fr; }
    .manage-setting-panel .manage-panel-body { align-items: stretch; }
    .manage-setting-panel .btn { width: 100%; }
}
```

Keep refresh buttons in their headers and preserve the current live-region behavior.

- [ ] **Step 5: Add manage viewport coverage**

Add to `tests/test_browser_e2e.py`:

```python
@pytest.mark.parametrize("viewport", [
    {"width": 1440, "height": 900},
    {"width": 1024, "height": 768},
    {"width": 390, "height": 844},
])
def test_manage_cards_reflow_without_horizontal_overflow(
    browser_session: BrowserSession, viewport: dict[str, int]
) -> None:
    page = browser_session.page
    page.set_viewport_size(viewport)
    _open_locked_application(browser_session)
    _unlock(page)
    nav_selector = ".sidebar" if viewport["width"] > 720 else ".mobile-nav"
    page.locator(f'{nav_selector} [data-route="manage"]').click()
    page.locator("#connectionPanel").wait_for(state="visible")
    _assert_no_horizontal_overflow(page)
    for selector in ("#railRefresh", "#refreshOpsBtn", "#manageThemeToggle", "#logoutButton"):
        box = page.locator(selector).bounding_box()
        assert box is not None and box["height"] >= 44
    _assert_browser_clean(browser_session)
```

- [ ] **Step 6: Run manage contracts and browser coverage**

Run:

```bash
python3 -m pytest tests/test_frontend_contract.py -q
python3 -m pytest tests/test_browser_e2e.py -q -k "manage_cards or health_storage or route_navigation"
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit the manage route**

```bash
git add web/index.html web/styles.css tests/test_frontend_contract.py tests/test_browser_e2e.py
git commit -m "style(manage): balance status and settings panels"
```

---

### Task 5: Cross-Route Responsive And Accessibility Verification

**Files:**
- Modify: `web/styles.css` only for issues proven by this task's tests.
- Modify: `web/index.html` only for accessibility issues proven by this task's tests.
- Test: `tests/test_frontend_contract.py`
- Test: `tests/test_browser_e2e.py`

**Interfaces:**
- Consumes: Completed layouts from Tasks 1-4.
- Produces: Verified no-overflow, touch-target, keyboard-focus, light/dark, and reduced-motion behavior across all supported routes and viewports.

- [ ] **Step 1: Add a cross-route viewport matrix test**

Add or extend a parametrized browser test in `tests/test_browser_e2e.py`:

```python
@pytest.mark.parametrize("viewport", [
    {"width": 1440, "height": 900},
    {"width": 1024, "height": 768},
    {"width": 390, "height": 844},
    {"width": 375, "height": 667},
])
@pytest.mark.parametrize("route", ["transfer", "files", "manage"])
def test_workspace_density_matrix(
    browser_session: BrowserSession,
    viewport: dict[str, int],
    route: str,
) -> None:
    page = browser_session.page
    page.set_viewport_size(viewport)
    _open_locked_application(browser_session)
    _unlock(page)
    nav_selector = ".sidebar" if viewport["width"] > 720 else ".mobile-nav"
    page.locator(f'{nav_selector} [data-route="{route}"]').click()
    page.locator(f'[data-route-page="{route}"]').wait_for(state="visible")
    _assert_no_horizontal_overflow(page)
    assert page.locator(f'[data-route-heading="{route}"]').get_attribute("tabindex") == "-1"
    _assert_browser_clean(browser_session)
```

- [ ] **Step 2: Run the matrix and record concrete failures**

Run:

```bash
python3 -m pytest tests/test_browser_e2e.py -q -k workspace_density_matrix
```

Expected before final corrections: any remaining overflow or focus regression fails with its exact route and viewport parameter.

- [ ] **Step 3: Correct only failures demonstrated by the matrix**

Use bounded responsive fixes such as:

```css
.route-page,
.panel,
.panel-head > *,
.library-tools,
.file-card,
.manage-panel {
    min-width: 0;
}

@media (max-width: 430px) {
    .panel-head { padding-inline: var(--space-2); }
    .upload-summary-actions,
    .file-actions { width: 100%; }
}
```

Add selector-specific rules only when a failing route proves they are required.

- [ ] **Step 4: Verify theme and reduced-motion behavior**

Extend existing browser assertions:

```python
def test_density_layout_preserves_dark_theme_and_reduced_motion(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    _open_locked_application(browser_session)
    _unlock(page)
    page.emulate_media(reduced_motion="reduce")
    page.locator("#themeToggle").click()
    assert page.locator("html").evaluate("node => node.classList.contains('dark')")
    scroll_behavior = page.locator("#timelineContainer").evaluate(
        "node => getComputedStyle(node).scrollBehavior"
    )
    assert scroll_behavior == "auto"
    _assert_browser_clean(browser_session)
```

- [ ] **Step 5: Run complete verification**

Run:

```bash
python3 -m pytest tests/test_frontend_contract.py -q
python3 -m pytest tests/test_browser_e2e.py -q
python3 -m pytest -q
python3 -m compileall -q app server.py tests
git diff --check
```

Expected: frontend contracts, all 22 or more browser E2E tests, and the complete default suite pass; compile and diff checks exit successfully.

- [ ] **Step 6: Preview all routes**

Start or reuse the documented preview server, request the platform preview URL, and manually inspect Transfer, Files, and Manage at 1440x900, 1024x768, 390x844, and 375x667 in light and dark themes. Confirm populated, empty, long-content, active-upload, expanded-filter, active-selection, and management-error states.

- [ ] **Step 7: Commit final responsive corrections**

```bash
git add web/index.html web/styles.css tests/test_frontend_contract.py tests/test_browser_e2e.py
git commit -m "test(ui): verify workspace density across viewports"
```
