# Card-evidence scoring: proposal (not in production yet)

> **Update 2026-11-21: running in shadow mode.** After review, the design became: rule-based
> identity (with payee, debit-card, NPS and account-ending guards added), two logistic models
> (`carddiscovery/evidence_model.py`, weights in `carddiscovery/data/evidence_model.json`), card
> confidence from independent event families (copies and one campaign count once), and
> wallet/ask thresholds derived from product costs (`thresholds()`). Every email the engine reads
> is scored and stored with its evidence; the dev panel shows the model's decision next to the
> rules'. It never moves a card. Study tooling: `python -m cdserver corpus evaluate|blind|
> blind-apply|export` (`cdserver/corpus_eval.py`). The weights below are from the first pass;
> the shipped ones are in the JSON file. Labels are still from rules until the blind review is done.

Learned from one consenting mailbox's study set (1,397 bank emails over 5 years, 541 templates,
20 real cards, 11 banks), with `python -m cdserver corpus …` (see `cdserver/corpus.py`,
`cdserver/corpus_model.py`). Built to transfer to other mailboxes, not to fit this one.

## How it's kept from over- or under-fitting

- **Generic features only.** 25 concepts, each tagged by where it appears (subject S, preview P,
  body without footer B, footer F, PDF, Gmail tab): "billing words in S/P", "a card number the
  engine keeps", "account words in S/P"… No bank names, product names, sender names or phrases
  of this mailbox. Sender-name features were tried and dropped: they encoded one bank's habits
  ("bankalerts@" sends account mail at Kotak) and wouldn't carry to another bank.
- **One row per template** (sender + subject pattern): a mail sent 50 times counts once.
- **Few, readable weights.** L2-regularised logistic regression, penalty chosen by validation;
  each weight is a log-odds "point" with a bootstrap range. Weights whose range crossed zero were
  removed; the lean model is as good or better on unseen banks than the full one.
- **Validated where it matters for new users:** leave-one-bank-out (each bank scored by a model
  that never saw it), a time split, and at card level.

## Three questions, three mechanisms

| Question | How | Why |
|---|---|---|
| Identification: which card? | Rules: card number (after the account-number guard), "ending NN", "your … card ending NNNN", product name, PDF filename | Exact; statistics add nothing |
| Relevance: about a card the person has or applied for? | `p_rel = σ(−0.86 + Σ w_rel)` | Separates card mail from account mail, cross-sell and notices |
| Ownership: shows they hold one? | `p_own = σ(−2.07 + Σ w_own)` | Separates statements/alerts/lifecycle from applications and generic service mail |

## Weights (points; positive = more likely)

| Feature | where | relevance | ownership |
|---|---|---|---|
| card number the engine keeps | S/P/B | +2.50 | **+3.27** |
| account words (a/c, savings, debit card, deposit, UPI, KYC, insurance…) | S/P | −1.80 | −1.95 |
| application words | S/P | +1.16 | **−1.70** |
| billing words (amount/minimum due, due date, bill ready) | S/P | +0.91 | +1.50 |
| lifecycle words (dispatch, deliver, activate, PIN, renew, block, replace) | S/P | +0.48 | +1.40 |
| a product of the sending bank | S | +0.39 | +1.36 |
| "your … card ending NNNN" | S/P/B | +1.17 | +1.25 |
| a 2–3 digit card ending | S/P/B | +0.60 | +1.14 |
| "credit card" | S | +1.64 | +1.05 |
| a product of the sending bank | B only | +1.00 | +1.00 |
| Gmail Promotions tab | — | −1.69 | −0.98 |
| "statement" | S | +0.18 | +0.96 |
| bill-payment app (CRED, SaveSage…) | sender | +0.43 | +0.95 |
| "welcome" | S/P | +0.53 | +0.92 |
| "Dear Cardmember/Cardholder" | P/B | +1.48 | +0.86 |
| Most Important Terms / MITC | F/B | +1.08 | −0.81 |
| notice words (fraud, scam, maintenance, login, safety…) | S/P | −1.66 | −0.79 |
| payment received / refund | S/P | +0.24 | +0.74 |
| PDF has billing words | PDF | +0.62 | +0.74 |
| spend words only in the body (not S/P) | B | −0.76 | −0.64 |
| offer words (cashback, % off, voucher, upgrade to, 2nd card…) | S/P | −1.30 | −0.56 |
| "statement" only in the body | B | +0.51 | +0.55 |
| "card" | S | +0.89 | +0.48 |
| OTP | S | +0.18 | +0.33 |
| "Dear Applicant" | P/B | +0.37 | −0.24 |
| intercept | | −0.86 | −2.07 |

What the weights say, in words:
- A card number is the strongest single signal; it's what the engine's number guards protect.
- Where a word is matters: spend words in the subject/preview are mildly positive, the same words
  only in the body are **negative** (they come from ads and notices). The current engine's
  "body rule" typing is where most false cards came from.
- Applications are about cards (relevance +) but not held cards (ownership −): they should wait
  for dispatch/statement evidence, not raise a question.
- MITC footers come with card mail in general (relevance +) but more with applications and
  service mail than with transactions (ownership −).

## Card-level decision (per card the resolver builds)

| Decision | Rule |
|---|---|
| Wallet | identified, and (best `p_own` ≥ 0.70, or ≥ 2 emails with `p_own` ≥ 0.50) |
| Ask the user | identified, and best `p_own` ≥ 0.30 |
| Needs more evidence (no question) | only application emails so far |
| Supports only | generic service mail (`p_rel` high, `p_own` low): strengthens a card at that bank, never creates one |
| Hidden (kept in the dev panel) | anything else |

Deterministic guards stay in front of the score: the account-number guard (to add: "a/c no."),
the identifier guard (CRN, customer ID…), OTP in a subject counts only next to a card number,
and a product in an offer counts only in "your … card ending NNNN".

## Results on the study mailbox (out of sample)

| Check | Relevance | Ownership |
|---|---|---|
| Leave-one-bank-out AUC (full / lean features) | 0.934 / 0.934 | 0.918 / **0.936** |
| Time split AUC (trained before May 2026, tested after) | 0.95 | 0.98 |

Card level, each card scored by a model that never saw its bank:
- **20 of 20 real cards reach the wallet** (2 product-only groups would be asked about, but the
  same cards reach the wallet through their numbers).
- **0 false cards reach the wallet.** Of the non-card numbers left, one HDFC number is hidden and
  one Kotak term-deposit account ("deposit a/c no. XX9004") would become one question; the
  "a/c no." guard removes it.

## Limits, honestly

- One mailbox, 20 cards. Generic features and leave-one-bank-out reduce the risk, but the weights
  must be checked on other mailboxes before they're trusted (next: a second mailbox, scored
  without storing its text).
- The account / cross-sell / notice labels came partly from word rules similar to the features,
  so part of the agreement is built in. A 48-template audit sample is waiting for the owner; its
  error rate will say how much.
- The sample over-represents some kinds (PDF and random strata), so the intercepts reflect the
  sample, not a real mailbox's mix; the thresholds were chosen at card level for that reason.
- RBL's statements (no number, unusual layout) were the hardest bank for ownership; rules for
  identification matter more than weights there.
