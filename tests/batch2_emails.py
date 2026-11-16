"""Batch 2: ten emails in mixed styles, sent after a first scan of pro_emails.BATCH_1.

Styles: professional HTML, messy plain text, structured "Key: Value" text, an HTML
key/value table, short HTML with split numbers, and an all-caps SMS-style email.
Each entry has either `body` (plain text) or `html`. `expect` uses the same format
as the other kits; the combined expectation for BATCH_1 + BATCH_2 is EXPECTED_ALL.
"""

from __future__ import annotations

from tests.pro_emails import PRO

_P11 = next(e for e in PRO if e["id"] == "P11")

BATCH_2 = [
    dict(id="B1", style="professional HTML", subject=_P11["subject"], html=_P11["html"],
         tests="Statement with primary and add-on card sections",
         expect={("icici", "4730"): ("owned", "Sapphiro"), ("icici", "9012"): ("review", None)}),

    dict(id="B2", style="messy plain text", subject="Your card is on its way!!",
         body="View in browser | Unsubscribe | Contact us | Download our app\n\n"
              "Hi Shubham Kadam,\n\ngreat news - your new Axis Bank ATLAS credit card   has been DISPATCHED "
              "via BlueDart (AWB: 5512903347).\nCard No: XXXX-XXXX-XXXX-8531\n"
              "expected delivery: 21/11/2026\n\n--\nAxis Bank Cards Team\nPlease do not reply to this email.",
         tests="Junk preview line, odd capitals and spacing, dashed mask",
         expect={("axis", "8531"): ("owned", "Atlas")}),

    dict(id="B3", style="structured text", subject="Annual Fee Intimation - SBI Card",
         body="Dear Shubham Kadam,\n\nPlease find the details of the annual fee charged to your card.\n\n"
              "Card Name: SBI Card ELITE\nCard Number: XXXX XXXX XXXX 3333\nFee Type: Annual Fee\n"
              "Amount: Rs. 4,999.00 + GST\nCharged On: 05-Nov-2026\nWaiver Criteria: Annual spends of Rs. 10 Lakh\n\n"
              "Regards,\nSBI Card",
         tests="Plain Key: Value lines, fee for the old unused card",
         expect=None),

    dict(id="B4", style="short HTML, split number", subject="Transaction alert: HDFC Bank Credit Card",
         html='<p>Dear Customer,</p><p>Rs. <b>1,250.00</b> was spent on your HDFC Bank Credit Card ending '
              '<span style="font-weight:bold">74</span><span>10</span> at <b>SWIGGY</b> on 15-11-2026.</p>'
              '<p style="font-size:11px;color:#888">Not you? SMS BLOCK 7410 to 5676712.</p>',
         tests="Card number split across tags, on a card already found",
         expect=None),

    dict(id="B5", style="messy plain text", subject="Important: your Yes Bank credit card statement",
         body="[date:2021-08-05] dear SHUBHAM KADAM ,\nyour Yes Bank Marquee credit card statement for jul-2021 "
              "is attached.card ending  4321 . total due rs.2,140 /- . min due rs 107/-\n"
              "yes, you can pay online via netbanking/upi.\nthanks\nyes bank",
         tests="Lower case, missing spaces, double spaces, old date",
         expect={("yes", "4321"): ("owned", "Marquee")}),

    dict(id="B6", style="structured HTML table", subject="Payment Due Reminder - ICICI Bank Credit Card",
         html='<table border="1" cellpadding="6" style="border-collapse:collapse;font-family:Arial">'
              '<tr><th colspan="2" style="background:#b02a30;color:#fff">ICICI Bank - Payment Reminder</th></tr>'
              '<tr><td>Customer Name</td><td>Priya Sharma</td></tr>'
              '<tr><td>Card Number</td><td>XXXX XXXX XXXX 9191</td></tr>'
              '<tr><td>Total Amount Due</td><td>Rs. 3,450.00</td></tr>'
              '<tr><td>Due Date</td><td>20-Nov-2026</td></tr></table>'
              '<p>Dear Priya Sharma, please pay before the due date to avoid late charges.</p>',
         tests="Key/value HTML table for someone else's card",
         expect={("icici", "9191"): ("review", None)}),

    dict(id="B7", style="all-caps SMS style", subject="KOTAK CREDIT CARD - INACTIVITY NOTICE",
         body="[date:2025-09-01] DEAR CUSTOMER, YOUR KOTAK WHITE CREDIT CARD XX1593 HAS NOT BEEN USED FOR OVER "
              "12 MONTHS. AS PER RBI GUIDELINES THE CARD MAY BE CLOSED AFTER 30 DAYS. TO KEEP IT ACTIVE, USE IT "
              "ONCE. -KOTAK MAHINDRA BANK",
         tests="All capitals, SMS-style wording, RBI wording inside the actual message",
         expect={("kotak", "1593"): ("review", "White")}),

    dict(id="B8", style="messy promo", subject="LAST CHANCE!!! Lifetime FREE HDFC Bank Credit Card",
         body="Hurry!!! Get the HDFC Bank Infinia Credit Card or Millennia Credit Card - LIFETIME FREE.\n"
              "Instant approval in 5 mins. Apply now: https://example.invalid/apply\nT&C apply.",
         tests="Shouting marketing for cards the user doesn't hold",
         expect=None),

    dict(id="B9", style="structured text", subject="HDFC Bank Debit Card Alert",
         body="Dear Customer,\n\nTransaction Type: Debit Card Purchase\nCard: Debit Card XX5544\n"
              "Account: Savings A/c XX1122\nAmount: Rs. 300.00\nMerchant: BIGBASKET\n\n"
              "Get more rewards with an HDFC Bank Credit Card! Apply today.",
         tests="Debit-card alert in Key: Value form with a credit-card ad",
         expect=None),

    dict(id="B10", style="plain text", subject="Your credit card application - IndusInd Bank",
         body="Dear Shubham Kadam,\nThank you for applying for an IndusInd Bank Credit Card. After careful review, "
              "we regret to inform you that we are unable to approve your application at this time.\n"
              "IndusInd Bank Cards",
         tests="Rejected application in plain text",
         expect=None),
]

# Everything the app should show after scanning BATCH_1 and then BATCH_2.
EXPECTED_ALL = {
    ("amex", "1004"): ("owned", "Membership Rewards"),
    ("axis", "6158"): ("owned", "Magnus"),
    ("axis", "8531"): ("owned", "Atlas"),
    ("hdfc", "7410"): ("owned", "Regalia Gold"),
    ("hdfc", "7777"): ("owned", "Regalia Gold"),
    ("icici", "2222"): ("owned", "Amazon Pay"),
    ("icici", "4730"): ("owned", "Sapphiro"),
    ("sbi", "3333"): ("owned", "ELITE"),
    ("yes", "4321"): ("owned", "Marquee"),
    ("icici", "9012"): ("review", None),
    ("icici", "9191"): ("review", None),
    ("kotak", "1593"): ("review", "White"),
    ("hdfc", "6666"): ("history", "Regalia"),
    ("kotak", "2297"): ("history", "League Platinum"),
}
EXPECTED_APPLICATIONS = [("indusind", "rejected")]
