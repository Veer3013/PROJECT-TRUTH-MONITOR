PROJECTTRUTH MONITOR FROZEN FIXTURE MANIFEST

All names, registrations, developers, URLs, dates, and claims in this directory are
synthetic. No fixture requires a live government server.

CASE A - COMPLETION EXTENSION
  html/case_a_completion_old.html
  html/case_a_completion_extended.html
  Expected change: 30 June 2027 to 31 March 2028; +275 days; evidence and review.

CASE B - BROCHURE MISMATCH
  html/case_b_official_record.html
  text/case_b_brochure_claims.txt
  text/revised_brochure_claims.txt
  Expected: three conflicts; page-linked claims; official and marketing layers separate.

CASE C - NEW PHASE / LOCALITY PULSE
  html/case_c_new_phase.html
  html/case_c_multiple_phases.html
  html/case_c_phase_baseline.html
  Expected: new registration event; parent relationship; Sector 79 monitored-data increment.

CASE D - SOURCE CONFLICT / LOW CONFIDENCE
  html/case_d_official_missing_completion.html
  html/case_d_developer_claim.html
  text/case_d_low_confidence_ocr.txt
  Expected: conflict and needs-review; no confirmed external alert.

EDGE CASES
  html/clean_project_page.html
  html/project_marked_lapsed.html
  html/qpr_flag_added.html
  html/qpr_flag_removed.html
  html/similar_project_names.html
  html/source_unavailable.html
  html/captcha_restricted.html
  html/layout_changed.html
  html/value_removed_old.html
  html/value_removed_new.html
  html/value_reverted_1.html through value_reverted_3.html
  csv/projects_valid.csv
  csv/projects_duplicate_rows.csv
  csv/projects_row_errors.csv
  csv/formula_injection_values.csv
  text/encrypted_pdf_metadata.txt
  text/corrupted_upload_metadata.txt
  text/tenant_access_attempt.txt
  pdf/brochure_text_fixture.pdf

The committed PDF is a minimal, synthetic, text-based fixture. Tests derive an encrypted
copy in memory to verify fail-closed handling without storing a password-protected artifact.
