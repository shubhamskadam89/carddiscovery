"""Batch 3: a precision test. 2 credit-card emails, 5 debit-card emails and 4 promotions.

Only the 2 credit cards may appear. The debit emails include a "new debit card
dispatched" and a "debit card activated" email, which read exactly like new
credit-card mail. Each entry has `body` (plain text) or `html`.
"""

from __future__ import annotations

from tests.pro_emails import _banner, _doc, _ref, _table

BATCH_3 = [
    # ---- credit cards (2)
    dict(id="C1", kind="credit", subject="Your HDFC Bank Millennia Credit Card Statement - November 2026",
         html=_doc("HDFC Bank", "#004c8f", "Credit Card Statement", "Total amount due Rs. 9,412.00",
                   _ref() + "<p>Dear Shubham Kadam,</p><p>Your Millennia Credit Card statement for 06 Oct 2026 "
                   "to 05 Nov 2026 is ready.</p>" + _table([
                       ("Card number", "XXXX XXXX XXXX 2846"), ("Total amount due", "Rs. 9,412.00"),
                       ("Payment due date", "25 Nov 2026"), ("CashPoints earned", "471")]),
                   _banner("Get the <b>HDFC Bank Infinia Credit Card</b> by invitation.", "Know more")),
         expect={("hdfc", "2846"): ("owned", "Millennia")}),
    dict(id="C2", kind="credit", subject="Welcome to Axis Bank ACE Credit Card",
         body="Dear Shubham Kadam,\n\nWelcome to the Axis Bank family! Your ACE Credit Card ending 5719 has been "
              "dispatched and will reach you in 5-7 working days.\n\nCard: ACE Credit Card XX5719\n"
              "Joining fee: Rs. 499 + GST (waived on spends of Rs. 10,000 in 45 days)\n\nAxis Bank Cards",
         expect={("axis", "5719"): ("owned", "ACE")}),

    # ---- debit cards (5): none may become a card
    dict(id="D1", kind="debit", subject="HDFC Bank Debit Card Transaction Alert",
         body="Dear Customer,\n\nTransaction Type: POS Purchase\nCard: Debit Card XX5544\n"
              "Account: Savings A/c XX1122\nAmount: Rs. 1,240.00\nMerchant: DMART THANE\nDate: 15-11-2026\n\n"
              "Not you? Call 1800 202 6161. Get more rewards with an HDFC Bank Credit Card!",
         expect=None),
    dict(id="D2", kind="debit", subject="ICICI Bank Debit Card: ATM cash withdrawal",
         html=_doc("ICICI Bank", "#b02a30", "Debit Card Alert", "Rs. 5,000 withdrawn from your account",
                   "<p>Dear Customer,</p><p>Rs. 5,000.00 was withdrawn using your ICICI Bank Debit Card ending "
                   "8102 at ATM S1AW0123, Pune.</p>" + _table([
                       ("Card", "Debit Card XX8102"), ("Account", "Savings A/c XX4410"),
                       ("Available balance", "Rs. 42,118.50")])),
         expect=None),
    dict(id="D3", kind="debit", subject="Axis Bank Debit Card used for online purchase",
         body="Dear Customer, your Axis Bank Debit Card ending 3391 was used for Rs. 799.00 at AMAZON on "
              "15-Nov-2026. Available balance in A/c XX7765: Rs. 18,402.10. If not done by you, call 1860 419 5555.",
         expect=None),
    dict(id="D4", kind="debit", subject="Your new Kotak Debit Card has been dispatched",
         html=_doc("Kotak Mahindra Bank", "#ed1c24", "Debit Card Dispatched", "Your new debit card is on its way",
                   "<p>Dear Shubham Kadam,</p><p>Your new Kotak Debit Card ending 6604, linked to your Savings "
                   "Account XX2231, has been dispatched and will reach you in 5 working days.</p>"
                   "<p>Activate it from the Kotak app and set your PIN before first use.</p>"),
         expect=None),
    dict(id="D5", kind="debit", subject="Welcome! Your YES BANK Debit Card is now active",
         body="Dear Shubham Kadam,\nCongratulations! Your YES BANK Debit Card ending 7720 has been activated "
              "successfully and your PIN is set. Enjoy contactless payments up to Rs. 5,000.\n-YES BANK",
         expect=None),

    # ---- promotions (4)
    dict(id="R1", kind="promo", subject="You're pre-approved: American Express Platinum Travel Credit Card",
         html=_doc("American Express", "#006fcf", "Pre-approved Offer", "Earn travel vouchers worth Rs. 11,800",
                   "<p>Dear Shubham,</p><p>You are pre-approved for the American Express Platinum Travel Credit "
                   "Card. Apply in 2 minutes and earn vouchers worth Rs. 11,800 every year.</p>",
                   _banner("Limited period offer: first year fee waived.", "Apply now")),
         expect=None),
    dict(id="R2", kind="promo", subject="Lifetime FREE: IDFC FIRST Wealth Credit Card",
         body="Get the IDFC FIRST Wealth Credit Card - lifetime free, 10X rewards, unlimited lounge access.\n"
              "Apply now in 5 minutes: https://example.invalid/apply\nT&C apply.",
         expect=None),
    dict(id="R3", kind="promo", subject="Exclusive for you: 10X CashPoints on your Millennia Credit Card",
         body="This festive season, earn 10X CashPoints on your HDFC Bank Millennia Credit Card ending 2846 "
              "at SmartBuy. Offer valid till 31 Dec 2026. T&C apply.",
         expect=None),
    dict(id="R4", kind="promo", subject="Upgrade to SBI Card ELITE today",
         body="Dear Customer, upgrade to SBI Card ELITE and enjoy complimentary movie tickets worth Rs. 6,000 "
              "every year. Limited period offer. Apply now!",
         expect=None),
]

EXPECTED = {k: v for e in BATCH_3 if e["expect"] for k, v in e["expect"].items()}
