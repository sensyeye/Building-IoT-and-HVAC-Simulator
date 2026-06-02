# Sensgreen Sensor Simulator — UI & UX Context

This document defines the UI and UX rules for the **Sensgreen Sensor Simulator**
web interface. It is the single source of truth for layout, styling, navigation,
and workflow conventions across all pages. Implementers (and Copilot) must
treat this file the same way they treat `COPILOT_CONTEXT.md`: read it first,
follow it strictly, and update it before changing the rules.

> **Status:** Specification only. Do **not** implement any UI code based on
> this document yet. Implementation will be scoped in a later task.

---

## 1. Purpose & Audience

The web UI is an **internal tool** used by the Sensgreen team to:

- Configure realistic sensor simulations for demo accounts and prospects.
- Generate historical telemetry datasets for a chosen date range.
- Validate that generated data is physically and temporally consistent.
- Fix configuration issues surfaced by the validator.
- Export CSV bundles for ingestion into the Sensgreen platform.
- Start live MQTT publishing for real-time demos.

Primary users:

- **Solution engineers** preparing tailored demos.
- **Sales engineers** running live or historical walkthroughs.
- **Backend engineers** debugging the simulator and validator.

The UI is **not** customer-facing. It does not need marketing polish, but it
must look like a professional internal SaaS admin panel and feel fast to use.

---

## 2. UX Goals

- **Easy to understand.** A new team member should reach a useful state in
  under five minutes without reading docs.
- **Clean and modern.** Minimal chrome, generous spacing, neutral palette.
- **Professional SaaS admin panel feel.** Think Linear / Stripe Dashboard /
  Vercel admin, not consumer apps.
- **Not over-designed.** No animations, gradients, or illustrations beyond
  what is required to communicate state.
- **Useful for technical and sales-demo preparation.** Surfaces the
  information both audiences need without forcing them through wizards.
- **Fast to use during internal workflows.** Keyboard-friendly, low click
  count, forms remember last-used values where it makes sense.

---

## 3. Recommended Stack (MVP)

| Layer            | Choice                                  |
| ---------------- | --------------------------------------- |
| Web framework    | FastAPI                                 |
| Templating       | Jinja2                                  |
| Dynamic UI       | HTMX (partial swaps, polling)           |
| Styling          | Tailwind CSS via CDN                    |
| Charts           | Plotly.js **or** Chart.js (pick one and stick with it) |
| Icons            | Heroicons (inline SVG)                  |
| JS framework     | **None.** No React / Vue / Svelte in MVP. |

Rationale: HTMX + Jinja keeps the surface area tiny, lets the existing
Python services stay authoritative, and avoids a separate frontend build.

---

## 4. Architectural Boundaries (Hard Rules)

The UI is a **thin presentation layer**. It must respect the following
boundaries — violations are bugs, not stylistic choices.

- The UI **must not** generate sensor data directly.
- The UI **must** call backend services for every domain action.
- Simulation logic stays in the **simulator engine** (`simulator/sensors`,
  `simulator/services`).
- Validation logic stays in the **validator services**
  (`simulator/validators`).
- MQTT logic stays in the **MQTT service**
  (`simulator/integrations` / publisher).
- Templates stay simple and readable. No business logic in Jinja.
- No large JavaScript frameworks. HTMX + small inline scripts only.
- Use reusable template components (partials) where possible:
  cards, tables, badges, form rows, modals, job-status panels.
- Every form field with a technical name must have a short helper
  description underneath.
- Every dangerous action (delete, overwrite, publish live) must require an
  explicit confirmation step.
- Every long-running action must surface job status and progress (queued,
  running, succeeded, failed, with a timestamp and, where possible, a
  percentage or row count).

---

## 5. Layout

### 5.1 Global frame

```
┌─────────────────────────────────────────────────────────────────────┐
│  Header: project name · environment · status pills · user menu      │
├──────────────┬──────────────────────────────────────────────────────┤
│              │                                                      │
│   Sidebar    │                  Main content                        │
│ (navigation) │                  (cards / tables)                    │
│              │                                                      │
└──────────────┴──────────────────────────────────────────────────────┘
```

- **Left sidebar:** primary navigation, fixed width (~240 px), collapsible
  on narrow viewports. Active item highlighted with a left accent bar.
- **Top header:** project name, current environment, global status pills
  (last validation score, MQTT connection, active job), user menu on the
  right.
- **Main content:** card-based, max-width container (~1200 px), generous
  padding (24 px), 16 px gap between cards.

### 5.2 Cards

- White background, subtle border (`border-gray-200`), rounded
  (`rounded-2xl`), small shadow (`shadow-sm`).
- Header row: title (semibold) + optional action buttons on the right.
- Body: form rows, table, or chart.
- Footer (optional): primary/secondary actions, right-aligned.

### 5.3 Step-by-step workflows

Long workflows (e.g. Create Project, Simulation Runner) use a horizontal
stepper at the top of the page:

```
( 1 Config ) ─▶ ( 2 Generate ) ─▶ ( 3 Validate ) ─▶ ( 4 Fix ) ─▶ ( 5 Export / Publish )
```

The current step is highlighted; completed steps show a check; future
steps are gray and not clickable until prerequisites are satisfied.

---

## 6. Navigation

Primary navigation, in this exact order:

1. **Dashboard**
2. **Projects**
3. **Building Setup**
4. **Zones & Rooms**
5. **Devices**
6. **Scenarios**
7. **Simulation Runner**
8. **Validation Report**
9. **Live MQTT Monitor**
10. **Exports**

Items 3–10 are scoped to the currently selected project. If no project is
selected, they are disabled with a tooltip pointing the user to the
Projects page.

---

## 7. Core Workflow

The whole product is built around one loop:

```
Config  →  Generate  →  Validate  →  Fix  →  Export or Publish
```

Every page should make it obvious where the user is in this loop and what
the next sensible action is. The Dashboard surfaces the loop status for
each project; the Simulation Runner enforces it.

---

## 8. Visual Language

### 8.1 Typography

- System font stack (Tailwind default).
- Page title: `text-2xl font-semibold`.
- Card title: `text-lg font-semibold`.
- Body: `text-sm` for tables and dense forms, `text-base` elsewhere.
- Labels: `text-sm font-medium text-gray-700`.
- Helper text: `text-xs text-gray-500`.

Labels should be **large and readable**. Avoid all-caps micro labels.

### 8.2 Colors (status semantics)

These colors carry meaning and must be used consistently. Do **not** use
them decoratively.

| Meaning                   | Color  | Tailwind hint                    |
| ------------------------- | ------ | -------------------------------- |
| Healthy / success         | Green  | `bg-green-100 text-green-800`    |
| Warning / degraded        | Yellow | `bg-yellow-100 text-yellow-800`  |
| Critical / error / failed | Red    | `bg-red-100 text-red-800`        |
| Running / informational   | Blue   | `bg-blue-100 text-blue-800`      |
| Disabled / inactive       | Gray   | `bg-gray-100 text-gray-600`      |

Neutral chrome (borders, page background, secondary text) uses Tailwind
`gray-*` shades only.

### 8.3 Status badges

A reusable `badge` partial renders a colored pill. It is the only sanctioned
way to show:

- Validation status (`healthy`, `warnings`, `critical`).
- MQTT connection (`connected`, `connecting`, `disconnected`, `error`).
- Simulation jobs (`queued`, `running`, `succeeded`, `failed`, `canceled`).
- Device status (`enabled`, `disabled`, `error`).

### 8.4 Tables

- Sticky header, zebra rows off, row hover `bg-gray-50`.
- Filter row above the table: text search + relevant dropdowns
  (zone, sensor type, status).
- Pagination only when row count exceeds ~50.
- Row actions in the rightmost column as icon buttons with tooltips.

### 8.5 Forms

- Single column by default; two columns only on wide forms where pairing
  is meaningful (e.g. `start_date` / `end_date`).
- Each field: label, control, helper text, error text (when invalid).
- Primary action right-aligned in the card footer; secondary action to its
  left as a ghost button. Cancel/Back uses a tertiary text link.
- Destructive actions are red and require a confirmation modal that
  re-states what will be lost.

---

## 9. Pages to Build in MVP

> Scope note: the following pages are the MVP surface. Anything outside
> this list (audit log, RBAC, multi-tenant settings, theming) is out of
> scope until the MVP ships.

### 9.1 Project Dashboard

- Grid of project cards.
- Each card shows: project name, building type, total area, device count,
  last validation score (badge), last run status (badge), last run
  timestamp.
- Primary action on each card: **Open**.
- Top-right action: **New Project**.

### 9.2 Create Project

- Single-card form.
- Fields: project name, building type, city, timezone, total area (m²),
  number of floors, demo depth (`light` / `standard` / `deep`).
- Helper text under each field explaining what it influences (e.g.
  timezone affects scenario scheduling, demo depth affects device count).
- Submit creates the project and routes to **Building Setup**.

### 9.3 Building Setup

- Card 1: Building metadata (name, address, building type, year built).
- Card 2: Area & floor information (total area, per-floor area table).
- Card 3: Use-case selection (checkboxes for IAQ, energy, occupancy).
- Card 4: Recommended device summary (read-only counts derived from the
  selections, with a link to the Device Deployment Editor to apply them).

### 9.4 Device Deployment Editor

- Filterable device table: device ID, sensor type, model, zone, room,
  reporting frequency, enabled status.
- Toolbar: **Add Device**, **Auto-generate Recommended Devices**,
  **Bulk Enable/Disable**.
- Row actions: edit (modal), delete (with confirmation).
- Add/Edit modal shows all device fields with helper text and validation.

### 9.5 Scenario Editor

- Card list of demo scenarios for the current project.
- Add/Edit form: scenario type (e.g. `meeting_room_poor_ventilation`,
  `after_hours_energy_waste`, `cleaning_voc_spike`), target zone, start
  time, end time, severity (`mild` / `moderate` / `severe`).
- Read-only **Expected Effects** panel summarizing which metrics will be
  perturbed and by how much, sourced from the scenario engine.

### 9.6 Simulation Runner

- Step 1: Historical generation form — start date, end date, output
  folder, optional notes.
- Step 2: **Run** button. Submits a job; UI polls job status via HTMX.
- Step 3: Job status panel — state badge, started/finished timestamps,
  rows generated, log tail (last ~20 lines), link to the resulting
  Validation Report and Exports.

### 9.7 Validation Report

- Header: overall score (large numeric), overall status badge.
- Category scores (physical, temporal, correlation, hierarchy, scenario,
  demo quality) as a horizontal bar list.
- Findings grouped by severity (critical → warning → info), each with
  rule ID, message, affected device/zone, and a **Suggested Fix** link
  back to the relevant editor page.
- Scenario detectability section: per-scenario badge showing whether the
  validator could detect the injected anomaly.

### 9.8 Live MQTT Monitor

- Card 1: MQTT config (broker host, port, TLS, username, topic prefix).
  Credentials read from env vars only — UI shows masked values and never
  accepts plaintext passwords.
- Card 2: Dry-run preview — render the next N payloads without
  publishing.
- Card 3: Controls — **Start Publishing**, **Stop Publishing** (both
  require confirmation when pointing at non-local brokers).
- Card 4: Recent payload samples (live-updating list, last ~50 messages).
- Card 5: Broker status badge (connected / connecting / disconnected /
  error) with last error message when applicable.

---

## 10. Component Inventory (Jinja Partials)

These partials should exist in `templates/_partials/` and be reused
everywhere instead of inlining markup:

- `card.html` — card frame with title slot and body slot.
- `badge.html` — status pill (takes `variant` and `label`).
- `stepper.html` — horizontal workflow stepper.
- `form_row.html` — label + control + helper + error.
- `table.html` — table frame with filter slot and rows slot.
- `modal.html` — confirmation / edit modal.
- `job_status.html` — job state panel with HTMX polling hook.
- `empty_state.html` — illustration-free empty state with a CTA.

---

## 11. Accessibility & Internationalization

- All interactive elements are reachable by keyboard; focus rings are
  never disabled.
- Color is never the sole carrier of meaning — every badge has a text
  label.
- Copy is English-only in the MVP. Strings live in templates, not in
  Python, to keep future i18n straightforward.

---

## 12. Out of Scope for MVP

- Authentication / RBAC (assumed to run behind an internal SSO proxy).
- Multi-tenant org switching.
- Theming / dark mode.
- Mobile layouts beyond "doesn't break below 1024 px".
- Customer-facing branding.

---

## 13. Change Control

Update this file **before** changing UI conventions in code. PRs that
introduce new pages, new status semantics, or new component partials must
update the relevant section here in the same change.
