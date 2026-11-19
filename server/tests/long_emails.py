"""Long, formal HTML emails modelled on how Indian card issuers write: a hidden preview
line, a branded header, a summary table, then cross-sell banners, security tips,
RBI notices, grievance contacts and a long legal footer.

The boilerplate is the point. It mentions other card products, "OTP", "closed",
"not been used", "regret", masked account numbers and many digits, all of which
can mislead rules that work on short emails.

All names, numbers and addresses are made up. `expect` follows the same format as
the other kits.
"""

from __future__ import annotations

_HEAD = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style type="text/css">
body{margin:0;padding:0;background:#f2f2f2;font-family:Arial,Helvetica,sans-serif;color:#333}
table{border-collapse:collapse} td{font-size:13px;line-height:19px}
.hdr{background:#004c8f;color:#fff;font-size:18px;padding:16px}
.small{font-size:10px;color:#777;line-height:14px} .btn{background:#ed232a;color:#fff;padding:10px 18px}
@media only screen and (max-width:600px){ .w{width:100%!important} }
</style>
<!--[if mso]><style>td{font-family:Arial,sans-serif}</style><![endif]-->
</head><body>"""

_PIXEL = '<img src="https://track.example.invalid/open?id=8f3a2c91&amp;c=cc_stmt" width="1" height="1" alt="">'


def _preheader(text: str) -> str:
    filler = "&zwnj;&nbsp;" * 60
    return f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all">{text}{filler}</div>'


def _header(bank: str, title: str) -> str:
    return f"""<table width="640" align="center" class="w" bgcolor="#ffffff">
<tr><td class="hdr"><b>{bank}</b> &nbsp;|&nbsp; {title}</td></tr>
<tr><td style="padding:6px 16px" class="small" align="right">
<a href="https://example.invalid/view">View this email in your browser</a></td></tr>"""


def _security(bank: str) -> str:
    return f"""<tr><td style="padding:12px 16px;background:#fff7e6">
<b>Stay safe from fraud</b><br>
{bank} will never ask for your Card number, CVV, Expiry date, OTP, PIN or Net Banking password
over phone, SMS, email or social media. Do not share your OTP with anyone, including bank staff.
If you have received such a call, report it immediately on 1800-000-0000. To block your card
instantly, SMS BLOCK followed by the last 4 digits of your card to 5676700. Report unauthorised
transactions within 3 working days to limit your liability as per RBI guidelines.
</td></tr>"""


def _rbi_notice() -> str:
    return """<tr><td style="padding:12px 16px" class="small">
<b>Important regulatory information:</b> As per the Reserve Bank of India (Credit Card and Debit
Card - Issuance and Conduct) Directions, 2022, if a credit card has not been used for a period of
more than one year, the process to close the card shall be initiated after intimating the
cardholder. If no reply is received within 30 days, the card account shall be closed, subject to
payment of all dues. Cardholders can request closure of their credit card at any time through any
of the available channels. Please refer to the Most Important Terms and Conditions (MITC) for fees
and charges. Goods and Services Tax (GST) at 18% is applicable on all fees, interest and charges.
</td></tr>"""


def _grievance(bank: str) -> str:
    return f"""<tr><td style="padding:12px 16px" class="small">
<b>Grievance Redressal:</b> For any complaint, write to the Grievance Redressal Officer, {bank},
Cards Division, 4th Floor, Example Towers, Plot No. 12, Sector 3, Andheri (East), Mumbai - 400093.
Phone: 022-6000 0000 (Mon-Sat, 9:30 AM to 6:30 PM). We regret any inconvenience caused and will
respond within 7 working days. If the complaint is not resolved within 30 days, you may approach
the RBI Ombudsman at https://cms.rbi.org.in.
</td></tr>"""


def _footer(bank: str) -> str:
    return f"""<tr><td style="padding:14px 16px;background:#eeeeee" class="small">
This is a system-generated email. Please do not reply to this email. {bank} is a registered
trademark. Registered Office: Example House, 1 Example Marg, Lower Parel, Mumbai 400013.
CIN: U00000MH2000PLC000000. Toll-free: 1800 000 0000. Email: support@example.invalid<br>
You are receiving this email because you are a {bank} customer. To stop receiving promotional
emails, <a href="https://example.invalid/unsub?u=SK8989">unsubscribe here</a>. Transactional and
regulatory communication will continue to be sent. &copy; 2026 {bank}. All rights reserved.
Disclaimer: The information in this email is confidential and intended solely for the addressee.
If you are not the intended recipient, please delete it and notify the sender. Offers are at the
sole discretion of the issuer and third-party merchants. T&amp;C apply.
</td></tr></table>{_PIXEL}</body></html>"""


def _cross_sell(text: str, cta: str = "Apply Now") -> str:
    return f"""<tr><td style="padding:14px 16px;background:#f5f9ff">
<table width="100%"><tr><td>{text}</td>
<td align="right"><a class="btn" href="https://example.invalid/offer">{cta}</a></td></tr></table>
</td></tr>"""


def _wrap(bank: str, title: str, preheader: str, body: str, cross: str = "") -> str:
    return (_HEAD + _preheader(preheader) + _header(bank, title) + body + cross
            + _security(bank) + _rbi_notice() + _grievance(bank) + _footer(bank))


def _rows(pairs: list[tuple[str, str]]) -> str:
    return "".join(f'<tr><td style="padding:4px 8px;border:1px solid #ddd">{k}</td>'
                   f'<td style="padding:4px 8px;border:1px solid #ddd"><b>{v}</b></td></tr>' for k, v in pairs)


LONG = [
    dict(
        id="L1",
        subject="Your HDFC Bank Regalia Gold Credit Card Statement for October 2026",
        html=_wrap(
            "HDFC Bank", "Credit Card e-Statement", "Your statement is ready. Total Amount Due Rs. 32,418.60",
            f"""<tr><td style="padding:16px">Dear SHUBHAM KADAM,<br><br>
Thank you for being a valued HDFC Bank Credit Card customer. Your Regalia Gold Credit Card
statement for the period 06/10/2026 to 05/11/2026 has been generated and is attached to this
email as a password-protected PDF. The password is the first 4 letters of your name in capitals
followed by the date and month of your birth (DDMM).<br><br>
<table width="100%">{_rows([
    ("Card Number", "4893 XXXX XXXX 7410"),
    ("Statement Date", "05/11/2026"), ("Payment Due Date", "25/11/2026"),
    ("Total Amount Due", "Rs. 32,418.60"), ("Minimum Amount Due", "Rs. 1,621.00"),
    ("Credit Limit", "Rs. 4,50,000.00"), ("Available Credit Limit", "Rs. 4,17,581.40"),
    ("Reward Points Earned this cycle", "1,284"), ("Reward Points Balance", "48,906"),
])}</table><br>
Pay using HDFC Bank NetBanking, PayZapp, UPI or BillDesk. Payments made after the due date will
attract late payment charges as per the schedule of charges. Interest at 3.75% per month (45% per
annum) will be levied if the Total Amount Due is not paid in full.
</td></tr>""",
            _cross_sell("<b>You're eligible for an upgrade!</b> Get the Infinia Metal Credit Card with "
                        "unlimited airport lounge access and 5X reward points.", "Upgrade Now")
            + _cross_sell("<b>Pre-approved Personal Loan</b> of up to Rs. 15,00,000 at attractive "
                          "interest rates. Instant disbursal to your Savings A/c XX4411.", "Get Loan"),
        ),
        tests="Statement with an upgrade banner for a different card and a loan banner with an account number",
        expect={("hdfc", "7410"): ("owned", "Regalia Gold")},
    ),
    dict(
        id="L2",
        subject="Transaction alert for your ICICI Bank Credit Card",
        html=_wrap(
            "ICICI Bank", "Transaction Alert", "INR 4,560.00 spent on your ICICI Bank Credit Card",
            f"""<tr><td style="padding:16px">Dear Customer,<br><br>
Your ICICI Bank Credit Card XX5821 has been used for a transaction of INR 4,560.00 on
13-Nov-2026 at 21:14:52 IST at AMAZON PAY INDIA PVT LTD. The available credit limit on your card
is now INR 1,86,240.00.<br><br>
<table width="100%">{_rows([
    ("Card", "XX5821"), ("Amount", "INR 4,560.00"), ("Merchant", "AMAZON PAY INDIA PVT LTD"),
    ("Date &amp; Time", "13-Nov-2026 21:14:52"), ("Transaction Type", "E-commerce"),
    ("Reference No", "631788204519"),
])}</table><br>
If you have not done this transaction, please call our 24x7 Customer Care immediately or SMS
BLOCK 5821 to 9215676766 to block your card. You can also convert this transaction into easy EMIs
from the iMobile app.
</td></tr>""",
        ),
        tests="Long alert whose footer says cards not used for a year may be closed",
        expect={("icici", "5821"): ("review", None)},
    ),
    dict(
        id="L3",
        subject="Welcome to the SBI Card family!",
        html=_wrap(
            "SBI Card", "Welcome", "Your SimplyCLICK SBI Card is on its way",
            f"""<tr><td style="padding:16px">Dear Shubham Kadam,<br><br>
Congratulations and welcome to the SBI Card family! We are delighted to have you with us. Your
SimplyCLICK SBI Card ending 3907 has been dispatched to your registered mailing address and should
reach you within 7 working days.<br><br>
<b>Getting started</b><br>
1. Activate your card using the SBI Card app or by calling 1860 180 1290.<br>
2. Set your PIN through the app or at any SBI ATM.<br>
3. Enable online and international transactions from the Manage Card section.<br><br>
<b>Benefits on your SimplyCLICK SBI Card</b><br>
&bull; Amazon.in e-gift voucher worth Rs. 500 on payment of the joining fee<br>
&bull; 10X Reward Points on online spends with exclusive partners<br>
&bull; 5X Reward Points on all other online spends<br>
&bull; Annual fee of Rs. 499 + GST reversed on annual spends of Rs. 1,00,000<br><br>
<table width="100%">{_rows([
    ("Joining Fee", "Rs. 499 + GST"), ("Annual Fee (from 2nd year)", "Rs. 499 + GST"),
    ("Annual fee waiver", "On spends of Rs. 1,00,000 in the previous year"),
    ("Finance charges", "Up to 3.75% per month"),
])}</table>
</td></tr>""",
            _cross_sell("<b>Looking for more?</b> Explore SBI Card ELITE and SBI Card PRIME for premium "
                        "travel and lifestyle benefits.", "Explore Cards"),
        ),
        tests="Welcome email listing fees and promoting two other cards",
        expect={("sbi", "3907"): ("owned", "SimplyCLICK")},
    ),
    dict(
        id="L4",
        subject="Important information about your Axis Bank Credit Card",
        html=_wrap(
            "Axis Bank", "Annual Fee Intimation", "Annual fee for your Magnus Credit Card",
            f"""<tr><td style="padding:16px">Dear Shubham Kadam,<br><br>
We would like to inform you that the annual fee of Rs. 12,500 + applicable GST for your Axis Bank
Magnus Credit Card ending 6158 will be charged in your next statement. This fee is applicable as
the card completes another year of membership on 01-Dec-2026.<br><br>
The annual fee will be waived if your total spends in the preceding 12 months were Rs. 25,00,000
or more. Your spends in the last 12 months: Rs. 0.<br><br>
<table width="100%">{_rows([
    ("Card", "Magnus Credit Card XX6158"), ("Annual Fee", "Rs. 12,500 + GST"),
    ("Waiver criteria", "Annual spends of Rs. 25 lakh"), ("Fee charge date", "01-Dec-2026"),
])}</table>
</td></tr>""",
        ),
        tests="Generic subject; annual fee notice for an unused card, with RBI closure wording in the footer",
        expect={("axis", "6158"): ("owned", "Magnus")},
    ),
    dict(
        id="L5",
        subject="Festive Bonanza: Offers handpicked for you, Shubham",
        html=_wrap(
            "HDFC Bank", "Festive Treats", "Up to 10% instant discount this festive season",
            """<tr><td style="padding:16px">Dear Shubham,<br><br>
This festive season, make every purchase more rewarding with your Regalia Gold Credit Card ending
7410. Enjoy up to 10% instant discount on electronics, 5X reward points on dining and a host of
offers across travel and shopping.<br><br>
<table width="100%"><tr>
<td><b>Electronics</b><br>10% off up to Rs. 5,000 on Samsung, Apple and more</td>
<td><b>Travel</b><br>Flat Rs. 2,000 off on flights via SmartBuy</td>
<td><b>Dining</b><br>15% off at 2,500+ restaurants</td></tr></table><br>
Offer valid from 01-Nov-2026 to 31-Dec-2026 on HDFC Bank Credit Cards. Not applicable on
Corporate Cards.
</td></tr>""",
            _cross_sell("<b>Diners Club Black Metal Credit Card</b> - Unlimited lounge access worldwide. "
                        "Lifetime free for select customers.", "Apply Now"),
        ),
        tests="Long personalised marketing that names a card the user holds: must change nothing",
        expect=None,
    ),
    dict(
        id="L6",
        subject="Your American Express statement is ready",
        html=_wrap(
            "American Express", "Statement Notification", "Your November statement is now available",
            f"""<tr><td style="padding:16px">Dear Shubham Kadam,<br><br>
Your American Express Membership Rewards Credit Card statement is now available online.<br><br>
<table width="100%">{_rows([
    ("Card Member", "SHUBHAM KADAM"), ("Account Ending", "51004"),
    ("Statement Closing Date", "04-Nov-2026"), ("Closing Balance", "Rs. 18,775.00"),
    ("Minimum Payment Due", "Rs. 939.00"), ("Payment Due Date", "24-Nov-2026"),
    ("Membership Rewards Points Balance", "23,410"),
])}</table><br>
<b>Membership Rewards points summary:</b> Opening balance 21,980; earned 1,430; redeemed 0.
Points never expire as long as your Card account is active and in good standing.
</td></tr>""",
            _cross_sell("<b>Upgrade to the American Express Platinum Travel Credit Card</b> and earn "
                        "travel vouchers worth Rs. 11,800 every year.", "Upgrade Now"),
        ),
        tests="Amex 5-digit account ending in a table, plus an upgrade banner for another card",
        expect={("amex", "1004"): ("owned", "Membership Rewards")},
    ),
    dict(
        id="L7",
        subject="Confirmation of closure of your Kotak Credit Card",
        html=_wrap(
            "Kotak Mahindra Bank", "Card Closure Confirmation", "Your credit card account has been closed",
            f"""<tr><td style="padding:16px">Dear Shubham Kadam,<br><br>
As requested by you on 02-Nov-2026 (Service Request No. SR2611020045), we confirm that your
Kotak League Platinum Credit Card ending 2297 has been closed with effect from 10-Nov-2026. There
are no outstanding dues on the card account and a No Dues Certificate is attached for your
records.<br><br>
<table width="100%">{_rows([
    ("Card", "League Platinum XX2297"), ("Closure request date", "02-Nov-2026"),
    ("Closure date", "10-Nov-2026"), ("Outstanding", "Rs. 0.00"),
    ("Reward points forfeited", "1,120"),
])}</table><br>
We are sorry to see you go and value your feedback. Please take a 2-minute survey to tell us how we
can serve you better.
</td></tr>""",
            _cross_sell("<b>Come back anytime.</b> Kotak White Credit Card - premium lifestyle rewards with "
                        "a welcome bonus of 10,000 points.", "Know More"),
        ),
        tests="Formal closure letter with a no-dues certificate and a win-back banner",
        expect={("kotak", "2297"): ("history", "League Platinum")},
    ),
    dict(
        id="L8",
        subject="Payment Reminder: IDFC FIRST Bank Credit Card",
        html=_wrap(
            "IDFC FIRST Bank", "Payment Reminder", "Your payment of Rs. 6,342 is due on 18-Nov-2026",
            f"""<tr><td style="padding:16px">Dear Shubham Kadam,<br><br>
This is a gentle reminder that the payment for your FIRST Wealth Credit Card ending 8034 is due on
18-Nov-2026. Please pay at least the Minimum Amount Due to avoid late payment charges and impact on
your credit score.<br><br>
<table width="100%">{_rows([
    ("Card", "FIRST Wealth Credit Card XX8034"), ("Total Amount Due", "Rs. 6,342.00"),
    ("Minimum Amount Due", "Rs. 317.00"), ("Due Date", "18-Nov-2026"),
    ("Auto-debit account", "Savings A/c XX1122"),
])}</table><br>
<b>Late payment charges:</b> 15% of the Total Amount Due (minimum Rs. 100, maximum Rs. 1,300).
If you have already made the payment, please ignore this reminder. We regret any inconvenience.
</td></tr>""",
        ),
        tests="Payment reminder with \"regret\" in it, a late-fee table and an auto-debit account number",
        expect={("idfc", "8034"): ("review", "Wealth")},
    ),
    dict(
        id="L9",
        subject="ICICI Bank Sapphiro Credit Card Statement - Nov 2026",
        html=_wrap(
            "ICICI Bank", "Credit Card Statement", "Statement for card ending 4730",
            f"""<tr><td style="padding:16px">Dear SHUBHAM KADAM,<br><br>
Your ICICI Bank Sapphiro Credit Card statement for 16-Oct-2026 to 15-Nov-2026 is attached.<br><br>
<b>Primary Card: XXXX XXXX XXXX 4730 (SHUBHAM KADAM)</b>
<table width="100%">{_rows([
    ("02-Nov-2026 SWIGGY BANGALORE", "Rs. 642.00"), ("05-Nov-2026 IRCTC", "Rs. 2,310.00"),
    ("09-Nov-2026 SHELL PETROL PUMP", "Rs. 3,000.00"),
])}</table><br>
<b>Add-on Card: XXXX XXXX XXXX 9012 (PRIYA KADAM)</b>
<table width="100%">{_rows([
    ("04-Nov-2026 MYNTRA", "Rs. 4,199.00"), ("11-Nov-2026 BIGBASKET", "Rs. 1,860.00"),
])}</table><br>
<table width="100%">{_rows([
    ("Total Amount Due", "Rs. 12,011.00"), ("Minimum Amount Due", "Rs. 601.00"),
    ("Payment Due Date", "03-Dec-2026"),
])}</table>
</td></tr>""",
        ),
        tests="Statement with primary and add-on card sections and a transaction list",
        expect={("icici", "4730"): ("owned", "Sapphiro"), ("icici", "9012"): ("review", None)},
    ),
]
