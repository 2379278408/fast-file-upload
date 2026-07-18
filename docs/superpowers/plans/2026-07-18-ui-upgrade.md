# UI Upgrade Implementation Plan

> **For agentic workers:** Use subagent-driven-development or executing-plans to implement this plan task-by-task.

**Goal:** Transform the file transfer assistant from vertical panel stacking to a sidebar + topbar + dual-column workspace layout, with refined visual tokens and improved information architecture.

**Architecture:** Preserve all existing API, WebSocket, and session logic. Only change the frontend HTML structure, CSS tokens/layout, and JS DOM selectors. The backend remains untouched.

**Tech Stack:** Vanilla JS (ES Modules), CSS (custom properties), HTML5. No framework changes.

## Global Constraints

- Preserve all existing functionality: upload, download, preview, batch ops, timeline, undo, dark mode
- Backend API (`app/`) remains untouched
- CSP compliance: no inline styles/scripts, no `element.style` assignments
- Minimum touch target: 44px
- Responsive breakpoints: 360, 390, 430, 720, 900, 1024, 1366, 1440, 1920px
- OKLCH color tokens with light/dark variants
- Typography: Outfit (display), system (body), monospace (data)

---

## Task 1: CSS Token Overhaul

**Files:**
- Modify: `web/styles.css:1-60` (root variables)

**Goal:** Replace existing hex color tokens with OKLCH, add semantic colors (signal, danger), update typography and radius tokens.

- [ ] **Step 1: Update :root tokens**

Replace the existing `:root` block (lines 2-22) with:

```css
:root {
  --bg: oklch(96% 0.012 257);
  --surface: oklch(100% 0 0);
  --surface-soft: oklch(98% 0.006 257);
  --fg: oklch(25% 0.035 258);
  --muted: oklch(54% 0.03 257);
  --border: oklch(91% 0.012 257);
  --border-strong: oklch(84% 0.018 257);
  --accent: oklch(55% 0.22 263);
  --accent-dark: oklch(45% 0.2 263);
  --accent-soft: oklch(95% 0.028 263);
  --signal: oklch(58% 0.14 170);
  --signal-soft: oklch(95% 0.035 170);
  --danger: oklch(54% 0.19 27);
  --danger-soft: oklch(96% 0.026 27);
  --shadow-float: 0 22px 60px oklch(25% 0.04 258 / .16);
  --font-display: "Outfit", "Avenir Next", -apple-system, BlinkMacSystemFont, sans-serif;
  --font-body: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --font-mono: "JetBrains Mono", "SFMono-Regular", Consolas, monospace;
  color-scheme: light dark;
}
```

- [ ] **Step 2: Update .dark tokens**

Replace the `.dark` block (lines 24-40) with:

```css
.dark {
  --bg: oklch(17% 0.025 258);
  --surface: oklch(21% 0.028 258);
  --surface-soft: oklch(24% 0.028 258);
  --fg: oklch(94% 0.008 257);
  --muted: oklch(70% 0.025 257);
  --border: oklch(100% 0 0 / .09);
  --border-strong: oklch(100% 0 0 / .17);
  --accent: oklch(68% 0.17 263);
  --accent-dark: oklch(76% 0.13 263);
  --accent-soft: oklch(30% 0.07 263);
  --signal: oklch(72% 0.13 170);
  --signal-soft: oklch(29% 0.05 170);
  --danger: oklch(70% 0.15 27);
  --danger-soft: oklch(29% 0.05 27);
  --shadow-float: 0 22px 60px oklch(5% 0.02 258 / .45);
}
```

- [ ] **Step 3: Update body styles**

Replace body background with flat color (remove gradient):

```css
body {
  margin: 0;
  min-height: 100vh;
  font-family: var(--font-body);
  color: var(--fg);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
}
```

- [ ] **Step 4: Update radius tokens**

```css
--radius-xl: 14px;
--radius-lg: 12px;
--radius-md: 10px;
```

- [ ] **Step 5: Run tests**

```bash
python3 -m pytest -q
```

- [ ] **Step 6: Commit**

```bash
git add web/styles.css && git commit -m "refactor(ui): update CSS tokens to OKLCH with semantic colors"
```

---

## Task 2: HTML Restructure — Sidebar + Topbar Shell

**Files:**
- Modify: `web/index.html` (full restructure)

**Goal:** Replace the vertical panel layout with a sidebar + topbar + main workspace shell.

- [ ] **Step 1: Read current index.html**

Read the full current HTML to understand all element IDs and structure.

- [ ] **Step 2: Create new HTML structure**

Replace the `<body>` content with the new layout:

```html
<body>
  <a href="#mainContent" id="skipLink" class="sr-only skip-link">跳转到内容</a>

  <!-- Session: unlock overlay -->
  <div id="sessionExpired" class="session-overlay is-hidden" role="dialog" aria-modal="true" aria-labelledby="unlockTitle">
    <!-- Keep existing unlock form unchanged -->
  </div>

  <div class="app-shell">
    <!-- Sidebar -->
    <aside class="sidebar" id="sidebar">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true"><!-- monkey SVG --></span>
        <span class="brand-copy"><strong>MonkeyCode</strong><span>传输工作台</span></span>
      </div>
      <p class="side-label">工作区</p>
      <nav class="nav-list" aria-label="主导航">
        <button class="nav-item active" type="button" aria-current="page" data-section="workspace">传输工作台</button>
        <button class="nav-item" type="button" data-section="files">全部文件</button>
        <button class="nav-item" type="button" data-section="activity">活动记录</button>
      </nav>
      <p class="side-label">管理</p>
      <nav class="nav-list" aria-label="管理导航">
        <button class="nav-item" type="button" data-section="devices">连接设备</button>
        <button class="nav-item" type="button" data-section="settings">设置</button>
      </nav>
      <div class="sidebar-foot">
        <div class="storage-compact">
          <div class="storage-head"><span>本地存储</span><strong id="sidebarStorage">-</strong></div>
          <div class="storage-bar"><span id="sidebarStorageBar"></span></div>
        </div>
      </div>
    </aside>

    <!-- Main workspace -->
    <main class="workspace" id="mainContent">
      <header class="topbar">
        <div class="mobile-brand"><!-- monkey SVG --> MonkeyCode</div>
        <div class="breadcrumb"><span class="status-dot" id="connectionDot"></span><strong>个人工作区</strong></div>
        <div class="top-actions">
          <button class="button quiet" id="refreshHealthBtn" type="button">刷新状态</button>
          <button class="icon-button" id="themeToggle" type="button" aria-label="切换主题"><!-- theme SVG --></button>
        </div>
      </header>

      <div class="page">
        <!-- Health bar -->
        <div class="health" aria-label="工作区状态">
          <div class="health-item"><span>连接</span><strong id="healthConnection">-</strong></div>
          <div class="health-item"><span>单文件上限</span><strong id="healthLimit">-</strong></div>
          <div class="health-item"><span>访问模式</span><strong id="healthMode">-</strong></div>
        </div>

        <div class="dashboard-grid">
          <!-- Main column -->
          <div class="main-column">
            <!-- Transfer panel (composer) -->
            <section class="panel transfer-panel" id="composerPanel">
              <!-- Keep existing composer functionality -->
            </section>

            <!-- File library -->
            <section class="panel library-panel" id="libraryView">
              <!-- Keep existing library functionality, convert to table -->
            </section>
          </div>

          <!-- Context rail -->
          <aside class="rail">
            <!-- Connection panel -->
            <section class="panel" id="connectionPanel">
              <!-- Connection status -->
            </section>

            <!-- Recent activity (timeline summary) -->
            <section class="panel" id="timelinePanel">
              <!-- Recent timeline entries -->
            </section>

            <!-- Quick message -->
            <section class="panel message-panel" id="messagePanel">
              <!-- Text message input -->
            </section>
          </aside>
        </div>
      </div>
    </main>
  </div>

  <!-- Mobile bottom nav -->
  <nav class="mobile-nav" aria-label="移动端主导航">
    <button class="active" type="button" data-section="workspace">工作台</button>
    <button type="button" data-section="files">文件</button>
    <button type="button" data-section="activity">活动</button>
    <button type="button" data-section="settings">设置</button>
  </nav>

  <!-- Batch bar, preview drawer, toast (keep existing) -->
  <div class="batch-toolbar" id="batchToolbar">...</div>
  <div class="preview-modal" id="previewModal">...</div>
  <div class="toast" id="toast" role="status" aria-live="polite"></div>

  <script type="module" src="/js/app.js"></script>
</body>
```

- [ ] **Step 3: Preserve all element IDs**

Ensure these IDs are preserved (JS depends on them):
- `sessionExpired`, `unlockForm`, `accessToken`, `deviceName`, `unlockSubmit`
- `composerForm`, `composerTextarea`, `composerFileInput`, `composerDropTarget`, `composerQueue`
- `fileList`, `libraryCount`, `imageCount`, `otherCount`, `selectedCount`
- `librarySearch`, `fileTypeFilter`, `deviceFilter`, `dateFrom`, `dateTo`
- `gridViewBtn`, `listViewBtn`, `selectVisibleBtn`, `batchDownload`, `batchCopy`, `batchDelete`, `clearSelectionBtn`
- `libraryLoadMore`, `storageSummary`
- `timelineContainer`, `newMessageButton`, `timelineEmpty`
- `previewModal`, `previewImage`, `previewTitle`, `previewSize`, `previewDate`, `previewType`
- `previewCopyBtn`, `previewDownloadBtn`, `closePreviewBtn`
- `toast`, `batchToolbar`, `batchToolbarCount`, `batchToolbarDownload`, `batchToolbarCopy`, `batchToolbarDelete`, `batchToolbarClear`
- `connectionStatus`, `metricMode`, `metricLimit`, `metricCount`, `metricSize`
- `refreshHealthBtn`, `themeToggle`

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_frontend_contract.py -q
```

- [ ] **Step 5: Commit**

```bash
git add web/index.html && git commit -m "refactor(ui): restructure HTML to sidebar + topbar + dual-column layout"
```

---

## Task 3: CSS Layout — Sidebar + Topbar + Dashboard Grid

**Files:**
- Modify: `web/styles.css` (add new layout styles)

**Goal:** Implement the new layout system with sidebar, topbar, and dashboard grid.

- [ ] **Step 1: Add layout styles**

Add after the existing root/dark tokens:

```css
/* Layout shell */
.app-shell { min-height: 100vh; display: grid; grid-template-columns: 228px minmax(0, 1fr); }
.sidebar {
  position: sticky; top: 0; height: 100vh;
  display: flex; flex-direction: column; padding: 22px 16px;
  border-right: 1px solid var(--border); background: var(--surface); z-index: 10;
}
.brand { display: flex; align-items: center; gap: 11px; min-height: 44px; padding: 0 8px; }
.brand-mark { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 10px; color: var(--surface); background: var(--fg); }
.brand-copy strong { display: block; font: 600 16px/1.1 var(--font-display); letter-spacing: -.01em; }
.brand-copy span { color: var(--muted); font-size: 11px; letter-spacing: .02em; }
.side-label { margin: 30px 10px 8px; color: var(--muted); font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }
.nav-list { display: grid; gap: 4px; }
.nav-item {
  min-height: 44px; display: flex; align-items: center; gap: 11px;
  padding: 0 11px; border: 0; border-radius: 10px;
  color: var(--muted); background: transparent; text-align: left;
  font-size: 14px; font-weight: 520; letter-spacing: .02em;
}
.nav-item:hover { color: var(--fg); background: var(--surface-soft); }
.nav-item.active { color: var(--accent-dark); background: var(--accent-soft); }
.sidebar-foot { margin-top: auto; display: grid; gap: 12px; }
.storage-compact { padding: 13px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-soft); }
.storage-head { display: flex; justify-content: space-between; gap: 8px; color: var(--muted); font-size: 12px; }
.storage-head strong { color: var(--fg); font-family: var(--font-mono); font-weight: 500; }
.storage-bar { height: 4px; margin-top: 10px; overflow: hidden; border-radius: 4px; background: var(--border); }
.storage-bar span { display: block; height: 100%; background: var(--signal); }

.workspace { min-width: 0; }
.topbar {
  height: 68px; position: sticky; top: 0;
  display: flex; align-items: center; justify-content: space-between; gap: 20px;
  padding: 0 28px; border-bottom: 1px solid var(--border);
  background: color-mix(in oklch, var(--bg) 86%, transparent);
  backdrop-filter: blur(16px); z-index: 8;
}
.mobile-brand { display: none; align-items: center; gap: 9px; font: 600 15px/1 var(--font-display); }
.breadcrumb { display: flex; align-items: center; gap: 9px; color: var(--muted); font-size: 13px; }
.breadcrumb strong { color: var(--fg); font-weight: 550; }
.status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--signal); box-shadow: 0 0 0 4px var(--signal-soft); }
.top-actions { display: flex; align-items: center; gap: 8px; }

.page { max-width: 1480px; margin: 0 auto; padding: 30px 28px 48px; }
.health { display: flex; align-items: center; gap: 16px; padding: 11px 14px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); margin-bottom: 24px; }
.health-item { min-width: 76px; }
.health-item span { display: block; color: var(--muted); font-size: 11px; letter-spacing: .02em; }
.health-item strong { display: block; margin-top: 3px; font: 500 13px/1.2 var(--font-mono); }

.dashboard-grid { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 20px; align-items: start; }
.main-column, .rail { display: grid; gap: 18px; min-width: 0; }
.panel { border: 1px solid var(--border); border-radius: 14px; background: var(--surface); }
.panel-head { min-height: 64px; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 14px 18px; border-bottom: 1px solid var(--border); }
```

- [ ] **Step 2: Add component styles**

```css
/* Buttons */
.icon-button, .button {
  min-height: 44px; border: 1px solid var(--border); border-radius: 10px;
  background: var(--surface); transition: transform 140ms ease, background 140ms ease, border-color 140ms ease;
}
.icon-button { width: 44px; display: grid; place-items: center; }
.button { display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 0 14px; font-size: 13px; font-weight: 600; letter-spacing: .02em; }
.button.primary { color: white; border-color: var(--accent); background: var(--accent); }
.button.danger { color: var(--danger); background: var(--danger-soft); border-color: transparent; }
.button.quiet { color: var(--muted); background: transparent; }

/* Form controls */
.control { width: 100%; min-height: 44px; padding: 0 13px; border: 1px solid var(--border); border-radius: 10px; color: var(--fg); background: var(--surface-soft); }
.control:focus { border-color: var(--accent); background: var(--surface); }

/* Mobile nav */
.mobile-nav { display: none; }
```

- [ ] **Step 3: Add responsive breakpoints**

```css
@media (max-width: 1024px) {
  .dashboard-grid { grid-template-columns: 1fr; }
  .rail { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 900px) {
  .app-shell { grid-template-columns: 78px minmax(0, 1fr); }
  .sidebar { padding-inline: 10px; align-items: center; }
  .brand-copy, .side-label, .nav-item span, .storage-compact { display: none; }
  .nav-item { justify-content: center; padding: 0; }
}
@media (max-width: 720px) {
  body { padding-bottom: 70px; }
  .app-shell { display: block; }
  .sidebar { display: none; }
  .topbar { height: 60px; padding: 0 16px; }
  .mobile-brand { display: flex; }
  .breadcrumb { display: none; }
  .page { padding: 22px 14px 32px; }
  .rail { grid-template-columns: 1fr; }
  .mobile-nav { position: fixed; left: 0; right: 0; bottom: 0; height: 66px; display: grid; grid-template-columns: repeat(4, 1fr); padding: 6px 8px calc(6px + env(safe-area-inset-bottom)); border-top: 1px solid var(--border); background: color-mix(in oklch, var(--surface) 92%, transparent); backdrop-filter: blur(16px); z-index: 20; }
  .mobile-nav button { display: grid; place-items: center; gap: 2px; border: 0; color: var(--muted); background: transparent; font-size: 10px; min-height: 44px; }
  .mobile-nav button.active { color: var(--accent-dark); }
}
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add web/styles.css && git commit -m "feat(ui): add sidebar + topbar + dashboard grid layout system"
```

---

## Task 4: JS Module Adaptation — DOM Selectors

**Files:**
- Modify: `web/js/app.js` (DOM selectors and initialization)
- Modify: `web/js/library.js` (file list rendering)
- Modify: `web/js/timeline.js` (timeline rendering)
- Modify: `web/js/composer.js` (composer initialization)

**Goal:** Update JS modules to work with new DOM structure while preserving all existing functionality.

- [ ] **Step 1: Update app.js selectors**

Read current app.js and update DOM selectors to match new HTML structure. Key changes:
- Navigation buttons: `data-section` attributes instead of tab-based navigation
- Health items: new `.health-item` structure
- Theme toggle: `.icon-button` instead of `.theme-toggle`

- [ ] **Step 2: Update library.js rendering**

Convert file card rendering to table row rendering for the new `.file-table` structure. Keep grid/list as toggle between table and compact table views.

- [ ] **Step 3: Update timeline.js rendering**

Adapt timeline rendering for the new `.rail` context. Show only recent entries (last 5) in the rail, with a "view all" link.

- [ ] **Step 4: Update composer.js**

Adapt composer for the new `.transfer-panel` structure. Keep all existing functionality (text, file upload, drag-drop, queue).

- [ ] **Step 5: Run all tests**

```bash
python3 -m pytest -q
```

- [ ] **Step 6: Commit**

```bash
git add web/js/ && git commit -m "refactor(ui): adapt JS modules for new sidebar + dashboard layout"
```

---

## Task 5: Responsive Regression + E2E

**Files:**
- Modify: `web/styles.css` (responsive fine-tuning)
- Test: `tests/test_browser_e2e.py`

**Goal:** Verify all responsive breakpoints work correctly and E2E tests pass.

- [ ] **Step 1: Test at all breakpoints**

Manually verify (or add CSS tests) at: 360, 390, 430, 720, 900, 1024, 1366, 1440, 1920px

- [ ] **Step 2: Fix any horizontal overflow issues**

- [ ] **Step 3: Run full test suite**

```bash
python3 -m pytest -q
python3 -m pytest tests/test_browser_e2e.py -q
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "fix(ui): responsive regression fixes across all breakpoints"
```

---

## Task 6: Visual Polish — Transfer Route, File Table, Connection Panel

**Files:**
- Modify: `web/styles.css`
- Modify: `web/index.html`

**Goal:** Add the transfer route visualization, refine file table styling, and polish the connection panel.

- [ ] **Step 1: Add transfer route CSS**

```css
.transfer-route { max-width: 430px; display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); align-items: center; gap: 10px; margin-top: 18px; }
.route-node { min-width: 0; padding: 9px 10px; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); }
.route-link { position: relative; min-width: 68px; color: var(--signal); text-align: center; }
.route-link::before { content: ""; position: absolute; left: 0; right: 0; top: 7px; height: 1px; background: var(--signal); }
.route-link::after { content: ""; position: absolute; right: 0; top: 4px; width: 6px; height: 6px; border-top: 1px solid var(--signal); border-right: 1px solid var(--signal); transform: rotate(45deg); }
```

- [ ] **Step 2: Add file table CSS**

```css
.file-table { width: 100%; border-collapse: collapse; }
.file-table th { height: 42px; padding: 0 14px; color: var(--muted); font-size: 11px; font-weight: 550; text-align: left; border-bottom: 1px solid var(--border); }
.file-table td { height: 68px; padding: 9px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }
.file-table tbody tr:hover { background: var(--surface-soft); }
```

- [ ] **Step 3: Add connection panel CSS**

```css
.connection { padding: 16px 18px; }
.connection-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; margin-top: 16px; overflow: hidden; border: 1px solid var(--border); border-radius: 10px; background: var(--border); }
.connection-grid div { padding: 12px; background: var(--surface-soft); }
```

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(ui): add transfer route, file table, and connection panel styling"
```

---

## Task 7: Drawer Preview + Batch Bar + Toast

**Files:**
- Modify: `web/styles.css`
- Modify: `web/index.html`

**Goal:** Convert preview modal to right drawer, refine batch bar and toast positioning.

- [ ] **Step 1: Add drawer CSS**

```css
.drawer-backdrop { position: fixed; inset: 0; display: none; background: oklch(14% .03 258 / .46); z-index: 40; }
.drawer-backdrop.open { display: block; }
.drawer { position: absolute; top: 0; right: 0; width: min(440px, 100%); height: 100%; display: flex; flex-direction: column; background: var(--surface); box-shadow: var(--shadow-float); animation: drawer-in 180ms ease-out; }
@keyframes drawer-in { from { transform: translateX(24px); opacity: .65; } }
```

- [ ] **Step 2: Update batch bar positioning**

```css
.batch-bar { position: fixed; left: calc(228px + (100vw - 228px) / 2); bottom: 22px; /* ... */ }
@media (max-width: 900px) { .batch-bar { left: calc(78px + (100vw - 78px) / 2); } }
@media (max-width: 720px) { .batch-bar { left: 50%; bottom: 78px; } }
```

- [ ] **Step 3: Run tests**

```bash
python3 -m pytest -q
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat(ui): drawer preview, refined batch bar and toast positioning"
```

---

## Task 8: Dark Mode + Accessibility Final Pass

**Files:**
- Modify: `web/styles.css`
- Modify: `web/index.html`

**Goal:** Ensure dark mode works correctly with all new components, verify accessibility.

- [ ] **Step 1: Verify dark mode tokens**

Check all new components have proper dark mode styling using the `.dark` class tokens.

- [ ] **Step 2: Verify focus states**

All interactive elements have visible focus states with `:focus-visible`.

- [ ] **Step 3: Verify ARIA attributes**

All new structural elements have proper ARIA roles and labels.

- [ ] **Step 4: Run full test suite**

```bash
python3 -m pytest -q
python3 -m pytest tests/test_browser_e2e.py -q
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "fix(ui): dark mode and accessibility final pass"
```
