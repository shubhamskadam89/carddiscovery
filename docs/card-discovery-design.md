# Automatic Credit Card Discovery via Email — Design Exploration

Status: proposal for review · Date: 2026-11-13

> **Update 2026-11-16:** the current architecture is [architecture.md](architecture.md) (server-side workers, card picker first). [mobile-design.md](mobile-design.md) describes the earlier on-device option. The server-side processing parts of this doc (§3 architecture, §4 workflow, §9 performance, §10 schema, §11 API, §12 security) are replaced by it. The problem analysis, filtering strategy, extraction, scoring and the test matrix below still apply.

---

## 0. TL;DR

- **Treat it as entity discovery from evidence, not email parsing.** Each email can add *evidence* about a *card*. Cards come from combining evidence, and each card gets two separate scores: ownership confidence and status confidence.
- **Never read the whole inbox.** We use provider-side search (Gmail `q`, Graph `$search`/`$filter`) driven by a **bank sender registry** plus **one query per evidence type**. That shrinks a 100k-message inbox to a few hundred or a few thousand candidates before we download any message body.
- **Read metadata first and fetch bodies only when needed.** Headers plus the snippet classify most candidates. Full bodies are fetched only when extraction needs them. PDF attachments are almost never opened.
- **Use a hybrid extractor.** Deterministic per-bank templates handle the common emails. An LLM handles the long tail. Every template we learn is cached, so the LLM cost per user falls as we see more users.
- **Fetch rare, high-value evidence exhaustively; sample frequent, low-value evidence.** Fetch every approval, welcome, dispatch, statement, annual-fee, renewal, closure and upgrade email. Only sample transaction alerts. Dormant cards are found through the rare kinds, so this is where recall comes from.
- **Status is inferred from evidence, never from recency alone.** "No recent email" means **dormant**, never **closed**. A card is closed only when there is explicit evidence of closure.
- **Everything uncertain goes to a "please confirm" list.** Nothing is silently dropped.
- **Architecture is a modular monolith plus a job queue plus Postgres.** No microservices and no stream processor.
- **Biggest non-technical risk:** `gmail.readonly` is a Google **restricted scope**, so it needs OAuth verification and an annual third-party security assessment. Start that process in parallel with engineering, because it can take longer than building the system.

---

## 1. Problem decomposition

| # | Sub-problem | Core question | Hard part |
|---|---|---|---|
| P1 | Connection | How do we get read-only, revocable access to one or more mailboxes? | Restricted scopes, token security, multiple accounts per user |
| P2 | Candidate retrieval | Which messages could be card evidence? | Recall over 5–15 years of mail without fetching everything |
| P3 | Classification | What *kind* of evidence is this message, if any? | Promotions look like statements; offers mention real cards |
| P4 | Extraction | Bank, product, variant, last4, holder name, dates | Template variety across banks and years; image-only emails |
| P5 | Authenticity | Did the bank really send it, to this user, about this user? | Forwards, phishing, family members' cards, add-ons |
| P6 | Entity resolution | Which pieces of evidence describe the same physical card? | Missing last4, renamed products, sender changes, reissues |
| P7 | Lifecycle inference | Is the card active, dormant, closed, replaced or only applied for? | Absence of evidence ≠ evidence of absence |
| P8 | Scoring and UX | How sure are we, and what do we ask the user? | Calibration without labelled data on day one |
| P9 | Sync | How do we stay current cheaply? | History-cursor expiry, push notification renewal |
| P10 | Privacy | How do we keep the risk small? | Store derived facts, not emails |

**Assumptions to challenge:**

1. *"Email is the source of truth for which cards a user owns."* It is not. Users delete mail, change addresses and filter bank mail. A **credit bureau report** lists every open card account, including dormant ones, but has no product name or last4. Email gives us product and last4. We recommend adding an optional bureau check in phase 2 to answer "we found 3 HDFC cards; the bureau says you have 4" (§10.4). Email discovery should not be expected to reach 100% recall alone.
2. *"We must process every email."* We do not. Bank mail is a tiny, sender-identifiable slice of an inbox.
3. *"Statements prove a card is active."* Many issuers send no statement in a cycle with no transactions and no balance. A dormant card can go years without one. Rare lifecycle mail (annual fee, renewal or reissue, reward-point expiry, regulatory notices) is how dormant cards show up.
4. *"Last4 identifies a card."* Mostly, but last4 changes on upgrade or loss replacement, sometimes stays the same on a variant change, and two cards at one bank can collide (about 1 in 10,000). It is the main key, not an infallible one.
5. *"An LLM can just read the emails."* It can, but it costs too much at inbox scale, isn't deterministic, adds privacy exposure, and can invent a last4. It belongs on the long tail, with grounding checks.

---

## 2. Approach comparison

### 2.1 Mail access

| Option | Verdict | Why |
|---|---|---|
| **Gmail API, `gmail.readonly`** | ✅ Primary for Gmail | Server-side search, `history` incremental sync and Pub/Sub push. Restricted scope: needs verification and a security assessment. |
| Gmail API, `gmail.metadata` | ❌ | Also restricted, **doesn't support the `q` search parameter**, and gives no bodies. Not enough for extraction, and no cheaper to get approved. |
| **Microsoft Graph, `Mail.Read` (delegated) + `offline_access`** | ✅ Primary for Outlook/Hotmail/M365 | KQL `$search`, `$filter` on dates, per-folder `delta`, change-notification webhooks. |
| IMAP / app passwords | ❌ | Users would hand over full-access credentials, there's no scoping, Gmail discourages it, and it hurts trust. Possible later fallback for other providers using OAuth-IMAP (Yahoo etc.). |
| Forwarding rule to our ingest address | ❌ as primary | Least privilege, but **sees no history**, which breaks the core requirement. Could be an opt-in "future mail only" mode. |
| Browser extension / on-device | ❌ for v1 | Strong for privacy, but heavy for distribution and support, and we can't run background sync. |
| Bureau report (complement) | ➕ Phase 2 | Authoritative list of accounts but no product/last4. Good for reconciliation (§10.4). |

### 2.2 Narrowing what we read

| Technique | Use | Notes |
|---|---|---|
| **Sender registry** (bank → domains/addresses, versioned) | Primary filter | Handles case 23 (sender changes). Kept in data, not code. |
| **Typed search queries** (sender × subject keywords per evidence type) | Primary | The query itself pre-classifies, so we can treat each type differently (exhaustive vs sampled). |
| Date windows (`after:`/`before:`) | Sampling and phasing | Recent-first quick pass, then all-time; quarterly buckets for alert sampling. |
| Keyword sweep for unknown senders (`"credit card" (approved OR statement OR "ending")`) | Background | Catches banks not yet in the registry (co-brand issuers, new banks) and feeds the registry review queue. |
| Gmail category (`category:promotions`) | **Feature, not filter** | Statements sometimes land in Promotions; offers land in Updates. |
| Attachment filename (`filename:pdf`) | Feature | Filenames often carry last4 or the card name. No download needed. |

### 2.3 Classification and extraction

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| Pure deterministic rules (per-bank regex templates) | Fast, free, explainable, deterministic | Long tail; templates drift over 10 years; one rule set per bank | ✅ First tier |
| Pure LLM on every candidate | Best coverage, least engineering | Cost, latency, privacy, can invent last4 | ❌ |
| Trained classifier (e.g. gradient boosting on subject/sender n-grams) | Cheap at inference | **No labelled data on day one** | ⏳ Phase 3, trained on accumulated LLM and user-confirmed labels |
| **Hybrid: rules → LLM fallback → template learning** | Common cases cheap and deterministic, long tail covered, cost falls with scale | Two code paths to maintain | ✅ **Chosen** |

**Template learning:** bank mail is generated from templates. We fingerprint each candidate as `(bank, normalised subject skeleton)`, where digits, amounts, dates and names become placeholders. For example, `Your {CARD_PRODUCT} Credit Card Statement for {MONTH} {YEAR}`. A fingerprint seen for the first time goes to the LLM once. Its classification, and if it holds steady a proposed extraction rule, is cached **globally** (it contains no user data, only the skeleton). Later users with that template never reach the LLM. A few hundred templates per major bank cover nearly all of the volume.

### 2.4 PDF statements

- **Default: don't open them.** Statement email bodies and subjects nearly always carry the bank, product and last4 (and attachment filenames often do too). Downloading and parsing PDFs costs bandwidth, CPU and privacy exposure.
- Many issuers (especially Indian banks) **password-protect statement PDFs** with values derived from the cardholder's name and date of birth. Opening them means asking for or deriving personal data. That isn't worth it for discovery.
- **Exception:** only when a statement email from a known bank yields no last4 and no product from body, subject or filename, and the user agrees to "deep scan". This is phase 2.

---

## 3. Recommended architecture

```
                ┌──────────────────────────── API (REST + SSE) ─────────────────────────────┐
                │  connections · discovery runs · cards · decisions · data deletion          │
                └───────────────┬──────────────────────────────────────────────┬────────────┘
                                │ enqueue                                      │ read
 ┌──────────────┐   ┌───────────▼───────────┐                         ┌────────▼─────────┐
 │ OAuth broker │──▶│  Job queue (Postgres  │                         │    Postgres       │
 │ + token vault│   │  SKIP LOCKED / SQS)   │                         │ cards, evidence,  │
 │ (KMS envelope)│  └───────────┬───────────┘                         │ processed msgs,   │
 └──────────────┘               │                                     │ registry, templates│
                     ┌──────────▼──────────────────────────────────┐  └────────▲─────────┘
                     │ Discovery worker (one codebase, stages =     │           │
                     │ modules, per-mailbox concurrency limiter)    │───────────┘
                     │                                              │
                     │ 1 Provider adapter (Gmail | Graph)           │
                     │ 2 Query planner (registry → typed queries)   │
                     │ 3 Candidate lister + dedupe (processed_msgs) │
                     │ 4 Metadata fetch + authenticity check        │
                     │ 5 Classifier (template cache → rules → LLM)  │
                     │ 6 Extractor (rules → LLM w/ grounding)       │
                     │ 7 Entity resolver                            │
                     │ 8 Lifecycle + confidence engine              │
                     │ 9 Persist + emit progress                    │
                     └──────────────────────────────────────────────┘
  Push (Gmail Pub/Sub, Graph webhooks) ──▶ incremental-sync jobs ──▶ same pipeline
```

**Why a modular monolith?** Every stage is I/O-bound on one mailbox's rate limit, so splitting stages into services adds network hops and failure modes without adding throughput. Workers scale horizontally by mailbox. *Alternatives:* microservices per stage (rejected: operating cost, no throughput gain); Kafka/streaming (rejected: volumes are small per user, and a durable job queue with idempotent stages is enough); serverless functions (viable, but long-running paged syncs and per-mailbox concurrency limits are awkward). *Trade-off:* we must keep module boundaries clean so the extractor can be split out later if LLM workloads need their own scaling.

**Extensibility points (no pipeline rewrites):**

| To add | You change |
|---|---|
| A bank | `bank_senders` rows + optional template rules. The LLM fallback covers it on day one. |
| A template | `email_templates` row (auto-proposed by template learning, approved by a human) |
| A product alias | `card_aliases` row mapping to the existing card catalogue |
| An email provider | One `MailProvider` adapter: `search`, `getMetadata`, `getBody`, `changesSince`, `subscribe` |
| An evidence type | Enum value + weight row + query template |

Queries are written once as a provider-neutral object (`senders`, `subjectTerms`, `bodyTerms`, `dateRange`) and compiled into Gmail `q` or Graph KQL by each adapter.

---

## 4. End-to-end discovery workflow

**Phase A — Connect (≈5 s).** OAuth consent (read-only) → refresh token encrypted in the vault → store the mailbox address and its aliases (Gmail `sendAs`, Graph `proxyAddresses` where available) for "addressed to user" checks → start a discovery run.

**Phase B — Quick discovery (goal: first card in under 10 s, quick pass done in under 45 s).**
1. Query planner builds **typed, high-value queries** for known bank senders over the **last 24 months**: statements, welcome, dispatch/activation, annual fee, renewal/reissue.
2. List IDs (500 per page) → drop IDs already in `processed_messages` → batch-fetch **metadata** (headers + snippet).
3. Classify from subject, snippet and sender using the template cache, then rules. Extract last4/product from the snippet where possible.
4. Resolve entities → score → stream cards to the UI as they appear (SSE).

**Phase C — Full historical discovery (goal: under 3 min at P95 for a 100k-message inbox).**
1. The same typed high-value queries over **all time** (no date filter), so we see approval and welcome mail from years ago.
2. Lifecycle queries: closure, upgrade/conversion, replacement, inactivity notices, points expiry, limit change.
3. Negative-evidence queries: application received/in-progress/rejected, and promotional offers from bank senders. These only shape *how* cards are labelled, never whether one is created.
4. **Stratified alert sampling:** for each (bank, quarter), fetch up to *K* = 10 transaction-alert metadata records. If a sample shows a last4 not seen before, or volume is high, widen the sample for that quarter. This finds cards that only ever produce alerts (case 7) without fetching thousands of alerts.

**Phase D — Background sweep (minutes, low priority).** Keyword sweep for **unknown senders**, plus body fetches for candidates whose metadata was inconclusive, plus the LLM fallback. Results go to the "needs review" list, and unknown bank-like senders go to the registry review queue.

**Phase E — Steady state.** Gmail `users.watch` → Pub/Sub → `history.list` from the stored `historyId`. Graph change notifications → `delta`. Each new message goes through the same pipeline (one metadata fetch plus a template-cache hit, usually under 1 s). A daily reconciliation job renews subscriptions and runs a light 7-day query to catch any missed pushes.

**User review.** Cards appear in three groups: **Your cards** (confirmed or high confidence), **Please confirm** (medium confidence, add-ons, possibly someone else's, ambiguous merges), and **History** (closed/replaced). Every card shows *why* we think so ("Statement Mar 2021, Annual fee notice Jan 2024").

---

## 5. Email filtering strategy

### 5.1 Sender registry

```
bank_senders(bank_id, match_type[domain|address|regex], value, valid_from, valid_to,
             dkim_domain, trust[official|partner|unknown], notes)
```
- Seeded by hand for supported issuers (each issuer typically uses several sending domains: alerts, statements, marketing and partner ESPs). **Each address must be checked against real mail; don't trust lists copied from the web.**
- `valid_from/valid_to` covers sender migrations (case 23). Old domains stay in the registry indefinitely because historical mail still comes from them.
- The background sweep proposes new senders when a message from an unregistered domain has card-like content *and* passes DKIM for that domain. A human approves registry changes.

### 5.2 Query families (illustrative Gmail syntax; Graph has KQL equivalents)

| Family | Example | Fetch policy |
|---|---|---|
| Statement | `from:({bank senders}) subject:(statement) ` | Exhaustive |
| Onboarding | `from:(...) subject:(welcome OR approved OR dispatched OR delivered OR activated OR "card is on its way")` | Exhaustive |
| Lifecycle | `from:(...) subject:(renewal OR reissue OR "annual fee" OR upgrade OR upgraded OR replaced OR closure OR closed OR "reward points" expir*)` | Exhaustive |
| Application | `from:(...) subject:(application OR "application status" OR "unable to approve" OR regret)` | Exhaustive (negative evidence) |
| Alerts | `from:(...) subject:(transaction OR spent OR "used for" OR alert) after:Q before:Q+1` | Stratified sample |
| Payments | `from:(...) subject:(payment received OR due OR reminder)` | Sampled |
| Unknown-sender sweep | `"credit card" (statement OR approved OR "ending in" OR "card no") -from:({known})` | Background, capped |
| OTPs | `subject:(OTP OR "one time password")` | **Excluded** (high volume, almost never names a product) |

Notes:
- Gmail query strings have practical length limits, so we split the sender list into chunks of about 20–30 senders per query and run chunks in parallel.
- Subject keywords are **recall aids, not truth**. Classification always re-checks the actual content.
- Searching for last4 is unreliable (`XX1234` tokenisation), so we never use it to de-duplicate on the server.

### 5.3 Authenticity gate (applied before anything counts as ownership evidence)

1. **Sender trust:** `Authentication-Results` shows DKIM `pass` with `d=` matching a registry `dkim_domain`. Failing or missing DKIM (forwards, very old mail, spoofing) → evidence weight × 0.3.
2. **Addressed to user:** `To`/`Delivered-To` matches one of the user's connected addresses or aliases.
3. **Forward detection:** subject starts with `Fwd:`/`FW:`, sender is not the bank, or the body has a forwarded-message header → classified as `forwarded`. It never counts as ownership on its own (case 18).
4. **Holder name match:** if the email names a cardholder, fuzzy-match it against the user's profile names. A mismatch means "possibly someone else's card" (case 17), which goes to Please confirm.

---

## 6. Card extraction and entity resolution

### 6.1 Extraction

Fields extracted per message: `bank_id`, `product_raw`, `catalog_card_id?`, `variant?`, `network?`, `last4?`, `holder_name?` (compared then discarded; only a match/mismatch flag is kept), `evidence_type`, `event_date`, `addon_flag`, `referenced_cards[]` (for offers or upgrades that mention an existing card and a target card).

- **Masked-number normalisation:** `XX1234`, `xxxx xxxx xxxx 1234`, `ending 1234`, `ending with 1234`, `4xxxxxxxxxxx1234`, `**1234`, `Card No: 5xxx-xxxx-xxxx-1234` → `last4=1234` (+ `network` from the first digit if shown). Case 25.
- **Full-number guard:** any 13–19 digit run that passes the Luhn check is **redacted in memory before any further processing, logging or LLM call**, never stored, and counted in a metric.
- **Product normalisation:** `product_raw` → `card_aliases` (exact, then token-set fuzzy match against the existing card catalogue). Unknown → stored as raw with `catalog_card_id = null` and shown as "{Bank} credit card (variant unknown)". Case 24.
- **LLM fallback contract:** input = redacted subject, snippet and plain-text body (truncated to the relevant part), sender bank, list of known products for that bank. Output = strict JSON schema. **Grounding check:** every extracted `last4` and product string must appear verbatim (after normalisation) in the input text, or it is dropped. This stops invented card numbers.

### 6.2 Evidence → card resolution

The unit is **evidence**. Cards are clusters of evidence.

Resolution rules, in order:
1. **Same user + bank + last4** → same card (cases 21, 22, 25). If products differ over time, treat it as a variant change on the same number and keep a `product_history`. The latest strong evidence sets the current product.
2. **Explicit link evidence** ("your card ending 1234 has been upgraded to X, new card ending 5678") → `card_links(1234 → 5678, upgraded_to)`. The old card's status is `replaced` (case 11). Same for "replacement card" after loss (case 12).
3. **Evidence without last4** (an approval email that names only the product): attach to a *provisional card* keyed by (bank, product, approval month). Merge it into a last4 card of the same bank and product whose first evidence comes 0–90 days later. **If more than one candidate qualifies, don't merge**; show "possible duplicate" to the user.
4. **Expiry renewal** (same last4, new expiry) → same card. No new entity.
5. **Implicit replacement** (same bank and product, new last4 appears within 60 days of the old last4's final evidence, no explicit link): `possibly_replaced_by` link with low confidence → Please confirm. We do not auto-merge, because two concurrent cards of the same product are possible (case 4).
6. **Add-on cards** (case 16): if the user is the **primary** holder (statement lists the add-on) → record as an add-on of the primary card with `holder = other`; rewards accrue to the primary, so it is shown nested. If the user is the **add-on holder** (alerts to the user naming an add-on, the primary holder's name differs) → `ownership_role = addon_holder` → Please confirm.
7. **Cross-mailbox de-duplication:** the same RFC `Message-ID` across a user's connected mailboxes is processed once (case: multiple email addresses).
8. **Same-bank last4 collision:** if two distinct product histories with the same last4 overlap in time with interleaved usage, split them and flag for review. Rare.

### 6.3 Applications are separate entities

`applications(bank, product, applied_at, status[received|in_progress|approved|rejected|withdrawn])`. Application emails **never create a card** (cases 19, 20). An `approved` application becomes a provisional card. A provisional card becomes owned only when ownership evidence arrives (dispatch, welcome, activation, statement, alert) or the user confirms.

---

## 7. Confidence scoring

### 7.1 Two scores

- **Ownership confidence** `P(user personally holds or held this card)`
- **Status confidence** `P(current status label is right)`, with status ∈ {active, dormant, closed, replaced, application_only, unknown}

### 7.2 Ownership: capped noisy-OR

`own = 1 − Π_types (1 − w_t)`, where each **evidence type** contributes at most once (its strongest instance), so 60 statements don't turn into overconfidence and a single strong type can't saturate the score.

| Evidence type | Base w | Notes |
|---|---|---|
| Statement (DKIM pass, to user, name OK) | 0.95 | |
| Card dispatched / delivered / activated / PIN set | 0.95 | |
| Welcome email for a specific card | 0.90 | |
| Annual fee charged/waived, renewal/reissue, points expiry, inactivity notice naming last4 | 0.85 | Main dormant-card signal |
| Transaction alert | 0.85 | Shows usage; could be an add-on |
| Payment received / due reminder naming last4 | 0.75 | |
| Approval email | 0.70 | Approved ≠ issued; strengthened later by dispatch |
| Pre-approved upgrade/limit offer naming *existing* last4 | 0.50 for the referenced card, **0 for the offered card** | |
| Application received / in progress | 0 | Creates an application entity |
| Rejection | 0 (closes the application) | |
| Promotional / marketing | **0** | Case 14, 15 |

**Modifiers (multiplicative on w):** DKIM fail or missing ×0.3 · forwarded ×0.2 · holder name mismatch ×0.2 · not addressed to the user ×0.3 · add-on holder role → routed to review regardless of score.

**Decision thresholds** (user can always override):
- `own ≥ 0.90` → **Your cards** (auto-listed, user can remove)
- `0.40 ≤ own < 0.90` → **Please confirm**
- `own < 0.40` but at least one non-promotional piece of evidence → **Please confirm**, collapsed under "Low-evidence cards". Never discarded.
- Promotional-only → not shown as a card.

**Why noisy-OR with hand-set weights?** It is interpretable ("statement + annual fee → 0.99"), needs no training data, handles combined evidence naturally, and makes it easy to explain "why" in the UI. *Alternatives:* Bayesian network (too much modelling effort for v1); learned logistic regression (best long-term, needs labels); Dempster–Shafer (hard to explain). **Plan:** log every user confirm/reject as a label, then refit the weights as a calibrated logistic model after about 2–5k labelled cards and check the reliability curve.

### 7.3 Status inference

| Status | Rule | Typical confidence |
|---|---|---|
| `active` | Usage evidence (alert, statement with transactions, payment) in the last 6 months | 0.9+ |
| `dormant` | Ownership proven, no usage for over 6 months, **no closure evidence**. Lifecycle mail (annual fee, renewal) keeps confidence high that it is still open | 0.6–0.85 |
| `closed` | **Explicit** closure confirmation / no-dues / account-closed email, with no later evidence for that card | 0.9 |
| `possibly_closed` | Inactivity-closure warning (issuers must notify before closing inactive cards) with no later activity, or long silence after a closure-request email | 0.5–0.7 → Please confirm |
| `replaced` | Upgrade/replacement link to a newer card | 0.9 explicit / 0.5 implicit |
| `application_only` | Approved but no issuance or usage evidence | — |

**Absence of mail never moves a card to `closed`.** Silence only moves `active → dormant`.

---

## 8. Dormant, closed, upgraded — worked examples

| Situation | Evidence seen | Outcome |
|---|---|---|
| Card unused 3 years (case 2, 8) | Statements 2019–2022, annual fee notices 2023–2025 | `dormant`, own 0.99 — the fee notices prove it is still open |
| Approval 4 yrs ago only (case 3) | Approval 2022 | Provisional card, own 0.70 → Please confirm ("Do you still have this?") |
| Welcome only (case 9) | Welcome 2020 | own 0.90 → Your cards, status `dormant` (status confidence 0.5) |
| Last statement years ago (case 10) | Statements to 2019, nothing since | own 0.95, `dormant`, status confidence 0.5 → status shown as "not used recently — still open?" |
| Upgraded (case 11) | Regalia ••1234 statements; "upgraded to Regalia Gold, new card ••5678" | 1234 `replaced` → 5678 `active`. Upgrade on the same number → one card with product history |
| Expired/replaced (case 12) | "Replacement card dispatched", new last4 | Old card `replaced` |
| Closed (case 13) | "Your card account has been closed" 2021 | `closed`, shown under History, excluded from the active list |

---

## 9. Performance

### 9.1 Provider limits that shape the design (verify before launch)

| Provider | Relevant limits (as we understand them) |
|---|---|
| Gmail | Per-user quota around 250 units/s; `messages.list` 5 units (≤500 IDs/page), `messages.get` 5 units, `history.list` 2 units; HTTP batch up to 100 calls (≤50 recommended); `watch` must be renewed at least every 7 days; `history` cursors expire (404 → partial resync). |
| Graph | Per-mailbox throttling (around 10k requests / 10 min, about 4 concurrent requests per mailbox for Outlook); JSON `$batch` up to 20; `$search` has result caps and limited paging; message-subscription lifetime is a few days at most (renew); `delta` is per folder. |

→ **Every mailbox has its own token bucket and concurrency limiter** (Gmail: about 40 gets/s steady with bursts; Graph: 4 in flight). Respect `Retry-After`; exponential backoff with jitter on 429/5xx.

### 9.2 Volume model (100k-message inbox, heavy card user, 10 years of mail)

| Family | Matches | Fetched |
|---|---|---|
| Statements (3 cards × 12 × ~8 yrs, minus empty cycles) | ~250 | 250 metadata |
| Onboarding + lifecycle + applications | ~150 | 150 metadata |
| Bank promotions (matched by lifecycle/offer words) | ~400 | 400 metadata (cheap; classified out) |
| Transaction alerts | ~8,000 | ~10 × 40 quarters × 3 banks ≈ **1,200** sampled (adaptive) |
| Unknown-sender sweep | ~300 | 300 metadata (background) |
| **Total** | ~9,100 | **~2,300 metadata + ~150 bodies** |

At about 40 metadata gets/s: **Phase B ≈ 10–20 s, Phase C ≈ 60–90 s, Phase D ≈ 1–3 min.** Light users (10k inbox) are in the low tens of seconds end to end.

### 9.3 Targets (SLOs)

| Metric | Target |
|---|---|
| Time to first card shown | P50 < 10 s, P95 < 30 s |
| Quick pass complete (24 months, high-value types) | P50 < 30 s, P95 < 60 s |
| Full historical discovery (known banks) | P50 < 90 s, P95 < 3 min for 100k inbox |
| Background sweep done | P95 < 15 min |
| Incremental: new bank email → card updated | P95 < 60 s from delivery |
| Re-run on unchanged inbox | < 5 s (all IDs already in `processed_messages`) |
| LLM calls per new user | P50 ≤ 20, P95 ≤ 100 |
| Quality | Ownership recall ≥ 95% on the labelled suite (≥ 90% on dormant subset); precision of auto-listed cards ≥ 99%; zero promo-only discoveries |

### 9.4 Techniques

1. **Typed queries** that pre-classify and keep per-family fetch policies.
2. **Metadata-first** (`format=metadata` with chosen headers / Graph `$select=subject,from,toRecipients,receivedDateTime,bodyPreview,internetMessageHeaders`). Fetch the body only when the snippet can't answer.
3. **Never download attachments** in v1. Read filenames from the MIME structure only.
4. **Idempotent processing ledger** (`processed_messages` keyed by `(mailbox, provider_msg_id)` with `extractor_version`). Re-runs skip; a version bump re-processes only affected templates.
5. **Global template cache** (no user data) → most messages are classified with no LLM call.
6. **Stratified, adaptive sampling** for high-volume alert types.
7. **Recent-first ordering + streaming results** so the user sees value immediately.
8. **Checkpointing** via `pageToken` per query in the run state; a crashed worker resumes.
9. **Phase separation**: the user waits for B; C and D run in the background with lower queue priority.
10. **Batching**: Gmail batch (≤50) and Graph `$batch` (≤20) to cut HTTP overhead (quota cost is unchanged).

---

## 10. Data model (Postgres)

```sql
-- identities & connections
users(id, display_name, name_variants text[], created_at)
mail_connections(id, user_id, provider, email_address, aliases text[],
  token_ciphertext bytea, token_key_id, scopes text[],
  sync_cursor text,            -- Gmail historyId / Graph deltaLink (encrypted)
  watch_expires_at, status [active|revoked|error], connected_at, last_synced_at)

-- reference data (no user data)
banks(id, name, country)
bank_senders(id, bank_id, match_type, value, dkim_domain, valid_from, valid_to, trust)
card_catalog(...)                             -- existing platform table
card_aliases(id, bank_id, alias_normalised, catalog_card_id, source)
email_templates(id, bank_id, subject_skeleton_hash, evidence_type,
  extraction_rule jsonb, version, status [proposed|approved|retired], seen_count)

-- processing state
discovery_runs(id, user_id, mode [quick|full|incremental], phase, status,
  started_at, finished_at, stats jsonb, checkpoint jsonb)
processed_messages(connection_id, provider_msg_id, rfc_message_id_hash,
  received_at, template_id, evidence_type, extractor_version, processed_at,
  PRIMARY KEY (connection_id, provider_msg_id))
  -- no subject, no body, no snippet

-- discovered entities
discovered_cards(id, user_id, bank_id, catalog_card_id NULL, product_raw,
  last4 char(4) NULL, network, ownership_role [primary|addon_holder|addon_of_primary|other],
  ownership_conf numeric, status, status_conf numeric,
  first_evidence_at, last_evidence_at, last_usage_at,
  user_decision [none|confirmed|rejected|not_mine|closed], user_decided_at,
  created_at, updated_at)
  UNIQUE (user_id, bank_id, last4) WHERE last4 IS NOT NULL
card_evidence(id, card_id, connection_id, provider_msg_id, evidence_type,
  occurred_at, weight, modifiers jsonb, extracted jsonb, extractor [rule|llm|user],
  UNIQUE (card_id, provider_msg_id, evidence_type))
card_links(from_card_id, to_card_id, relation [upgraded_to|replaced_by|addon_of|possible_duplicate],
  confidence, source [explicit|inferred|user])
applications(id, user_id, bank_id, product_raw, applied_at, status, last_evidence_at)
card_product_history(card_id, catalog_card_id, product_raw, from_date, to_date)
```

Notes: `extracted` jsonb holds only derived fields (product, last4, date, amount buckets if needed), never raw text. `ownership_conf`/`status` are recomputed from `card_evidence` (pure function), which makes reweighting a batch job, not a migration.

### 10.4 Optional bureau reconciliation (phase 2)
`bureau_accounts(user_id, issuer, opened_at, status, limit_bucket)`, matched per issuer by count. A mismatch ("bureau: 4 HDFC cards, email: 3") prompts the user to add the missing one by hand.

---

## 11. API design

```
POST   /v1/mail-connections/{provider}/authorize      → { authUrl }
GET    /v1/oauth/{provider}/callback                  (server-side code exchange, PKCE + state)
GET    /v1/mail-connections                           → list (address, status, lastSyncedAt)
DELETE /v1/mail-connections/{id}?purge=evidence|all    → revoke token at provider + purge

POST   /v1/discovery-runs            { connectionIds?, mode: "full" }   → 202 { runId }
GET    /v1/discovery-runs/{id}       → { phase, progress{listed,fetched,classified}, cardsFound }
GET    /v1/discovery-runs/{id}/events   (SSE: card.discovered, card.updated, phase.changed, run.completed)

GET    /v1/cards?group=owned|review|history
GET    /v1/cards/{id}                → card + evidence summary (types, dates; no email text)
GET    /v1/cards/{id}/evidence/{evidenceId}/preview   → on-demand live re-fetch of the snippet
                                                        from the provider, never stored
POST   /v1/cards/{id}/decision       { action: confirm|reject|not_mine|mark_closed|mark_replaced,
                                       replacedByCardId? }
POST   /v1/cards/{id}/merge          { intoCardId }
POST   /v1/cards                     (manual add; also used when a bureau mismatch prompts it)

DELETE /v1/me/discovery-data        → full erasure (connections, ledger, evidence, cards unless kept)

-- internal
POST   /internal/push/gmail          (Pub/Sub push, verified JWT)
POST   /internal/push/graph          (validation token + clientState check)
```

User decisions are **sticky**: a `rejected`/`not_mine` card is never re-surfaced by later evidence unless new *strong* evidence of a different kind arrives, and even then it goes to review.

---

## 12. Security and privacy

| Concern | Control |
|---|---|
| Scopes | Gmail `gmail.readonly` only; Graph `Mail.Read` + `offline_access` + `User.Read`. No send/modify scopes. Ask for consent with a clear explanation screen. |
| Google verification | Restricted scope → OAuth app verification + annual CASA security assessment. **Start immediately**; budget time and cost; publish a privacy policy that fits Google's Limited Use rules. Microsoft: publisher verification. |
| Token storage | Refresh tokens encrypted with envelope encryption (KMS data key per user), decrypted only in worker memory; never logged; access tokens never persisted. |
| Data minimisation | Store **derived facts only**: bank, product, last4, dates, evidence type, message ID. **No bodies, subjects or snippets at rest.** "Why" previews are fetched live on demand. |
| Card numbers | Luhn-detected full numbers redacted in memory before logs or the LLM; only last4 persisted; `last4` + bank is still treated as sensitive (column-level encryption optional). |
| LLM processing | Redacted text only; provider under no-training/zero-retention terms where available; per-request input cap; no user identifiers in prompts. |
| Retention | Processing ledger kept while connected; evidence kept while card exists; logs scrubbed of PII; transient bodies in memory only, never on disk or queue (queue messages carry IDs, not content). |
| Disconnect / delete | Revoke the token at the provider, stop watch/subscription, delete ledger and evidence; the user chooses whether to keep confirmed cards. Hard-delete within 24 h; backups expire within 30 days. |
| Access | Worker identity can decrypt tokens; API cannot. Audit log for every token decryption. |
| Abuse | Only the account owner can connect a mailbox (OAuth); no admin-side mailbox browsing. |

---

## 13. Test-case matrix

Legend — Fixture: synthetic `.eml` sets per bank/template. Expect: groups `Owned / Review / History / None`.

### A. Discovery
| # | Scenario | Fixture | Expected |
|---|---|---|---|
| 1 | Active card, monthly statements | 24 statements ••1111 | 1 card, Owned, active, own ≥ 0.99 |
| 2 | Unused 3 yrs | statements to 2022 + annual fee 2023–25 | Owned, dormant |
| 3 | Approval 4 yrs ago only | 1 approval email, product only | Review, provisional, own 0.70 |
| 4 | Two cards, same bank | statements ••1111 + ••2222 same product | 2 cards, both Owned |
| 5 | Different variants, same bank | Regalia ••1111, Millennia ••2222 | 2 cards with correct catalogue IDs |
| 6 | Lifetime-free, rarely used | welcome 2021 + 2 alerts | Owned, dormant |
| 7 | Occasional alerts only | 5 alerts across 3 yrs, no statements in sample window | Found by adaptive sampling, Owned |

### B. Historical / dormant
| # | Scenario | Expected |
|---|---|---|
| 8 | Used then silent for years | Owned, dormant, **not closed** |
| 9 | Old welcome only | Owned (0.90), dormant, status confidence low |
| 10 | Last statement years ago | Owned, dormant, "still open?" prompt |
| 11 | Upgraded variant (new number) | Old → History `replaced`, link → new Owned |
| 11b | Upgraded variant (same number) | One card, product_history shows both, current = new |
| 12 | Expired → reissued same last4 | One card, active |
| 12b | Lost → replaced new last4 | Old `replaced`, new Owned |
| 13 | Explicit closure | History `closed` |

### C. False positives
| # | Scenario | Expected |
|---|---|---|
| 14 | Promo for card not owned | No card |
| 15 | Bank offer "apply now" / pre-approved offer for a new card | No card for the offered product; if it names an existing ••1234, that card gets +0.5 |
| 16 | Add-on: user is primary | Add-on nested under primary, holder = other |
| 16b | Add-on: user is add-on holder | Review, role addon_holder |
| 17 | Payment reminder for someone else's card | Name mismatch / not-to-user → Review or None |
| 18 | Forwarded statement | Weight ×0.2 → Review at most, labelled "forwarded" |
| 19 | Rejected application | Application `rejected`, no card |
| 20 | Application in progress | Application `in_progress`, no card |

### D. Duplicates
| # | Scenario | Expected |
|---|---|---|
| 21 | Card in 60 statements | One card |
| 22 | Card in alerts + promos | One card; promos add 0 |
| 23 | Bank changes sender domain (2019) | One card across both domains |
| 24 | "Regalia" / "HDFC Regalia CC" / "Regalia Credit Card" | One card, one catalogue ID |
| 25 | `XX1234`, `**** 1234`, `ending 1234`, `4xxx…1234` | One card, last4 1234 |

### E. Additional edge cases
| # | Scenario | Expected |
|---|---|---|
| 26 | Same message in two connected mailboxes | Processed once (Message-ID) |
| 27 | Phishing email spoofing a bank | DKIM fail → ×0.3, never auto-listed |
| 28 | Image-only email (empty text body) | Subject + filename only; Review if nothing extracted |
| 29 | Full card number in body | Redacted, never stored; metric incremented |
| 30 | Approval without last4 + two later same-product cards | No auto-merge; "possible duplicate" review |
| 31 | Same last4 at two different banks | Two cards |
| 32 | Same last4, same bank, different concurrent products | Split + flag |
| 33 | Co-branded card (issuer bank + brand partner sender) | Partner sender in registry with `trust=partner`, mapped to issuer |
| 34 | Inactivity-closure warning then later alert | Stays active (later evidence wins) |
| 35 | Inactivity-closure warning, nothing after | `possibly_closed` → Review |
| 36 | User deleted all statements, only fee notices remain | Owned, dormant |
| 37 | Bank not in registry | Found by background sweep → Review; sender proposed for registry |
| 38 | Gmail history cursor expired | Partial resync of last 30 days; no duplicates |
| 39 | Token revoked mid-run | Run fails gracefully, connection `revoked`, user notified |
| 40 | 429 storm | Backoff; run completes, latency SLO tracked |
| 41 | LLM returns a last4 not in the text | Dropped by grounding check |
| 42 | User rejected a card; more promo mail later | Stays rejected |
| 43 | Non-English / mixed-language bank mail | Rules miss → LLM fallback; covered by fixture |
| 44 | Business/corporate card in the user's name | Shown with `card_type=corporate` if detectable, else Review |
| 45 | Debit-card or loan emails from the same bank | Classified out (not a credit card) |

**Test infrastructure:** (1) a fixture corpus of synthetic emails per template, (2) a replay harness with a fake `MailProvider` that enforces realistic rate limits, (3) a golden-file assertion on the final card set, (4) a load test with 100k synthetic messages measuring the §9.3 SLOs, (5) an offline eval set of real, consented, redacted inboxes for recall/precision, re-run on every rule/weight/model change.

---

## 14. Failure scenarios & mitigation

| Failure | Impact | Detection | Mitigation |
|---|---|---|---|
| Bank changes template | Rules miss → LLM cost up, possibly wrong type | Spike in unknown fingerprints per bank | LLM fallback absorbs it; auto-propose new template; alert |
| Bank changes sender domain | Missed new mail | Background sweep finds DKIM-valid unregistered sender | Registry review queue |
| Rate limiting | Slow runs | 429 rate, P95 run time | Per-mailbox token bucket; phase priorities |
| History cursor expiry | Missed incremental mail | 404 from `history.list` | Bounded resync (last 30 days via query) |
| Push subscription lapses | Stale data | `watch_expires_at` | Daily renew + daily light reconciliation query |
| LLM invents last4/product | Wrong cards | Grounding-check drop rate | Verbatim grounding check; catalogue-constrained products |
| LLM outage | Long tail unprocessed | Error rate | Queue the items, retry later; rules path unaffected |
| Over-merging two cards | User sees one card instead of two | Review rate on merges | Merge only on last4 or a unique candidate; else ask |
| Under-merging | Duplicates | Users merging cards manually | Alias table updates; merge endpoint |
| Promo misclassified as ownership | False card | User rejects | Promos weight 0; promo-shaped templates reviewed; rejection logged as a label |
| Users delete bank mail | Missed cards | Bureau mismatch (phase 2) | Manual add; bureau reconciliation |
| Workspace admin blocks third-party apps | Can't connect | OAuth error | Clear error message and guidance |
| Token leak | Severe | Audit log anomalies | Envelope encryption, least privilege, short-lived access tokens, rotation, revocation |
| Google verification delayed | Launch blocked | — | Start now; launch Outlook first or run a limited test-user beta (Gmail unverified apps are capped to a small number of test users) |

---

## 15. Cost & latency estimates

**Mail APIs:** no per-call fee; the only constraint is quota (§9).

**LLM (per new user, worst case early on, before the template cache warms up):**
- About 60 fallback calls × ~1,500 input tokens + ~200 output tokens ≈ 90k input, 12k output.
- With **Claude Haiku 5.5** ($0.10 / $0.50 per M input/output tokens; prices checked 2026-11, confirm before budgeting): ≈ $0.009 + $0.006 ≈ **$0.015 per user**. The background phase can use the Batch API (50% off) → about **$0.008**.
- Once the cache is warm (most templates known): about 5–20 calls → **< $0.003 per user**.
- Template induction (a stronger model such as Claude Opus 5.5 at $4 / $20, run once per *new template*, about 5k in / 1k out): ≈ $0.04 per template, a one-off. Even 2,000 templates ≈ $80 total.
- Incremental sync: nearly all cache hits → effectively $0.

**Compute:** a 100k-inbox discovery run is about 2–3k API calls and some regex. Roughly a few CPU-seconds of worker time; one modest worker handles dozens of concurrent mailboxes because the work is I/O-bound.

**Storage:** about 2–3k ledger rows + about 100–500 evidence rows per user, which is under 1 MB.

**Fixed costs to plan for:** Google restricted-scope verification and the annual third-party security assessment (quotes vary by assessor and tier; get them early), KMS, monitoring.

**Latency (summary):** first card in under 10 s; full historical discovery in about 1–3 min for very large inboxes; incremental updates within a minute of email delivery.

---

## 16. Phased delivery

1. **Phase 1 (MVP):** Gmail adapter, sender registry for the top ~10 issuers, typed queries, metadata-first, rules + LLM fallback with grounding, entity resolution rules 1–4, noisy-OR scoring, three-group UI with confirm/reject, full delete. Start Google verification on day 1.
2. **Phase 2:** Outlook/Graph adapter, push incremental sync, template learning with human approval, add-on handling polish, opt-in PDF deep scan, bureau reconciliation.
3. **Phase 3:** Learned calibration from user decisions, trained lightweight classifier to replace most LLM calls, more issuers and countries.

---

## 17. Facts to verify before build (don't trust this doc for these)

- Gmail per-user quota and per-method unit costs, batch limits, `watch` renewal period, `history` retention behaviour.
- Whether `gmail.metadata` still rejects `q` (it did historically).
- Graph `$search` result caps and paging on messages, throttling limits, and subscription max lifetime for mail.
- Current Google restricted-scope verification and security-assessment requirements, timelines and costs.
- For each launch issuer: real sender addresses, DKIM domains, whether statements are sent in nil-activity cycles, statement PDF password scheme, whether upgrades keep the card number.
- Regulatory rules on issuer notifications for inactive cards in each launch market (used as a dormant/closure signal).
