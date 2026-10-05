# Product, legal, and marketplace boundaries

## What ProjectTruth Monitor does

ProjectTruth Monitor captures permitted public-source and user-authorized project material, preserves immutable history, detects deterministic field-level changes, shows evidence and separate confidence dimensions, routes uncertain or material items for human review, and produces traceable operational reports.

It is intended for research and operations analysts monitoring a curated set of under-construction Gurugram projects. Secondary users may include suppliers, contractors, due-diligence consultants, lenders, developer intelligence teams, and marketing/compliance teams.

## What it does not do

ProjectTruth Monitor is not:

- A property listing, discovery, recommendation, or ranking service.
- A broker or transaction intermediary.
- A title-verification or legal due-diligence authority.
- A valuation, price-prediction, investment-advice, or buy/sell decision system.
- A fraud, scam, or developer risk classifier.
- A guarantee of project completion, delivery timing, regulatory compliance, or data completeness.
- A complete Gurugram or India market dataset.
- A CAPTCHA-solving, paywall-bypass, or unauthorized scraping system.

The product does not automate credit denial, contractual action, legal conclusions, property purchases, or external client claims.

## Neutral language

Use observation language tied to evidence:

- "Completion date changed."
- "Possible delay signal."
- "Official and marketing sources conflict."
- "QPR-related flag observed."
- "Needs review."
- "Monitoring incomplete. Last verified on [timestamp]."

Do not state or imply:

- Fraud or scam.
- Guaranteed delay or guaranteed completion.
- Legal non-compliance, except when reproducing exact official wording with its source and limitation.
- Which project a user should buy.
- That data is complete, real-time, or 100 percent accurate.

An official value may receive display precedence, but the interface preserves conflicting developer, brochure, OCR, and reviewed-correction values.

## Human review boundary

Human review is required for ambiguous identity, merge proposals, material completion or status changes, legal notices, OCR-derived core fields, brochure conflicts, value removal, conflicting sources, and high-impact client-facing reports.

Review actions are confirm, dismiss, correct, request another source, mark expected, or keep unresolved. The authenticated reviewer, decision, reason, note, timestamp, before/after references, and reversibility are audited.

Rule-driven business-impact suggestions are review prompts, not decisions. For example, a completion extension may prompt an adviser to review a client brief or a supplier to review a delivery schedule. It does not direct a purchase, credit, legal, or contract outcome.

## AI boundary

Core identity, normalization, dates, counts, snapshots, diffs, signals, evidence links, freshness, confidence bands, deduplication, and reports are deterministic.

When a concrete optional OCR or schema-constrained LLM provider is wired and policy-approved, it may identify candidate claims in unstructured authorized documents. Its output remains a claim with method, confidence, page locator, and review state. It cannot overwrite an official fact or directly create a confirmed external alert. The current repository wires disabled provider implementations only.

The application remains useful when every AI provider is disabled.

## Suproc boundary

ProjectTruth Monitor is a standalone service that may be listed on the Suproc Marketplace. It complements business discovery, workspaces, projects, procurement, RFQ workflows, and context-aware execution without recreating them.

This MVP does not implement:

- A replacement Suproc Workspace, CRM, ERP, supplier network, or procurement system.
- Undocumented Suproc APIs, authentication, billing, or agent orchestration.
- Direct Suproc integration without an official contract.

Future integration may use secure links, access-controlled PDF/CSV/JSON export, or a documented webhook after a verified contract. No unverified integration is implied.

## Locality pulse boundary

Every locality aggregate displays this statement:

> Based only on projects in this monitored dataset - not the complete Gurugram market.

It reports monitored project counts and reviewed observations. It does not represent market share, pricing, investment performance, project quality, or total registration activity.
