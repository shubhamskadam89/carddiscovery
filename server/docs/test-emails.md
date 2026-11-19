# Test emails: try discovery without owning a card

Send these emails from your personal Gmail, then run discovery in test mode. Every card number is made up.

## How test mode works

```bash
.venv/bin/carddiscovery discover --test-sender you@gmail.com --name "Shubham Kadam" --show-unknown --fresh
```

- Mail **from** the `--test-sender` address is treated as bank mail. The bank is the first one the email names ("HDFC Bank", "SBI Card", "Amex", ...). If no bank is named, it shows as *Test Bank*.
- The signature check is skipped for these emails, because mail you send yourself carries no DKIM result.
- Put `[date:YYYY-MM-DD]` at the **start of the body** to make an email count as if it arrived on that date. That's how you test old and dormant cards.
- Personal mail from the same address is ignored unless it contains the word "card".
- Results from test runs are tagged. A normal run (without `--test-sender`) never includes them, so switching to real bank mail later just means dropping the flag.

You can send the emails to yourself (from your address to your address). Or send them from a second account to the one you connected, and pass that second address as `--test-sender`. Both work.

Send each email separately. Gmail may group ones with the same subject into a thread, which is fine.

## The emails

| # | Subject | Body | Tests |
|---|---|---|---|
| 1 | Your HDFC Bank Regalia Gold Credit Card Statement | Dear Shubham Kadam, your statement for card ending 1111 is ready. Total amount due Rs 12,340. | Active card |
| 2 | Your HDFC Bank Regalia Gold Credit Card Statement | [date:2026-10-05] Dear Shubham Kadam, your statement for card ending 1111 is ready. | Same card again, so still one card (no duplicate) |
| 3 | Welcome to the world of ICICI Bank Amazon Pay Credit Card | [date:2020-02-10] Dear Shubham Kadam, your Amazon Pay Credit Card ending 2222 is on its way. | Old welcome email |
| 4 | Transaction alert for your ICICI Bank Credit Card | [date:2023-06-01] INR 499 spent on ICICI Bank Card XX2222 at Amazon. | Rarely used card, so dormant |
| 5 | Your SBI Card ELITE Statement | [date:2020-04-03] Dear Shubham Kadam, statement for card ending 3333. | Last statement years ago, so dormant, not closed |
| 6 | Your Axis Bank ACE Credit Card Statement | Dear Shubham Kadam, statement for card ending 4444. | Two cards, same bank (1 of 2) |
| 7 | Your Axis Bank ACE Credit Card Statement | Dear Shubham Kadam, statement for card ending 5555. | Two cards, same bank (2 of 2) |
| 8 | Congratulations! Your Kotak White Credit Card is approved | [date:2022-08-20] Dear Shubham Kadam, your Kotak White Credit Card has been approved. | Approval only, no number, so you're asked to confirm |
| 9 | Your HDFC Bank Regalia Credit Card Statement | [date:2023-01-05] Dear Shubham Kadam, statement for card ending 6666. | Old card, before an upgrade |
| 10 | Your card has been upgraded | [date:2024-03-01] Your HDFC Bank Regalia Credit Card ending 6666 has been upgraded to Regalia Gold Credit Card. Your new card ending 7777 will be dispatched. | Upgrade to a new number |
| 11 | Your HDFC Bank Regalia Gold Credit Card Statement | Dear Shubham Kadam, statement for card ending 7777. | The new card after the upgrade |
| 12 | Your ICICI Bank Coral Credit Card Statement | [date:2021-05-05] Dear Shubham Kadam, statement for card ending 8888. | A card that was later closed |
| 13 | Your credit card has been closed | [date:2022-01-10] Your ICICI Bank Coral Credit Card ending 8888 has been closed as per your request. | Closed card |
| 14 | Apply now for HDFC Bank Infinia Credit Card | Exclusive offer: lifetime free for you. Apply now! | Promotion, so no card |
| 15 | You are pre-approved for an Axis Bank Atlas Credit Card | Get your card in minutes. Limited period offer. | Offer, so no card |
| 16 | Update on your IndusInd Bank Credit Card application | Dear Shubham Kadam, we regret to inform you that we are unable to approve your application. | Rejected application, so no card |
| 17 | Payment due reminder for your ICICI Bank Credit Card | Dear Priya Sharma, payment due for card ending 9191. | Someone else's card, so you're asked to confirm |
| 18 | Your HDFC Bank Regalia Gold Credit Card Statement | Dear Shubham Kadam, statement for card ending 1111. Add-on card ending 5656 transactions included. | Add-on card |
| 19 | OTP for your HDFC Bank Credit Card transaction | Your OTP is 482913 for card XX1111. | OTP, so ignored |
| 20 | Fwd: Your HDFC Bank Millennia Credit Card Statement | ---------- Forwarded message --------- Statement for card ending 3434. | Forwarded statement, so you're asked to confirm |
| 21 | Your Axis Bank Magnus Credit Card is inactive | [date:2025-09-01] Your Magnus Credit Card ending 1212 has not been used for over a year. | Inactivity warning, so "possibly closed" |

## What you should see

**YOUR CARDS**
- HDFC Regalia Gold ••1111: active
- HDFC Regalia Gold ••7777: active
- ICICI Amazon Pay ••2222: dormant
- SBI ELITE ••3333: dormant
- Axis ACE ••4444: active
- Axis ACE ••5555: active

**PLEASE CONFIRM**
- Kotak White (no number): approved, never seen
- ICICI ••9191: name mismatch
- HDFC ••5656: add-on
- HDFC Millennia ••3434: forwarded
- Axis Magnus ••1212: possibly closed

**HISTORY**
- HDFC Regalia ••6666: replaced, now ••7777
- ICICI Coral ••8888: closed

**APPLICATIONS**
- IndusInd: rejected

**Nothing** for emails 14, 15 and 19.

Anything different points to a gap in the rules. Run with `--show-unknown` and paste the output.

## Messy set

A second set of 14 harder emails lives in [tests/messy_emails.py](../tests/messy_emails.py). Each one has the scenario it tests and the result we expect. They cover: HTML tables, junk preview text, the card number only in the subject, Amex's 5-digit numbers, "XXXX-XXXX-XXXX-1234" and "xx6789" formats, an ad naming a card you hold, a debit alert with a credit-card ad, mixed Hindi and English, a points notice with no number, an upgrade worded as "converted", and "Yes Bank" in mixed case.

## Switching to real bank mail

Run the same command without `--test-sender`. Test results are left out automatically. Once you've finished testing, delete the test emails from Gmail.
