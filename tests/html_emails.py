"""HTML test emails that look like real bank mail: nested tables, values in separate
cells, numbers split across tags, entities, hidden preview text, stub plain-text
parts, and account numbers that look like card numbers.

`plain` (optional) is the email's plain-text part. Many banks send a useless stub
there and put everything in the HTML. `expect` is (bank, last4 or product) ->
(group, product), or None when the email must not create or change a card.
"""

_STYLE = "<style>td{font-family:Arial;font-size:12px} .h{display:none}</style><!-- tracking: abc123 -->"

HTML = [
    dict(
        id="H1",
        subject="Your HDFC Bank Credit Card Statement - November 2026",
        html=f"""<html><head>{_STYLE}</head><body>
<table width="100%"><tr><td>
  <table><tr><td><b>HDFC Bank</b></td><td align="right">Statement</td></tr></table>
  <table border="0" cellpadding="4">
    <tr><td>Name</td><td>SHUBHAM KADAM</td></tr>
    <tr><td>Card Number</td><td>XXXX XXXX XXXX 2580</td></tr>
    <tr><td>Card Type</td><td>Regalia Gold</td></tr>
    <tr><td>Statement Date</td><td>05/11/2026</td></tr>
    <tr><td>Total Dues</td><td>&#8377; 24,310.00</td></tr>
  </table>
</td></tr></table></body></html>""",
        tests="Label and value in separate table cells (\"Card Type | Regalia Gold\")",
        expect={("hdfc", "2580"): ("owned", "Regalia Gold")},
    ),
    dict(
        id="H2",
        subject="ICICI Bank Credit Card statement",
        html=f"""<html><head>{_STYLE}</head><body><p>Dear Shubham Kadam,</p>
<p>Statement for your ICICI Bank Coral Credit Card
<span style="color:#999">XXXX XXXX XXXX </span><span style="font-weight:bold">36</span><span>91</span>
is ready.</p></body></html>""",
        tests="Card number split across <span> tags",
        expect={("icici", "3691"): ("owned", "Coral")},
    ),
    dict(
        id="H3",
        subject="Statement ready",
        html=f"""<html><head>{_STYLE}</head><body>
<div class="h">Your monthly summary is inside &zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;</div>
<p>Dear&nbsp;Shubham&nbsp;Kadam,</p>
<p>Your&nbsp;Axis&nbsp;Bank&nbsp;Atlas&nbsp;Credit&nbsp;Card&nbsp;statement for card ending&nbsp;4826 is ready.</p>
</body></html>""",
        tests="&nbsp; and zero-width characters everywhere, hidden preview text",
        expect={("axis", "4826"): ("owned", "Atlas")},
    ),
    dict(
        id="H4",
        subject="Your SBI Card statement",
        plain="This email requires an HTML-capable email client. Please view it in a browser.",
        html=f"""<html><head>{_STYLE}</head><body><p>Dear Shubham Kadam,</p>
<p>Your SBI Card PRIME statement for card ending 6204 is now available.</p></body></html>""",
        tests="Plain-text part is a stub; the real content is only in the HTML",
        expect={("sbi", "6204"): ("owned", "PRIME")},
    ),
    dict(
        id="H5",
        subject="Discover a card made for you",
        html=f"""<html><head>{_STYLE}</head><body>
<h1>Find your perfect card</h1>
<table><tr>
<td><h3>Infinia Credit Card</h3><p>5X rewards on travel</p><a href="#">Apply Now</a></td>
<td><h3>Millennia Credit Card</h3><p>5% cashback online</p><a href="#">Apply Now</a></td>
<td><h3>Swiggy HDFC Bank Credit Card</h3><p>10% cashback on Swiggy</p><a href="#">Apply Now</a></td>
</tr></table>
<p style="font-size:9px">HDFC Bank. T&amp;C apply. To unsubscribe click here.</p></body></html>""",
        tests="Marketing grid of three cards with Apply Now buttons: must create nothing",
        expect=None,
    ),
    dict(
        id="H6",
        subject="Kotak Credit Card statement for October 2026",
        html=f"""<html><head>{_STYLE}</head><body><p>Dear Shubham Kadam,</p>
<p>Card: Kotak White Credit Card &bull;&bull;&bull;&bull; 1593</p>
<table><tr><td>Amount due</td><td>Rs 9,880</td></tr>
<tr><td>Auto-debit from</td><td>Savings A/c XX7788</td></tr></table>
<p>Your linked account number XXXXXX4455 will be debited on 20 Nov.</p></body></html>""",
        tests="Statement that also shows masked bank-account numbers: only the card should count",
        expect={("kotak", "1593"): ("owned", "White")},
    ),
    dict(
        id="H7",
        subject="Transaction alert on your Credit Card",
        html=f"""<html><head>{_STYLE}</head><body><table>
<tr><td>Bank</td><td>IndusInd Bank</td></tr>
<tr><td>Card</td><td>XX9630</td></tr>
<tr><td>Merchant</td><td>AMAZON</td></tr>
<tr><td>Amount</td><td>INR 1,999.00</td></tr>
<tr><td>Date</td><td>13-11-2026 18:42</td></tr></table></body></html>""",
        tests="Alert laid out as a key/value table",
        expect={("indusind", "9630"): ("review", None)},  # a single alert: confirm
    ),
    dict(
        id="H8",
        subject="Welcome to Axis Bank Magnus® Credit Card",
        html=f"""<html><head>{_STYLE}</head><body>
<h1>Welcome to Axis Bank Magnus&reg; Credit Card</h1>
<p>Dear Shubham Kadam, your card ending 7531 has been dispatched and will reach you in 5 days.</p>
</body></html>""",
        tests="Product followed by ® symbol",
        expect={("axis", "7531"): ("owned", "Magnus")},
    ),
    dict(
        id="H9",
        subject="Your American Express Card statement",
        html=f"""<html><head>{_STYLE}</head><body><table>
<tr><td>Card Member</td><td>SHUBHAM KADAM</td></tr>
<tr><td>Card Member Since</td><td>2018</td></tr>
<tr><td>Account Ending</td><td>-</td><td>21009</td></tr>
<tr><td>Card</td><td>American Express Platinum Travel Credit Card</td></tr>
</table></body></html>""",
        tests="Amex 5-digit number in its own cell, with a dash cell in between",
        expect={("amex", "1009"): ("owned", "Platinum Travel")},
    ),
    dict(
        id="H10",
        subject="Payment received",
        html=f"""<html><head>{_STYLE}</head><body>
<p>Dear Shubham Kadam,</p><p>We have received your payment of <b>Rs&nbsp;5,000</b> towards your
<b>HDFC Bank</b> Credit Card ending <b>2580</b>.</p></body></html>""",
        tests="Bold tags around the number and the bank name",
        expect={("hdfc", "2580"): ("owned", "Regalia Gold")},  # joins H1's card, no new card
    ),
]
