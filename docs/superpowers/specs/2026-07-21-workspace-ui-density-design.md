# Workspace UI Density Design

## Purpose

Improve information density and responsive behavior across the Transfer, Files, and Manage routes while preserving the existing MonkeyCode visual language. The work retains the current colors, typography, radii, controls, dark theme, and interaction model.

## Goals

- Give primary content more usable space at desktop, tablet, and mobile sizes.
- Remove content overlap and reduce nested visual boundaries.
- Establish consistent information hierarchy and spacing across all routes.
- Preserve 44px touch targets, keyboard navigation, focus visibility, reduced-motion behavior, and dark mode.
- Prevent horizontal scrolling at 375px and wider supported viewports.

## Non-Goals

- Rebranding or replacing the existing visual system.
- Changing upload, message, file, session, or storage behavior.
- Introducing a component framework or runtime dependency.
- Reworking navigation routes or backend APIs.

## Shared Layout System

### Spacing

Use a shared `8 / 12 / 16 / 24px` spacing scale. Page regions use 24px separation on desktop, 16px on tablet, and 12px on mobile. Internal component gaps use 8px or 12px. Card padding uses 16px or 20px according to content density.

### Content Width

The workspace keeps the existing sidebar and top bar. Route content receives a readable maximum width with centered horizontal space on wide displays. Dense operational content may use the full available width inside that maximum.

### Visual Hierarchy

Primary containers retain the existing surface, border, and radius treatment. Nested sections use spacing, typography, and subtle background changes before introducing another border. Headings, supporting copy, metadata, and actions follow a consistent four-level hierarchy.

### Responsive Breakpoints

- Desktop: above 1024px, sidebar navigation and multi-column layouts.
- Tablet: 721px to 1024px, reduced page gutters and flexible two-column layouts.
- Mobile: up to 720px, bottom navigation and single-column content.
- Compact mobile: up to 430px, abbreviated secondary labels and stacked action groups where needed.

Controls keep a minimum 44px interactive target at every breakpoint. Text and metadata may become more compact while hit areas remain unchanged.

## Transfer Route

### Status Strip

Replace the visually separate source and target cards with one compact status strip. Source device, verification method, target device, and connection state remain visible. The strip wraps predictably on tablet and uses a compact three-column arrangement on mobile.

### Timeline

The timeline becomes the primary flexible region. It receives the remaining viewport height after the status strip and composer, with a practical minimum height and no restrictive desktop maximum that compresses active content.

Messages separate body content from metadata. Long URLs and identifiers wrap safely or truncate with an accessible full value. Upload cards keep filename, state, progress, metrics, and controls in distinct rows.

The new-message control remains sticky inside the timeline. The scroll container reserves a bottom safe area equal to the control height and offset, preventing overlap with message content.

### Composer

The composer uses one primary boundary. Its descriptive header becomes compact on desktop and hidden on compact mobile. The text area and action row use consistent internal spacing. Attachment and send actions stay grouped with at least 8px separation and retain 44px targets.

The composer remains sticky where viewport height allows. Short-height layouts use natural document flow so the timeline and composer can both remain reachable without competing nested scroll areas.

### Upload Summary

Batch status and controls form a wrapping toolbar. Status text receives the flexible space. Actions wrap as a group on tablet and become a clear stacked or grid arrangement on compact mobile.

## Files Route

### Header And Tools

Organize controls into two levels:

1. Title, result count, search, and filter toggle.
2. Expanded filters, view selection, and contextual statistics.

Image and other-file counts become lightweight metadata near the result summary. The filter region preserves labels and keyboard order while using an adaptive grid.

### Selection Actions

Bulk actions appear only when selection is active. Desktop and tablet show a compact toolbar near the result list. Mobile continues to use the bottom batch toolbar with safe-area spacing. The duplicate inactive toolbar contributes no visual height.

### Results

The card view uses auto-fitting columns with a minimum readable card width. File names wrap safely, metadata forms a secondary row, and actions occupy a stable footer.

The list view remains tabular on desktop. Tablet and mobile use a summary-list presentation that avoids horizontal scrolling while preserving selection, preview, download, and metadata access.

## Manage Route

### Health Summary

Connection, file count, and storage usage form an adaptive KPI grid. Values receive stronger visual weight and labels remain secondary. The grid uses three columns on desktop and collapses progressively.

### Primary Panels

Connection and storage panels are the primary management cards. Their headers, body padding, KPI rows, and refresh actions share common alignment and spacing. A balanced grid prevents one panel from creating an isolated empty column.

### Settings Panels

Appearance and session controls become compact setting rows on desktop and tablet. Each row has a title, supporting text, and right-aligned action. Mobile stacks the action below the copy while preserving the same semantic order.

## Accessibility

- Preserve DOM reading order when visual grids change.
- Preserve route heading focus behavior and skip-link behavior.
- Keep visible focus rings and minimum 44px interactive targets.
- Use wrapping and truncation only when the complete value remains available through the link, title, or accessible label.
- Keep status and upload announcements in the existing live regions.
- Preserve `prefers-reduced-motion` behavior.
- Maintain light and dark theme contrast.

## Implementation Boundaries

Most changes belong in `web/styles.css`. Small semantic grouping changes may be made in `web/index.html` where CSS alone cannot express the required hierarchy. JavaScript behavior remains unchanged except for class or hidden-state wiring required by contextual toolbars.

Existing selectors and IDs used by application logic and tests remain stable. New wrapper classes should be minimal and route-specific.

## Verification

### Automated

- Update frontend contract tests for structural and responsive expectations.
- Run the complete frontend contract suite.
- Run browser E2E tests at existing viewport coverage.
- Verify keyboard navigation, route focus, dialogs, drawers, and batch toolbars.
- Run the default backend test suite to detect integration regressions.

### Visual Viewports

- 1440x900 desktop.
- 1024x768 tablet landscape.
- 390x844 mobile.
- 375x667 compact mobile.

At each viewport verify all three routes, light and dark themes, empty and populated states, long filenames and URLs, active uploads, expanded filters, active selection, and management error states.

### Acceptance Criteria

- Timeline content is never covered by the new-message control.
- Transfer status, timeline, and composer remain reachable without horizontal scrolling.
- Files tools wrap or collapse without overlap, and result content stays readable.
- Manage cards form a balanced hierarchy at every supported width.
- No supported viewport has horizontal document overflow.
- Interactive targets remain at least 44px.
- Existing functional and accessibility tests pass.
