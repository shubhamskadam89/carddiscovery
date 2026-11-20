"""Messy, realistic-looking test emails: HTML layouts, junk preview text, odd number
formats, look-alike debit mail. Used both to send real test mail and in tests.

`expect` is what *should* happen: (bank, last4 or product) -> (group, product),
or None when the email must not create or change any card.
"""

MESSY = [
    dict(
        id="M1",
        subject="Statement for HDFC Bank Credit Card XX2468 - October 2026",
        html="""<html><body><table width="600"><tr><td style="font:12px Arial">
<img src="https://example.com/logo.png" alt="HDFC Bank"></td></tr>
<tr><td><h3>Statement Summary</h3><table border="1"><tr><th>Statement Date</th><th>Total Amount Due</th>
<th>Minimum Amount Due</th><th>Payment Due Date</th></tr>
<tr><td>05/11/2026</td><td>Rs. 18,432.00</td><td>Rs. 922.00</td><td>25/11/2026</td></tr></table>
<p>Dear Shubham Kadam,</p><p>Please find attached your e-statement.</p></td></tr></table></body></html>""",
        tests="Card number only in the subject, HTML table body, product never named",
        expect={("hdfc", "2468"): ("owned", None)},
    ),
    dict(
        id="M2",
        subject="Your e-statement is here",
        html="""<html><body><div style="display:none">View in browser | Add us to your address book |
Unsubscribe | Privacy Policy | Contact us | Download our app | Follow us on social media</div>
<p>View this email in your browser</p><p>Dear Cardmember,</p>
<p>Your ICICI Bank Sapphiro Credit Card statement for the period 06-Oct-2026 to 05-Nov-2026 is now available.</p>
<p>Card No: XXXX-XXXX-XXXX-1357</p><p>Total Amount Due: INR 7,210.50</p></body></html>""",
        tests="Junk preview text (\"View in browser...\"), number and product only deep in the body",
        expect={("icici", "1357"): ("owned", "Sapphiro")},
    ),
    dict(
        id="M3",
        subject="Your Axis Bank Credit Card Statement for October",
        body="Dear Shubham Kadam,\nCard Number: 5XXX XXXX XXXX 9753\nCard Type: Axis Bank MY Zone Credit Card\n"
             "Total Payment Due: Rs 3,450.00\nPayment Due Date: 20 Nov 2026",
        tests="Card number with a leading digit and spaces, product on its own line",
        expect={("axis", "9753"): ("owned", "MY Zone")},
    ),
    dict(
        id="M4",
        subject="Alert : Update on your HDFC Bank Credit Card",
        body="Dear Customer, Rs.1,250.00 was spent on your credit card ending 8642 at SWIGGY on 12-11-2026:20:15:33. "
             "Not you? Call 18002586161 to block your card.",
        tests="Alert with an \"Update on\" subject, phone number and timestamp digits",
        expect={("hdfc", "8642"): ("review", None)},  # one alert alone could be an add-on: confirm
    ),
    dict(
        id="M5",
        subject="Your American Express Card statement is ready",
        body="Dear Shubham Kadam, your statement for the Card Member account ending 71005 is ready to view online. "
             "Membership Rewards Credit Card. Closing balance: Rs 12,000.",
        tests="Amex shows the last 5 digits, not 4",
        expect={("amex", "1005"): ("owned", "Membership Rewards")},
    ),
    dict(
        id="M6",
        subject="Welcome aboard!",
        body="Dear Shubham Kadam, your SBI Card SimplySAVE **** **** **** 1029 has been activated successfully. "
             "Enjoy 10X reward points on dining.",
        tests="Generic subject, product after the brand, number masked with stars and spaces",
        expect={("sbi", "1029"): ("owned", "SimplySAVE")},
    ),
    dict(
        id="M7",
        subject="Exclusive for you: 10X reward points on your Regalia Gold Credit Card XX2468",
        body="This festive season, earn 10X reward points on your HDFC Bank Regalia Gold Credit Card ending 2468. "
             "Offer valid till 31 Dec 2026. T&C apply.",
        tests="Marketing that names a card the user holds, with its number: no new card, but it names "
              "••2468 (from M1, which never says the product) as the Regalia Gold",
        expect={("hdfc", "2468"): ("owned", "Regalia Gold")},
    ),
    dict(
        id="M8",
        subject="Your IDFC FIRST Bank credit card statement",
        body="Dear Shubham Kadam, your FIRST Millennia Credit Card ending 3141 statement for October is ready.",
        tests="Product name shared with another bank (HDFC also has Millennia)",
        expect={("idfc", "3141"): ("owned", "Millennia")},
    ),
    dict(
        id="M9",
        subject="Payment received - Kotak Credit Card",
        body="Dear Shubham Kadam, payment of Rs 2,500.00 received towards your Kotak Credit Card xx6789. "
             "Ref No 123456789012. Available limit: Rs 1,47,500.",
        tests="Lower-case mask (xx6789) next to amounts and a 12-digit reference number",
        expect={("kotak", "6789"): ("review", None)},  # payment alone is not proof of ownership
    ),
    dict(
        id="M10",
        subject="Your HDFC Bank Debit Card ending 5544 was used",
        body="Dear Customer, Rs 300.00 was debited from your savings account using your HDFC Bank Debit Card "
             "ending 5544 at BigBasket. Want more rewards? Apply for an HDFC Bank Credit Card today!",
        tests="Debit-card alert with a credit-card ad in the footer: must not create a card",
        expect=None,
    ),
    dict(
        id="M11",
        subject="ICICI Bank Credit Card XX8080 का स्टेटमेंट",
        body="प्रिय Shubham Kadam, आपके ICICI Bank Credit Card XX8080 का अक्टूबर का statement तैयार है। "
             "कुल देय राशि: Rs 4,120",
        tests="Hindi and English mixed",
        expect={("icici", "8080"): ("owned", None)},
    ),
    dict(
        id="M12",
        subject="Your HDFC Bank reward points are expiring soon",
        body="Dear Shubham Kadam, 2,340 reward points will expire on 31-Dec-2026. Redeem now on SmartBuy.",
        tests="Points notice with no card number while the bank has several cards",
        expect=None,  # shouldn't create a mystery card; nothing to tie it to
    ),
    dict(
        id="M13",
        subject="Congratulations! Your card has been converted",
        body="Dear Shubham Kadam, your existing Axis Bank MY Zone Credit Card ending 9753 has been converted to "
             "Axis Bank Atlas Credit Card. Your new card ending 8531 has been dispatched.",
        tests="Upgrade worded as \"converted\", old and new numbers in one email",
        expect={("axis", "9753"): ("history", "MY Zone"), ("axis", "8531"): ("owned", "Atlas")},
    ),
    dict(
        id="M14",
        subject="Important: Your Credit Card statement",
        body="[date:2019-07-05] Dear Shubham Kadam, your Yes Bank credit card ending 4321 statement is attached. "
             "Yes, you can pay online.",
        tests="Bank written in mixed case (\"Yes Bank\"), old date",
        expect={("yes", "4321"): ("owned", None)},
    ),
]
