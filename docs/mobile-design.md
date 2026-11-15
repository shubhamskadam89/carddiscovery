# Card Discovery: mobile-first, on-device design

Status: proposal · Date: 2026-11-15 · Replaces the server-side processing parts of [card-discovery-design.md](card-discovery-design.md) (§3, §4, §9–§12). Its problem analysis, scoring approach and test matrix still apply.

---

## 0. The decision

**Discovery runs inside the mobile app, on the user's phone.** The app signs in to Google, reads bank email directly from the Gmail API, finds the cards, and sends **only the card list** to our servers. Email content and the Google token never reach our servers.

| | On-device (chosen) | Server-side (previous design) |
|---|---|---|
| Where the Google token lives | Phone's secure storage | Our token vault: the most valuable thing a breach could take |
| Where email is processed | Phone memory, for seconds | Our workers |
| What our servers hold | Card facts only | Tokens, message IDs, evidence |
| Privacy promise to users | "Your emails never leave your phone" | "We read your bank emails on our servers and delete them" |
| Google security assessment (CASA) | **Unsettled** while the card list is sent to our server (§6.1). Budget for it. | Required every year |
| DPDP scope | Card facts | Email processing plus card facts |
| Background sync | Limited (OS restrictions) | Easy (push notifications, workers) |
| Updating rules | Download a signed rules file | Deploy the server |
| AI fallback | Hard to keep the "never leaves your phone" promise | Straightforward |

Why we accept the costs: for a one-time discovery plus occasional refresh, background sync limits don't matter much. Rules can still be updated without an app release (see §4). The privacy and compliance gains outweigh both.

---

## 1. Architecture

```
┌──────────────────────────── Mobile app ─────────────────────────────┐
│                                                                      │
│  Consent screens ─▶ Google sign-in (OS SDK) ─▶ token in Keystore/    │
│                                               Keychain (never sent)  │
│                                                                      │
│  Discovery engine (port of the Python reference)                     │
│   1 Query planner      ← rules bundle (signed, downloaded)           │
│   2 Gmail client       → gmail.googleapis.com (direct, HTTPS)        │
│   3 Classify/extract   ← rules bundle                                │
│   4 Resolve + score    ← rules bundle (weights, thresholds)          │
│   5 Local ledger       (encrypted on-device DB, derived facts only)  │
│                                                                      │
│  Review screens: Your cards / Please confirm / History               │
└───────────────┬─────────────────────────────────────▲───────────────┘
                │ card facts only (bank, product,     │ rules bundle,
                │ last4, status, dates, user choice)  │ card catalogue
                ▼                                     │
┌──────────────────────────── Our backend ────────────┴───────────────┐
│  Card-facts API (app-attested)  ·  Rules bundle hosting + signing    │
│  Card catalogue (existing)      ·  Aggregate, content-free telemetry │
└──────────────────────────────────────────────────────────────────────┘
```

There is no email, no Google token and no message ID anywhere on the backend.

---

## 2. Components on the phone

### 2.1 Google sign-in
- **Android:** Google Identity Services authorization API, requesting the `gmail.readonly` scope. Google Play services holds and refreshes the grant, and the app asks for a fresh access token when it needs one. Our code never handles a refresh token.
- **iOS:** the Google Sign-In SDK, which stores credentials in the Keychain and refreshes them itself.
- **Never** request offline or server access (no server auth code). That would put a long-lived token on our servers and undo the design.
- Access tokens stay in memory only, never in logs, analytics or crash reports.
- *Confirm the exact current SDK calls for both platforms during implementation. The point that matters is that the OS SDK holds the grant and our code doesn't.*

### 2.2 Gmail client
- Direct HTTPS calls to the Gmail API, the same requests the Python reference makes: `messages.list` with `q`, `messages.get` with `format=metadata`, and `format=full` only when a body is needed.
- Batched requests (up to 50 per batch), or parallel requests with **at most 4–8 in flight**. Stay within the per-user rate limit, and back off on 429/5xx.
- Never call `attachments.get`.

### 2.3 Discovery engine
A port of the reference modules, one to one:

| Python reference | Mobile module | Notes |
|---|---|---|
| `queries.py` | QueryPlanner | Builds Gmail `q` strings from the bundle; adaptive alert windows |
| `providers/gmail.py` | GmailClient, HtmlText | HTML to text must give exactly the same output (block tags, entities, invisible characters) |
| `extract.py` | Extract | Last-4, account-number exclusion, full-number redaction, names |
| `classify.py` | Classifier | Rules, ad and boilerplate lines, product choice |
| `resolve.py` | Resolver | Same two passes |
| `score.py` | Scorer | Weights and thresholds come from the bundle |
| `ledger.py` | Ledger | Encrypted local DB |

The engine is **pure logic with no network or storage calls of its own**, so it can be tested against shared test vectors (§7).

### 2.4 Local ledger
- Stores only derived evidence, the same shape as `Evidence.to_dict()`, so a second scan skips emails already read.
- Encrypted: Android (Room + SQLCipher, or files protected by EncryptedFile/Keystore); iOS (Data Protection class "complete until first unlock" or stronger).
- Excluded from device and cloud backups.
- Wiped on disconnect.

### 2.5 Running the scan
- **First scan in the foreground**, while the user watches cards appear. Target: under 30 s on Wi-Fi for a typical inbox, under 90 s for 100k emails.
- If the user leaves the screen: Android continues as expedited WorkManager work (or a short foreground service with a visible notification); iOS asks for extra background time and resumes on next open.
- **Refreshes:** on app open, plus opportunistic background refresh (WorkManager periodic / BGAppRefreshTask). Use the Gmail history ID saved from the last scan, so only new mail is read.

---

## 3. What leaves the phone

| Data | Leaves the phone? | Where it goes | Why |
|---|---|---|---|
| Email bodies, subjects, preview text | **Never** | — | — |
| Google access or refresh token | **Never** | — | — |
| Gmail message IDs | **Never** | — | Kept only in the on-device ledger |
| Other people's names (add-on, family) | **Never** | — | Only a yes/no flag is stored, on the device |
| Card facts: bank, catalogue product, last 4, status, first/last seen month, evidence *types*, user's confirm/reject | Yes | Card-facts API | The feature itself: rewards optimisation |
| Content-free counters: number of emails read, cards found per bank, unclassified count per bank, app/rules version | Yes, opt-in | Telemetry | Measures recall gaps without content |
| Masked subject templates (e.g. "Your {BANK} Credit Card Statement for {MONTH}") | **Not by default.** Separate opt-in, and only after a legal check against Google's Limited Use policy | Template review queue | Improves rules; see §6 |

Card facts are personal financial data. The backend treats them as such: encrypted at rest, deletable by the user, and never shared or sold.

### 3.1 Every card carries its source

Google's Limited Use rules apply to *"the raw data obtained from the scopes and data aggregated, anonymized, or derived from them."* A card found in Gmail is derived data, so the restrictions stay with the card record on our servers. Every card fact therefore carries a **source tag**:

| Source | Restrictions on use |
|---|---|
| `gmail` | Google Limited Use (table below) |
| `outlook` | Microsoft's terms (to be checked) |
| `sms` | Google Play rules: core functionality only; never share non-financial or personal SMS |
| `manual` (user added or confirmed by hand) | Our own privacy policy and DPDP only |

A card seen in several sources keeps all its tags. The strictest rules apply until the user adds or confirms it by hand. Whether a user's confirmation actually lifts the Gmail restrictions is a **legal question to settle first** (§9).

| Use of a `gmail`-sourced card | Allowed? |
|---|---|
| Show the user their cards; reward optimisation on them | ✅ User-facing, prominent feature |
| Fee, points-expiry and due-date reminders about their own cards | ✅ Very likely, as user-facing features |
| Personalised ads, offers or retargeting | ❌ Banned ("serving ads… personalized or interest-based advertising") |
| Sharing with banks, issuers, partners or data brokers | ❌ Banned, except transfers needed for the user-facing feature, with consent |
| Credit or lending decisions, loan partners | ❌ Banned ("to determine credit-worthiness or for lending purposes") |
| Training or improving a general AI model | ❌ Banned (only "that specific user's personalized model") |
| Employees viewing individual records | ❌ Only with the user's agreement, for security, legal reasons, or as aggregates |
| New-card recommendations that earn us an affiliate commission | ⚠️ **Grey area.** May count as personalised advertising; needs counsel before launch |

The backend enforces this: services that run ads, partner integrations, lending or affiliate targeting **cannot read cards whose only sources are `gmail` or `outlook`**. Enforce it in the data-access layer, not by convention.

---

## 4. Rules bundle: updating rules without an app release

Today the bank list is data (`banks.toml`), but the classification rules, ad and boilerplate patterns, and weights are Python code. For mobile, **all of it becomes one versioned data file**:

```json
{
  "version": "2026.11.15-1",
  "min_engine": 3,
  "banks": { "hdfc": { "name": "HDFC Bank", "domains": ["hdfcbank.net"], "products": {"Regalia Gold": []} } },
  "rules":       [ {"type": "statement", "pattern": "\\bstatement\\b|e-?statement"} ],
  "offer":       "pre[\\s-]?approved|apply now|…",
  "boilerplate": "reserve bank of india|\\brbi\\b|…",
  "weights":     { "statement": 0.95, "welcome": 0.90 },
  "thresholds":  { "owned": 0.90, "active_days": 180 }
}
```

- **Signed** with Ed25519. The app holds the public key and rejects an unsigned or tampered bundle, so nobody can push rules that misread mail.
- **Shipped inside the app** as a fallback; a newer version is downloaded on app open.
- **`min_engine`** stops a bundle from loading on an app whose engine is too old to run it.
- **One regex dialect:** patterns must behave the same in Python `re`, Java/Kotlin and ICU (Swift). Stick to the shared subset: fixed-length lookbehind, no possessive quantifiers, Unicode flags set explicitly. The conformance tests (§7) enforce this.
- The Python reference loads the **same bundle**, so rules are written and tested once.

---

## 5. User flow

1. **Value first.** "Find all your credit cards automatically, including the ones you've forgotten." Show manual card entry next to it, not as a fallback.
2. **Our explainer, before Google's screen.**
   - "We only read emails from these banks" (expandable list).
   - "Your emails never leave your phone. We save only the card name and last 4 digits."
   - "How far back?" 1 / 3 / **5 years**.
   - Link to the privacy policy.
3. **Google's consent screen**, which says "read your email". Our explainer is what makes that acceptable.
4. **Scanning:** cards appear one by one, with a progress bar and a "Stop" button.
5. **Review**, written in plain sentences instead of scores:
   - **Your cards:** "HDFC Regalia Gold ••7410: seen on your statement, Nov 2026."
   - **Please confirm:** "ICICI ••9191: a payment reminder addressed to Priya Sharma. Is this yours?" with **Yes / No**.
   - **Old cards:** "Kotak League Platinum ••2297: closed in Nov 2026."
6. **Done:** the card list feeds the rewards features. "Refresh" and "Disconnect and delete" are in Settings.

Each Yes/No answer is stored with the card facts. These answers become the labels for calibrating weights later.

---

## 6. Privacy, security and compliance

### Security on the phone
- Token held by the OS SDK. Ledger encrypted and excluded from backups.
- No email text in logs, crash reports or analytics. Scrub crash reporters and turn off breadcrumb capture of network bodies.
- Card-facts API: TLS with certificate pinning, our own user authentication, plus **app attestation** (Play Integrity / App Attest), so only the genuine app can submit cards.
- Rules bundle signature verified before use.
- **Hostile email:**
  - Trust only the `Authentication-Results` header Gmail itself added (from `mx.google.com`).
  - Test-sender mode exists only in debug builds and is compiled out of release builds.
  - Cap processing per email (size, time).

### Disconnect and delete
- Revoke the grant with Google.
- Wipe the token and the ledger on the phone.
- Delete the card facts on the server, unless the user chooses to keep the card list. Ask them.

### 6.1 Google (checked against Google's policy pages, 2026-11-15)

**Scope.** `gmail.readonly` is **restricted**. `gmail.metadata` is restricted too, and can't run `q` searches, so no narrower scope works. The only sensitive (non-restricted) Gmail read scopes are for Gmail add-ons reading the currently open message, which is no use for scanning.

**Verification (required before public launch):**
- A verified domain, a homepage and a privacy policy that "discloses the manner in which your application accesses, uses, stores, or shares Google user data".
- The privacy policy includes Google's Limited Use statement, in substance: *our use and transfer of information received from Google APIs will adhere to the Google API Services User Data Policy, including the Limited Use requirements.*
- A demo video showing how a user starts the feature and grants access, plus a justification for each scope.
- Until verified: the unverified-app user cap and warning screen, with sign-ins expiring after 7 days in Testing mode. Google lists exemptions for apps in development, testing or staging, personal use, and internal-only use. Our internal pilot fits these; a public launch doesn't.

**Security assessment (CASA): unsettled for this design.**
- The Gmail scopes page: *"If you store restricted scope data on servers (or transmit), then you must go through a security assessment."*
- The restricted-scope verification page: required for any app that *"has the ability to access data from or through a third-party server."* It doesn't mention on-device-only apps.
- Google's developer forum had questions in September–October 2026 about **exactly this design** (mail parsed on the phone, only a derived list sent to the server). None has an answer from Google.
- So:
  - **Fully on-device**, with the card list also kept only on the phone: most likely to avoid the assessment, but the server can't use the cards.
  - **Our design**, sending the card list: may count as transmitting derived restricted data. **Budget for a yearly Tier 2 assessment.** Quotes seen range from about $540 a year (2024, discounted assessor) to several thousand dollars; get current quotes.
- Ask Google in writing during verification. The on-device design is still right either way, because it removes the token store and keeps email off our servers.
- Reassessment and reverification happen "at least every 12 months".

**Limited Use** (Google API Services User Data Policy and the Workspace API policy): see §3.1 for what that means for card records.

### 6.2 Other sources and their permissions

| Source | Permission | What's involved |
|---|---|---|
| Outlook / Hotmail / Live | Microsoft Graph `Mail.Read` (delegated) | Publisher verification. Microsoft's review process is not yet researched. |
| Android SMS | `READ_SMS` / `RECEIVE_SMS` | Only for the default SMS app, unless Google grants the **"SMS-based money management"** exception, case by case, through a Play Console declaration. Core functionality only; no alternative method may exist; "may not exfiltrate or share non-financial or personal SMS history". |
| iOS SMS | Not available | iOS gives apps no SMS access. |
| BBPS bill fetch | Through a BBPS operating partner | Needs the card's registered mobile number plus last 4 digits as input, so it doesn't discover cards. Good for dues and status of known cards. |
| Account Aggregator | Through an AA partner | No evidence found that card issuers share card statements through AA today. |

**Recommended combination:** manual picker (no restrictions) + Gmail on the phone (history and dormant cards) + SMS on Android (keeps active cards current; banks send an SMS for every transaction). SMS rarely finds dormant cards, so it complements email rather than replacing it.

### 6.3 What others do
- **SaveSage** (India): links Gmail, Outlook, Hotmail or Live to sync statements, and reads SMS on Android. One user on a public forum reported it found **15 of their 20 cards**, a rough benchmark for email-plus-SMS discovery. Its public privacy policy doesn't describe email access or include a Limited Use statement; we should do better.
- **CRED** (India): optional Gmail access for statements, limited by its policy to financial emails. Users report it opens statement PDFs, and fetches bill amounts through BBPS since 2024 (unconfirmed). Many users link a separate address that receives only forwarded statements.
- **Unroll.me** (US): sold data from users' receipts to Uber; the US FTC found it misled users about not touching personal emails. This is the reason Google's rules are strict, and the reason our promise must be checkable.

Email forwarding (a dedicated address users forward statements to) only covers **future** mail, because Gmail filters don't forward existing emails. It can't meet the 5-year history requirement, though it could be an opt-in mode for users who refuse inbox access.

### 6.4 DPDP Act 2023 (India)
- The DPDP Rules were notified on 13 November 2025. Consent-manager provisions follow about a year later. Most obligations, including security safeguards and breach notification, apply from **13 May 2027**.
- Breaches: tell the Data Protection Board **within 72 hours** of becoming aware, and affected users without delay. (From law-firm summaries; confirm against the gazette text.)
- Our servers process card facts only, but email is still processed on the user's behalf by our app. **Get counsel's view on how DPDP treats on-device processing by our app.** The consent screen (step 2) should meet DPDP standards either way.
- One purpose per consent; easy withdrawal; export and delete of card facts; a grievance contact; a breach plan.

### AI fallback
On-device processing makes the AI fallback a real trade-off:
1. **No fallback** (default for v1): unclassified bank emails are counted, not read by anyone.
2. **On-device model**: a small model running in the app. Privacy stays intact, but there are app-size and device-support costs.
3. **Cloud model with separate consent**: only the redacted email in question, sent from the phone straight to the provider under zero-retention terms. This changes the "never leaves your phone" promise for those emails, and the explainer must say so.

Decide this after the pilot shows how many bank emails the rules miss.

---

## 7. The Python CLI's new role: reference and test harness

The CLI stays, in this role:
- **The reference implementation.** Rules are developed and tried here first, against real inboxes in the pilot.
- **The conformance suite.** Export every test email and its expected result (clean, messy, HTML and long kits, plus the scenario matrix) as **JSON test vectors**:
  ```json
  {"id": "L1", "subject": "…", "html": "…", "expect_evidence": {"type": "statement", "last4s": ["7410"], "product": "Regalia Gold"},
   "expect_cards": [{"bank": "hdfc", "last4": "7410", "group": "owned", "product": "Regalia Gold"}]}
  ```
  The Python engine and both mobile engines must pass the same vectors on every build. This is what keeps three implementations in agreement.
- **The pilot tool** for colleagues: it runs locally, prints only derived output, and produces the first real held-out set.

---

## 8. Milestones

| # | Milestone | Done when |
|---|---|---|
| M0 | **Security fixes in the reference:** only Gmail's own header trusted; test mode limited to debug; `forget` revokes with Google | Tests cover forged headers and the release-mode lockout |
| M1 | **Rules bundle:** move the rules, ad and boilerplate patterns, and weights into a signed JSON bundle loaded by the Python engine | Existing 57 tests pass on the bundle |
| M2 | **Test vectors:** export all kits and the matrix as JSON; Python passes them | Vectors committed; CI runs them |
| M3 | **Mobile engine:** port the engine; pass all vectors | Engine passes 100% of vectors in a unit-test build |
| M4 | **On-device scan:** Google sign-in, Gmail client, encrypted ledger, foreground scan | Internal build finds the 54 test-email cards on a test account |
| M5 | **Review UX and card-facts API:** plain-language review, Yes/No, disconnect and delete, attestation | End-to-end demo on an internal testing track |
| M6 | **Pilot:** 5–10 consenting colleagues with cards, on the internal testing track; ground truth recorded | Recall and precision per bank, measured on real inboxes |
| M7 | **Google verification submitted**, in parallel from M4 onwards: verified domain, privacy policy with the Limited Use statement, demo video, scope justification; assessor quotes collected | Verification approved; security-assessment question answered in writing |
| M7a | **Source tags and use restrictions:** every card record carries `gmail` / `outlook` / `sms` / `manual`; ads, partner, lending and affiliate services can't read email-only cards | Enforced in the data-access layer, with tests |
| M7b | **Legal opinion:** affiliate recommendations, whether user confirmation changes a card's restrictions, DPDP for on-device processing | Written opinion on file before public launch |
| M9 | **Android SMS source** (optional): Play Console declaration for SMS-based money management; on-device SMS parsing into the same engine | Exception granted; SMS evidence merged with email evidence |
| M8 | **Incremental refresh:** history-based refresh on open and in the background | New statement updates a card within one app open |

Gate before public launch: **at least 90% of real cards found across the top 5 issuers in the M6 pilot**, measured on inboxes of people who didn't write the rules.

---

## 9. Open decisions

1. **Mobile stack.** Native Kotlin/Swift, React Native, Flutter, or Kotlin Multiplatform. This decides whether the engine is written once (shared code) or twice (Kotlin and Swift). With shared code, the conformance vectors are run once; with two native ports, both must pass them.
2. **Android first, or both platforms at once?**
3. **AI fallback:** none, on-device, or cloud with consent (§6).
4. **Opt-in template telemetry:** whether masked subject templates may leave the phone at all (legal check).
5. **Default scan depth:** 5 years, or 3 years with an option to extend.
6. **Keep the card list after disconnect?** Ask the user, or always delete.
7. **Affiliate card recommendations** using Gmail-derived cards: allowed, allowed only for cards the user added or confirmed by hand, or not at all (needs counsel, §3.1).
8. **Card list on the server or on the phone only?** Keeping it on the phone most likely avoids the security assessment, but rewards features would then run on the phone (§6.1).
9. **Add the Android SMS source** in v1 or later (§6.2).

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Google still requires the security assessment | Budget for it anyway; the on-device design still reduces its scope |
| Gmail-derived cards used for ads, partners, lending or affiliate targeting | Source tags enforced in the data-access layer (§3.1); legal opinion before launch |
| Google Play refuses the SMS exception | SMS is optional; email plus manual entry still work |
| Users expect full recall | A competitor's user reported 15 of 20 cards found; set expectations in the UI and keep manual entry next to discovery |
| Three implementations drift apart | One rules bundle plus shared test vectors in CI |
| Regex behaves differently across platforms | Use only the shared subset; vectors catch differences |
| Slow phones or networks on a 5-year scan | Foreground scan with live results; resume across sessions; 4–8 requests in flight |
| iOS background limits | Refresh on open; background refresh is a bonus, not a dependency |
| Users refuse Google's "read your email" screen | Our explainer first; manual entry offered alongside; measure consent rate in the pilot |
| Rules miss real bank mail | Content-free unclassified counters per bank; pilot ground truth; AI fallback decision after M6 |

---

## Sources (checked 2026-11-15)

- [Google: restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
- [Google API Services User Data Policy (Limited Use)](https://developers.google.com/terms/api-services-user-data-policy)
- [Google Workspace API user data and developer policy (AI/ML rule)](https://developers.google.com/workspace/workspace-api-user-data-developer-policy)
- [Gmail API scopes (restricted vs sensitive; assessment trigger)](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Google forum: CASA for on-device parsing with a derived list (unanswered)](https://discuss.google.dev/t/gmail-restricted-scope-is-the-casa-assessment-required-when-mail-is-processed-on-device-and-only-a-derived-subscription-list-reaches-our-server/398372)
- [Google forum: client-only Android app and CASA](https://discuss.google.dev/t/does-a-client-only-android-app-with-no-server-need-the-casa-security-assessment-for-drive-readonly/393638)
- [Google Play: SMS and Call Log permissions](https://support.google.com/googleplay/android-developer/answer/10208820?hl=en)
- [CASA Tier 2 pricing comparison (Switchlabs)](https://www.switchlabs.dev/post/casa-tier-2-tier-3-security-review-providers-pricing-and-the-cheapest-option)
- [SaveSage App Store listing](https://apps.apple.com/us/app/savesage-credit-card-points/id6736468204) · [SaveSage privacy policy](https://savesage.club/privacy-policy) · [User report: 15 of 20 cards found](https://technofino.in/community/threads/feedback-on-savesage-club.34718/post-859974)
- [Technofino: email access to CRED](https://technofino.in/community/threads/email-access-to-cred.22944/latest)
- [Unroll.me FTC settlement explained](https://leavemealone.com/blog/unroll-me-ftc-settlement-explained/)
- [Mondaq: DPDP Rules 2025 notified](https://www.mondaq.com/india/data-protection/1708164/digital-personal-data-protection-rules-2025-notified)

Policies change. Re-check these before submitting for verification, and have counsel confirm the legal points.
