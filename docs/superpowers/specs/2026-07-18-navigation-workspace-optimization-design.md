# Navigation And Workspace Optimization Design

**Date:** 2026-07-18

## Goal

Refocus the personal transfer assistant around its primary workflow: sending text and files across devices, reviewing the transfer timeline, and managing stored files. Replace the current five-button panel visibility mapping with three coherent, addressable destinations.

## Scope

This change covers the frontend shell, navigation state, page composition, responsive behavior, accessibility, and frontend tests.

The FastAPI routes, session protocol, WebSocket event model, upload and download behavior, soft-delete window, and persistence model remain unchanged.

## Confirmed Decisions

- Use three top-level destinations: Transfer, Files, and Manage.
- Use hash routes: `#transfer`, `#files`, and `#manage`.
- Make the transfer timeline the primary workspace surface.
- Dock the composer and upload queue at the bottom of the timeline surface.
- Consolidate device, storage, session, and appearance controls in Manage.
- Keep desktop sidebar and mobile bottom navigation semantically identical.
- Preserve current frontend modules and API contracts where practical.

## Information Architecture

### Transfer

The Transfer destination contains:

- A compact route header with current device, target device, and connection state.
- The message and file timeline as the main scrollable content.
- Empty-state guidance that leads directly to the composer.
- A composer dock containing text input, file selection, drag-and-drop support, send action, and upload queue.
- Compact connection recovery feedback when WebSocket delivery is interrupted.

The file library, storage summary, and detailed connection diagnostics are excluded from this destination.

### Files

The Files destination contains:

- File count and search.
- Collapsible type, device, and date filters.
- Card and table view controls.
- File results, preview, download, copy, locate, and delete actions.
- Batch selection and batch actions.
- A focused empty state with an action that returns to Transfer and opens file selection.

The batch toolbar is interactive only while at least one file is selected.

### Manage

The Manage destination contains grouped sections for:

- Connected device and real-time event status.
- Storage usage and file statistics.
- Appearance controls.
- Session information and logout.

Connection retry remains available beside detailed connection diagnostics. Destructive session actions are visually separated from ordinary settings.

## Navigation Model

A small navigation controller owns route parsing and presentation state.

Supported hashes:

- `#transfer`
- `#files`
- `#manage`

An empty or unknown hash resolves to `#transfer`. The controller listens for `hashchange`, activates exactly one page, and synchronizes:

- Desktop and mobile active navigation states.
- `aria-current="page"`.
- Breadcrumb and document title.
- Main heading focus after a user-initiated route change.
- Per-page scroll restoration.

Browser forward and back navigation use the native hash history. Authentication overlays do not replace the hash. Successful unlock returns the user to the requested destination.

## Layout

### Desktop

- Preserve the persistent sidebar and top bar.
- Replace the dual-column dashboard with one route-page container.
- Constrain Transfer to a readable central width while allowing the timeline to use the available vertical space.
- Let Files use the full route content width.
- Constrain Manage groups to a readable settings width.

### Tablet

- Keep the compact icon sidebar through the existing 900px breakpoint.
- Use one content column for every route.
- Preserve full labels through accessible names and tooltips when visual labels collapse.

### Mobile

- Use a three-item fixed bottom navigation.
- Reserve space for the bottom navigation using a shared safe-area token.
- Place the composer dock directly above the bottom navigation on Transfer.
- Keep timeline content clear of both fixed elements.
- Present Files and Manage as ordinary single-column scrolling pages.
- Use one consistent 430px breakpoint block.

## State Preservation

Route changes preserve:

- Unsent composer text.
- Upload queue state.
- Timeline scroll position.
- File search, filters, selected view, and list scroll position.
- Manage page scroll position.

Leaving Files clears transient batch selection and closes the batch toolbar. Persistent file filters remain until explicitly cleared.

## Interaction And Feedback

The batch toolbar uses hidden interaction semantics when inactive:

- `visibility: hidden`
- `opacity: 0`
- `pointer-events: none`

Its visible state restores visibility and pointer interaction. This prevents the transparent toolbar from intercepting the mobile Settings destination.

Async controls retain disabled and loading states. Upload, send, download, delete, restore, reconnect, and refresh actions expose success or recoverable error feedback. Toasts use a shared bottom offset that accounts for mobile navigation and safe-area insets.

## Accessibility

- Every destination has one programmatically focusable primary heading.
- Route changes update `aria-current` on both navigation variants.
- Focus moves to the destination heading after direct navigation actions.
- Browser history restoration does not force focus movement.
- Touch targets remain at least 44px.
- Focus rings remain visible.
- Reduced-motion preferences disable nonessential route, toolbar, drawer, and toast motion.
- Status is communicated through text and shape in addition to color.
- Hidden pages and controls are removed from the accessibility and pointer interaction trees.

## Error Handling

- Unknown hashes resolve to Transfer.
- Session expiry keeps the current hash and opens the authentication overlay.
- WebSocket interruption displays a compact status on Transfer and detailed recovery controls on Manage.
- Route switching never starts or closes the WebSocket connection.
- Failed file or timeline loads remain within their owning destination and expose retry feedback.

## Testing

### Frontend Contract Tests

Verify:

- Three navigation destinations exist in desktop and mobile markup.
- Hash-to-page mappings and route metadata are centralized.
- Exactly one route page is active.
- Inactive route pages are hidden.
- The batch toolbar disables pointer interaction when inactive.
- Mobile bottom offsets use shared tokens.
- The duplicate 430px media blocks are consolidated.

### Browser E2E

At desktop and 390px mobile widths, verify:

- Each navigation destination can be opened.
- Browser back and forward restore the previous destination.
- Breadcrumb, title, active navigation, and focus follow the route.
- The Manage destination is clickable with zero files selected.
- Empty Files state links to Transfer and opens file selection.
- Selecting and clearing a file controls batch toolbar visibility and pointer behavior.
- Transfer timeline and composer are not obscured by fixed navigation.
- No horizontal page overflow occurs.

### Regression Verification

Run:

- Full pytest suite.
- Frontend contract tests.
- Chromium E2E tests.
- Python compile check.

Visual acceptance uses the model's built-in image understanding capability according to the recorded project preference.

## Delivery Sequence

1. Add failing tests for routing, toolbar interaction, and responsive contracts.
2. Build route page semantics and the navigation controller.
3. Recompose Transfer around timeline and composer.
4. Move the library into Files and management panels into Manage.
5. Consolidate responsive CSS and fixed-element offsets.
6. Complete accessibility and interaction feedback.
7. Run focused and full verification.
