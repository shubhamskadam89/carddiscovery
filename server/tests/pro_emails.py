"""Professional, production-style HTML emails for a fresh end-to-end test.

Each one is a full email document the way large senders build them: doctype,
<head> with meta tags, a <style> block, an Outlook (MSO) conditional block, a
<script> block, a hidden preview line, a header bar, a summary table, sometimes a
transaction table and a cross-sell banner, then a legal footer.

Scripts never run in email clients. They're here as a trap: each one contains a
fake card number (••9999) and the words "statement" and "credit card". If the
parser ever read script content, a 9999 card would appear.

All names, numbers and addresses are made up. `[date:YYYY-MM-DD]` in the small
reference line makes a test email count as that old (test mode only).
"""

from __future__ import annotations

TRAP = "9999"


def _doc(bank: str, color: str, title: str, preview: str, body: str, banner: str = "") -> str:
    return f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" lang="en"><head>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting"><title>{title}</title>
<style type="text/css">body{{margin:0;padding:0;background:#f4f5f7;font-family:Arial,Helvetica,sans-serif;color:#222}}
table{{border-collapse:collapse}}.wrap{{width:600px;max-width:600px}}.k{{color:#666;font-size:12px}}.v{{font-weight:bold;font-size:14px}}
@media only screen and (max-width:620px){{.wrap{{width:100%!important}}}}</style>
<!--[if mso]><style>table,td{{font-family:Arial,sans-serif!important}}</style><![endif]-->
<script type="text/javascript">var cardNo="XXXX XXXX XXXX {TRAP}";var doc="credit card statement";function track(){{return cardNo+doc;}}</script>
</head><body>
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{preview}&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;</div>
<table role="presentation" width="100%" bgcolor="#f4f5f7"><tr><td align="center" style="padding:20px 10px">
<table role="presentation" class="wrap" width="600" bgcolor="#ffffff" style="border-radius:8px">
<tr><td style="background:{color};color:#ffffff;padding:18px 24px;font-size:20px;font-weight:bold">{bank}</td></tr>
<tr><td style="padding:24px">{body}</td></tr>{banner}
<tr><td style="padding:16px 24px;background:#fafafa;font-size:11px;color:#888;line-height:16px">
Never share your OTP, CVV or PIN with anyone. {bank} never asks for these details.
This is a system-generated email; please do not reply. Registered Office: 1 Example Marg, Mumbai 400013.
To unsubscribe from promotional emails, <a href="https://example.invalid/u">click here</a>. &copy; 2026 {bank}. All rights reserved.
</td></tr></table></td></tr></table></body></html>"""


def _table(rows: list[tuple[str, str]]) -> str:
    cells = "".join(f'<tr><td class="k" style="padding:8px 0;border-bottom:1px solid #eee">{k}</td>'
                    f'<td class="v" align="right" style="padding:8px 0;border-bottom:1px solid #eee">{v}</td></tr>'
                    for k, v in rows)
    return f'<table role="presentation" width="100%">{cells}</table>'


def _txns(rows: list[tuple[str, str, str]]) -> str:
    cells = "".join(f'<tr><td style="padding:6px 0;font-size:12px">{d}</td><td style="font-size:12px">{m}</td>'
                    f'<td align="right" style="font-size:12px">{a}</td></tr>' for d, m, a in rows)
    return (f'<table role="presentation" width="100%" style="margin-top:14px"><tr><th align="left" class="k">Date</th>'
            f'<th align="left" class="k">Description</th><th align="right" class="k">Amount</th></tr>{cells}</table>')


def _banner(text: str, cta: str) -> str:
    return (f'<tr><td style="padding:16px 24px;background:#eef4ff"><table role="presentation" width="100%"><tr>'
            f'<td style="font-size:13px">{text}</td><td align="right"><a href="https://example.invalid/o" '
            f'style="background:#1a56db;color:#fff;padding:9px 14px;border-radius:6px;text-decoration:none">{cta}</a></td>'
            f'</tr></table></td></tr>')


def _ref(date: str | None = None) -> str:
    tag = f"[date:{date}] " if date else ""
    return f'<p style="font-size:11px;color:#999;margin:0 0 12px">{tag}Ref: CC/2026/{abs(hash(date)) % 100000:05d}</p>'


PRO = [
    dict(id="P1", subject="Your HDFC Bank Regalia Gold Credit Card Statement - November 2026",
         html=_doc("HDFC Bank", "#004c8f", "Credit Card Statement", "Total amount due Rs. 28,640.00 by 25 Nov",
                   _ref() + "<p>Dear Shubham Kadam,</p><p>Your Regalia Gold Credit Card statement for 06 Oct 2026 "
                   "to 05 Nov 2026 is ready.</p>" + _table([
                       ("Card number", "4893 XXXX XXXX 7410"), ("Statement date", "05 Nov 2026"),
                       ("Payment due date", "25 Nov 2026"), ("Total amount due", "Rs. 28,640.00"),
                       ("Minimum amount due", "Rs. 1,432.00")]) + _txns([
                       ("12 Oct", "SWIGGY BANGALORE", "Rs. 642.00"), ("19 Oct", "MAKEMYTRIP", "Rs. 18,210.00"),
                       ("02 Nov", "AMAZON PAY", "Rs. 9,788.00")]),
                   _banner("You're eligible to upgrade to the <b>Infinia Metal Credit Card</b>.", "Upgrade now")),
         tests="Full statement with a transaction table and an upgrade banner for another card",
         expect={("hdfc", "7410"): ("owned", "Regalia Gold")}),
    dict(id="P2", subject="Welcome to the world of ICICI Bank Amazon Pay Credit Card",
         html=_doc("ICICI Bank", "#b02a30", "Welcome", "Your Amazon Pay Credit Card is on its way",
                   _ref("2020-02-10") + "<p>Dear Shubham Kadam,</p><p>Welcome aboard! Your Amazon Pay Credit Card "
                   "ending 2222 has been dispatched and will reach you within 7 working days.</p>" + _table([
                       ("Card", "Amazon Pay Credit Card XX2222"), ("Joining fee", "Nil (lifetime free)"),
                       ("Rewards", "5% back on Amazon for Prime members")])),
         tests="Old welcome email (2020)",
         expect={("icici", "2222"): ("owned", "Amazon Pay")}),
    dict(id="P3", subject="Transaction alert for your ICICI Bank Credit Card",
         html=_doc("ICICI Bank", "#b02a30", "Transaction Alert", "INR 499.00 spent on your card",
                   _ref("2023-06-01") + "<p>Dear Customer,</p><p>Your ICICI Bank Credit Card XX2222 has been used "
                   "for INR 499.00 at AMAZON.</p>" + _table([
                       ("Card", "XX2222"), ("Amount", "INR 499.00"), ("Merchant", "AMAZON"),
                       ("Available limit", "INR 1,49,501.00")])),
         tests="Old alert on the same card (2023): no new card",
         expect=None),
    dict(id="P4", subject="Your SBI Card ELITE Monthly Statement",
         html=_doc("SBI Card", "#0f4c81", "Monthly Statement", "Your statement is ready",
                   _ref("2020-04-03") + "<p>Dear Shubham Kadam,</p><p>Your SBI Card ELITE statement is now "
                   "available.</p>" + _table([
                       ("Card number", "XXXX XXXX XXXX 3333"), ("Statement date", "03 Apr 2020"),
                       ("Total amount due", "Rs. 4,210.00")])),
         tests="Last statement years ago: dormant, not closed",
         expect={("sbi", "3333"): ("owned", "ELITE")}),
    dict(id="P5", subject="Important information about your Axis Bank Credit Card",
         html=_doc("Axis Bank", "#97144d", "Annual Fee Intimation", "Annual fee for your Magnus Credit Card",
                   _ref() + "<p>Dear Shubham Kadam,</p><p>The annual fee of Rs. 12,500 + GST for your Axis Bank "
                   "Magnus Credit Card ending 6158 will be charged in your next statement.</p>" + _table([
                       ("Card", "Magnus Credit Card XX6158"), ("Annual fee", "Rs. 12,500 + GST"),
                       ("Waiver on annual spends of", "Rs. 25,00,000"), ("Your spends (12 months)", "Rs. 0")])),
         tests="Annual fee notice for an unused card, generic subject",
         expect={("axis", "6158"): ("owned", "Magnus")}),
    dict(id="P6", subject="Your HDFC Bank Regalia Credit Card Statement",
         html=_doc("HDFC Bank", "#004c8f", "Credit Card Statement", "Statement for card ending 6666",
                   _ref("2023-01-05") + "<p>Dear Shubham Kadam,</p><p>Your Regalia Credit Card statement is "
                   "ready.</p>" + _table([("Card number", "XXXX XXXX XXXX 6666"),
                                          ("Total amount due", "Rs. 7,300.00")])),
         tests="Old card before an upgrade (2023)",
         expect={("hdfc", "6666"): ("history", "Regalia")}),
    dict(id="P7", subject="Your card has been upgraded",
         html=_doc("HDFC Bank", "#004c8f", "Card Upgrade", "Your upgrade is complete",
                   _ref("2024-03-01") + "<p>Dear Shubham Kadam,</p><p>Your HDFC Bank Regalia Credit Card ending "
                   "6666 has been upgraded to Regalia Gold Credit Card. Your new card ending 7777 will be "
                   "dispatched shortly.</p>" + _table([("Old card", "Regalia XX6666"),
                                                       ("New card", "Regalia Gold XX7777")])),
         tests="Upgrade to a new number (2024)",
         expect={("hdfc", "7777"): ("owned", "Regalia Gold")}),
    dict(id="P8", subject="Your HDFC Bank Regalia Gold Credit Card Statement - October 2026",
         html=_doc("HDFC Bank", "#004c8f", "Credit Card Statement", "Statement for card ending 7777",
                   _ref() + "<p>Dear Shubham Kadam,</p><p>Your Regalia Gold Credit Card statement is ready.</p>"
                   + _table([("Card number", "XXXX XXXX XXXX 7777"), ("Total amount due", "Rs. 11,980.00")])),
         tests="Current statement for the upgraded card",
         expect=None),
    dict(id="P9", subject="Confirmation of closure of your Kotak Credit Card",
         html=_doc("Kotak Mahindra Bank", "#ed1c24", "Card Closure", "Your credit card account has been closed",
                   _ref() + "<p>Dear Shubham Kadam,</p><p>As requested, your Kotak League Platinum Credit Card "
                   "ending 2297 has been closed with effect from 10 Nov 2026. A No Dues Certificate is attached.</p>"
                   + _table([("Card", "League Platinum XX2297"), ("Outstanding", "Rs. 0.00")]),
                   _banner("Come back anytime: the <b>Kotak White Credit Card</b> has a 10,000-point welcome bonus.",
                           "Know more")),
         tests="Closure letter with a win-back banner",
         expect={("kotak", "2297"): ("history", "League Platinum")}),
    dict(id="P10", subject="Your American Express statement is ready",
         html=_doc("American Express", "#006fcf", "Statement", "Your November statement is available",
                   _ref() + "<p>Dear Shubham Kadam,</p><p>Your American Express Membership Rewards Credit Card "
                   "statement is now available.</p>" + _table([
                       ("Card Member", "SHUBHAM KADAM"), ("Account ending", "51004"),
                       ("Closing balance", "Rs. 18,775.00"), ("Membership Rewards points", "23,410")]),
                   _banner("Upgrade to the <b>American Express Platinum Travel Credit Card</b>.", "Upgrade")),
         tests="Amex 5-digit account ending in a table, upgrade banner",
         expect={("amex", "1004"): ("owned", "Membership Rewards")}),
    dict(id="P11", subject="ICICI Bank Sapphiro Credit Card Statement - Nov 2026",
         html=_doc("ICICI Bank", "#b02a30", "Credit Card Statement", "Statement for card ending 4730",
                   _ref() + "<p>Dear SHUBHAM KADAM,</p><p>Your ICICI Bank Sapphiro Credit Card statement is "
                   "ready.</p><p><b>Primary Card: XXXX XXXX XXXX 4730 (SHUBHAM KADAM)</b></p>"
                   + _txns([("02 Nov", "IRCTC", "Rs. 2,310.00")])
                   + "<p><b>Add-on Card: XXXX XXXX XXXX 9012 (PRIYA KADAM)</b></p>"
                   + _txns([("04 Nov", "MYNTRA", "Rs. 4,199.00")])
                   + _table([("Total amount due", "Rs. 6,509.00")])),
         tests="Primary card and add-on card sections",
         expect={("icici", "4730"): ("owned", "Sapphiro"), ("icici", "9012"): ("review", None)}),
    dict(id="P12", subject="Discover a card made for you",
         html=_doc("HDFC Bank", "#004c8f", "Offers", "Lifetime free cards, instant approval",
                   "<h2 style='margin:0 0 12px'>Find your perfect card</h2>" + _table([
                       ("Infinia Credit Card", "5X rewards on travel"),
                       ("Millennia Credit Card", "5% cashback online")])
                   + "<p>Apply now and get instant approval. Limited period offer.</p>"),
         tests="Marketing for cards the user doesn't hold: must create nothing",
         expect=None),
    dict(id="P13", subject="Your HDFC Bank Debit Card ending 5544 was used",
         html=_doc("HDFC Bank", "#004c8f", "Debit Card Alert", "Rs. 300.00 debited from your account",
                   "<p>Dear Customer,</p><p>Rs 300.00 was debited from your savings account using your HDFC Bank "
                   "Debit Card ending 5544 at BIGBASKET.</p>",
                   _banner("Want more rewards? Apply for an <b>HDFC Bank Credit Card</b> today!", "Apply")),
         tests="Debit card alert with a credit-card ad: must create nothing",
         expect=None),
    dict(id="P14", subject="Update on your IndusInd Bank Credit Card application",
         html=_doc("IndusInd Bank", "#8b1d41", "Application Status", "An update on your application",
                   "<p>Dear Shubham Kadam,</p><p>Thank you for your interest. We regret to inform you that we are "
                   "unable to approve your credit card application at this time.</p>"),
         tests="Rejected application: listed, never a card",
         expect=None),
]

# Second batch: arrives after a first scan of P1–P10, to test re-scanning.
PRO += [
    dict(id="P15", subject="Your Axis Bank Atlas Credit Card has been dispatched",
         html=_doc("Axis Bank", "#97144d", "Card Dispatched", "Your new card is on its way",
                   _ref() + "<p>Dear Shubham Kadam,</p><p>Your Axis Bank Atlas Credit Card ending 8531 has been "
                   "dispatched via Blue Dart (AWB 5512903347) and will reach you in 3 to 5 working days.</p>"
                   + _table([("Card", "Atlas Credit Card XX8531"), ("Courier", "Blue Dart"),
                             ("Expected delivery", "21 Nov 2026")])),
         tests="New card dispatched (recent): new and active",
         expect={("axis", "8531"): ("owned", "Atlas")}),
    dict(id="P16", subject="Annual fee charged on your SBI Card",
         html=_doc("SBI Card", "#0f4c81", "Annual Fee", "Annual fee of Rs. 4,999 + GST charged",
                   _ref() + "<p>Dear Shubham Kadam,</p><p>An annual fee of Rs. 4,999 + GST has been charged to your "
                   "SBI Card ELITE ending 3333 for the membership year starting November 2026.</p>"
                   + _table([("Card", "SBI Card ELITE XX3333"), ("Annual fee", "Rs. 4,999 + GST")])),
         tests="Fee notice for the old, unused SBI card: still yours, not used recently",
         expect=None),
    dict(id="P17", subject="Transaction alert for your HDFC Bank Credit Card",
         html=_doc("HDFC Bank", "#004c8f", "Transaction Alert", "Rs. 1,250.00 spent on your card",
                   _ref() + "<p>Dear Customer,</p><p>Rs. 1,250.00 was spent on your HDFC Bank Credit Card "
                   "ending 7410 at SWIGGY on 15 Nov 2026.</p>"
                   + _table([("Card", "XX7410"), ("Amount", "Rs. 1,250.00"), ("Merchant", "SWIGGY")])),
         tests="Fresh alert on a card already found: no new card",
         expect=None),
    dict(id="P18", subject="Your YES BANK Credit Card statement",
         html=_doc("YES BANK", "#00518f", "Credit Card Statement", "Your statement is ready",
                   _ref("2021-08-05") + "<p>Dear Shubham Kadam,</p><p>Your YES Bank Marquee Credit Card "
                   "statement is ready.</p>" + _table([("Card number", "XXXX XXXX XXXX 4321"),
                                                       ("Total amount due", "Rs. 2,140.00")])),
         tests="Old statement from another bank (2021): not used recently",
         expect={("yes", "4321"): ("owned", "Marquee")}),
    dict(id="P19", subject="Payment due reminder for your ICICI Bank Credit Card",
         html=_doc("ICICI Bank", "#b02a30", "Payment Reminder", "Payment due on 20 Nov 2026",
                   _ref() + "<p>Dear Priya Sharma,</p><p>The payment for your ICICI Bank Credit Card ending 9191 "
                   "is due on 20 Nov 2026.</p>" + _table([("Card", "XX9191"), ("Total amount due", "Rs. 3,450.00")])),
         tests="Someone else's card (name mismatch): asks to confirm",
         expect={("icici", "9191"): ("review", None)}),
    dict(id="P20", subject="Your Kotak Credit Card has not been used",
         html=_doc("Kotak Mahindra Bank", "#ed1c24", "Card Inactivity", "Use your card to keep it active",
                   _ref("2025-09-01") + "<p>Dear Shubham Kadam,</p><p>Your Kotak White Credit Card ending 1593 "
                   "has not been used for over a year. As per RBI guidelines, an unused card may be closed after "
                   "intimation. Use it within 30 days to keep it active.</p>"),
         tests="Inactivity warning (2025): may be closed, asks if you still have it",
         expect={("kotak", "1593"): ("review", "White")}),
]

BATCH_1 = [e for e in PRO if e["id"] in {f"P{i}" for i in range(1, 11)}]
BATCH_2 = [e for e in PRO if e not in BATCH_1]

# Applications the full set should produce.
EXPECTED_APPLICATIONS = [("indusind", "rejected")]
