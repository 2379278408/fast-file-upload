# Goal Contract: Fast File Upload Ongoing Refinement

## Goal

Complete a full refinement pass on `fast-file-upload`, verified by tests, local API checks, and preview review, while preserving the lightweight FastAPI plus static frontend architecture and producing a polished, compact, layout-stable UI.

## Completion Contract

This goal is complete only when:

- Front-end copy is shorter, clearer, and consistent with the quieter visual direction.
- UI layout is polished, responsive, and visually stable across the upload, controls, file library, and preview modal areas.
- Upload, listing, image preview, download, delete, and optional token flows are all rechecked with evidence.
- `pytest` passes.
- The preview site loads and reflects the current UI.
- Each iteration records findings and the next checkpoint.

## Evidence / Verification

Run from the repository root unless noted otherwise:

```bash
pytest
curl -s http://127.0.0.1:8083/api/health
curl -s -F "file=@tests/assets/sample-preview.svg;type=image/svg+xml" http://127.0.0.1:8083/api/upload
curl -s -F "file=@README.md;type=text/markdown" http://127.0.0.1:8083/api/upload
```

Preview verification uses the live preview URL and a manual check of upload area, controls, file cards, empty state, image preview modal, responsive layout, and token mode behavior.

## Scope and Constraints

The workflow may change:

- `web/**`
- `app/**`
- `tests/**`
- `README.md`
- `.monkeycode/docs/**`
- `.github/ISSUE_TEMPLATE/**`
- `AGENTS.md`

The workflow must preserve:

- optional `UPLOAD_TOKEN` access control
- current API route surface
- lightweight local deployment
- small, readable code paths over heavy framework expansion

## Context To Read First

- `README.md`
- `web/index.html`
- `app/main.py`
- `app/storage.py`
- `tests/test_app.py`
- `.monkeycode/docs/goal-iteration-log.md`

## Iteration Policy

Choose the next smallest checkpoint that improves one of these areas with verification: copy clarity, visual hierarchy, layout stability, preview behavior, upload flow reliability, token behavior, or regression coverage. Each round should focus on one UI or behavior layer, run evidence-based checks, record the result, and carry forward the next checkpoint.

## Blocked Stop Condition

Stop substantive work and record a blocker when:

- GitHub authentication is required for the next step and unavailable.
- Required tool support such as `/compress` is unavailable.
- A needed product decision cannot be inferred from the repository.
