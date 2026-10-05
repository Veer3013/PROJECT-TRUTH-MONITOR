# Fixture-backed demo walkthrough

This walkthrough uses only frozen synthetic data. It does not call a live government server and does not describe any real project, registration, developer, or source record.

The seed materializes the four trust scenarios so the dashboard can be demonstrated reliably. The automated tests exercise the capture/diff pipeline that produced equivalent records. Do not enable live HRERA for this walkthrough.

## 1. Prepare the local demo

From Windows PowerShell in the repository root:

```powershell
py -3.13 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m alembic upgrade head
python -m app.seed_demo
python -m uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000/login](http://127.0.0.1:8000/login).

Use `analyst@projecttruth.local` / `DemoAnalyst!2026` for monitoring, upload, and draft actions. Use `reviewer@projecttruth.local` / `DemoReviewer!2026` to record review decisions. These credentials are local-only.

## 2. Establish the evidence-first baseline

1. Open **Watchlists** and select the seeded Gurugram demo watchlist.
2. Confirm that Aster Heights Phase II, Lakeview Residences, Orion Greens Phase III, and Metro Square have distinct synthetic registrations.
3. Open **Source health**. The frozen fixture source is the reliable demo source; live HRERA should remain disabled.
4. Open a project passport and point out identity status, source counts, last verification, open alerts, conflicts, official facts, and separate confidence dimensions.
5. Open **Audit history** and confirm that actions are attributed to authenticated users rather than a submitted actor name.

Explain that a source failure never becomes "no change" and that a factual change links old/new snapshots and evidence.

## 3. Case A - completion extension

Project: **Aster Heights Phase II**  
Registration: `HRERA-DEMO-101`

1. Open the Aster **Project Truth Passport**. Confirm the current official completion value is 31 March 2028 and the alert remains in review.
2. Open **Change Ledger** and confirm:
   - Field: completion date.
   - Old value: 30 June 2027.
   - New value: 31 March 2028.
   - Deterministic magnitude: `+275 days`.
   - Old and new snapshot IDs.
   - Review required.
3. Open the **Evidence Card**. Confirm source class, source and capture dates, excerpts/locators or hashes, parser version, extraction method, limitations, and separate confidence dimensions.
4. Open **Project Truth Timeline** and confirm the tracking start, baseline, and extension remain in chronological order.
5. Sign in as reviewer, open **Alerts**, select the Aster alert, and record a reasoned decision. If later report-gate steps are required, either resolve this item or deliberately keep it unresolved to demonstrate the block.

Use the phrase "Completion date changed" or "Possible delay signal." Do not claim guaranteed delay or legal non-compliance.

To exercise the two-capture API pipeline rather than inspect the seeded state, run:

```powershell
python -m pytest -q tests/api/test_monitoring_flow.py::test_two_snapshots_create_plus_275_change_evidence_and_signal
```

That test uses `case_a_completion_old.html` and `case_a_completion_extended.html`; it does not use the network.

## 4. Case B - brochure mismatch

Project: **Lakeview Residences**  
Registration: `HRERA-DEMO-202`

The synthetic brochure claims six towers, 1,200 units, and possession in December 2027. The captured official fixture has five towers, 1,080 units, and completion in June 2028.

1. Open the project's **Documents** view. The seed provides synthetic document/version/extraction metadata and three page-linked comparison rows.
2. Confirm the three `CONFLICTING` values and the neutral wording: "Marketing claim conflicts with the currently captured official record."
3. Open **Source Conflict Center** and show every preserved value, source class, freshness, and review state.
4. Record a reviewer decision or keep the conflicts unresolved. Official display precedence must not erase brochure history.

The seeded document uses a fixture reference and is not a private downloadable PDF. To demonstrate the real file pipeline, upload an authorized local PDF:

1. Check the rights attestation and upload the PDF.
2. Open the uploaded document, run **Extract latest version**, then **Compare with official record**.
3. Upload a revised authorized PDF from the same workflow. It becomes V2 with its own hash; V1 remains listed and downloadable.
4. Confirm encrypted, corrupted, or unreadable files fail safely or remain in manual review and do not write official facts.

The current repository wires disabled OCR and LLM providers. Searchable PDFs use deterministic extraction; an image-only document remains `needs_review` unless a concrete provider is added in code and permitted by policy.

## 5. Case C - new phase and locality pulse

Project: **Orion Greens Phase III**  
Registration: `HRERA-DEMO-303`  
Location: Sector 79

1. Open **Project Phase Relationship Map**.
2. Confirm Phase I, Phase II, and Phase III retain separate registrations under the Orion Greens parent.
3. Open the timeline and locate the `NEW_PHASE_OR_REGISTRATION` event.
4. Open **Locality pulse** for Sector 79.
5. Confirm the monitored project/phase aggregate and developer/source summary.
6. Read the required disclaimer aloud: "Based only on projects in this monitored dataset - not the complete Gurugram market."

This event does not imply full market coverage, demand, price direction, or project quality.

## 6. Case D - source conflict and low confidence

Project: **Metro Square**  
Registration: `HRERA-DEMO-404`

The official HTML fixture has no completion date. Synthetic low-confidence OCR fixture data suggests 31 December 2029; a developer fixture claims June 2029.

1. Open the project profile and confirm the official completion value remains unknown/null.
2. Open **Source Conflict Center** and inspect the OCR and developer values separately.
3. Open the passport and point out reduced extraction/access confidence and the conflict state.
4. Confirm that there is no confirmed external completion alert.
5. In the review queue, choose an evidence-supported action and record a reason and note.
6. Open **Audit history** and confirm the authenticated reviewer and decision were recorded.

This case demonstrates that unknown remains unknown and low-confidence OCR never becomes an official fact.

## 7. Reports and approval gate

1. Open **Weekly digest** and compare successful, degraded, confirmed, review-required, status, new-phase, and conflict counts.
2. Open **Reports** and create a new draft with PDF, CSV, JSON, and `evidence` formats. The evidence format is the CSV manifest.
3. Attempt approval while an included high-impact required-review alert is unresolved. The API must return `409 unresolved_required_review`.
4. Confirm a viewer cannot download the internal draft. Resolve every required-review item as a reviewer, approve the report, and download the requested artifacts.
5. Confirm the evidence manifest includes alert/project IDs, old/new snapshot IDs, source or evidence reference, capture time, content hash, parser version, reviewer, and decision where available.
6. In the CSV, confirm formula-like cells are prefixed so a spreadsheet does not evaluate them.
7. Confirm the approved PDF is labelled approved and contains evidence references. Downloads are authenticated and no-store. A report returns `410` after its recorded expiry and `404` after explicit deletion.

The seeded draft report is dashboard metadata and does not guarantee that a generated artifact file exists. Create a new report through the application to demonstrate downloads.

## 8. Role and tenant checks

1. Sign in as `viewer@projecttruth.local` / `DemoViewer!2026`. Confirm mutation controls are absent or rejected.
2. Sign in as `analyst@projecttruth.local`. Confirm watchlist, refresh, upload, and draft actions are available but admin actions are not.
3. Sign in as `admin@projecttruth.local` / `DemoAdmin!2026`. Open members, source policy, retention, and deletion views.
4. A request for another organization's project or artifact must return a policy-safe not-found/forbidden response without leaking names, sources, evidence, paths, or identifiers.

The automated role, CSRF, and tenant checks are in `tests/api/test_auth_and_tenant.py`, `tests/api/test_organization_deletion.py`, and `tests/security/test_ssrf_and_exports.py`.

## 9. Failure behavior

The frozen regression fixtures cover:

- Source unavailable: last verified facts remain; the run is degraded.
- CAPTCHA/restricted response: collection stops and does not bypass the challenge.
- Layout change: parser validation fails and no factual alert is generated.
- Value removed: a review-required removal remains in history.
- Value changed then reverted: all values remain and `VALUE_REVERSED` is visible.
- Encrypted or corrupted PDF: extraction fails safely and creates no official facts.

Not every edge fixture is exposed as a seeded UI button. Run the full test suite to execute the regression paths:

```powershell
python -m pytest -q
```

Finish at **Source health** and **Audit history** so the audience sees that data uncertainty and operator actions remain visible.

## 10. Retention and deletion demonstration

Use a disposable database only. Organization deletion deactivates the seeded tenant and prevents continued normal use.

1. Sign in as admin and open `/settings/retention`.
2. Update the retention values and confirm an audit event is recorded.
3. Before deleting, create/upload disposable artifacts and note their authenticated URLs.
4. Enter the exact organization name `ProjectTruth Demo Advisory` and a reason, then submit deletion.
5. Confirm monitoring jobs/runs are canceled, projects/watchlists/documents/reports are soft-deleted, sources/policies are disabled, recognized private files are removed, and old artifact URLs fail.
6. A repeated authorized request returns the same tombstone and does not duplicate deletion work.

The current service retains immutable monitoring/audit history in the database. It is not a complete legal erasure/anonymization workflow, and no automatic age-based purge worker is included.
