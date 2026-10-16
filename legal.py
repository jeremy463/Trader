"""Legal page rendered from YOUR configured company details (everything HTML-escaped). The wording is a conventional starting point:
`legal_reviewed` must be set only after a qualified lawyer has reviewed it for your jurisdiction."""
from __future__ import annotations
from html import escape as e
from .webconfig import WebSettings


def legal_html(s: WebSettings) -> bytes:
    brand, co, jur, mail, upd = e(s.brand_name), e(s.company_name or s.brand_name), e(s.legal_jurisdiction or "the jurisdiction in which the operator is established"), e(s.support_email), e(s.legal_updated)
    contact = f'<a href="mailto:{mail}">{mail}</a>' if s.support_email else "the support contact shown in the app"
    refund = f"<h3>Refunds</h3><p>{e(s.refund_policy)}</p>" if s.refund_policy.strip() else ""
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Terms, risk disclosure and privacy - {brand}</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="/static/app.css"></head><body><main class="legal"><div class="card"><h2>Risk disclosure, terms and privacy</h2>
<p><b>Operator:</b> {co}. <b>Governing law:</b> {jur}. <b>Contact:</b> {contact}. {f'<b>Last updated:</b> {upd}.' if s.legal_updated else ''}</p>
<h3>Risk disclosure</h3><p>Trading foreign exchange, futures and other leveraged products carries a high risk of loss and is not suitable for everyone. You can lose more than you deposit with some instruments. Past performance, back-tests and historical statistics do not guarantee future results. Indicators, patterns and grades are analytical tools; they do not predict markets and a high grade is not a probability of profit.</p>
<h3>What this service is</h3><p>{brand} provides automated technical analysis, market-timing information and an economic calendar for information and education. It is not investment advice, a personal recommendation, or an offer to buy or sell any instrument. It does not place trades on your behalf for subscribers and does not hold your money. You are solely responsible for your own decisions.</p>
<h3>Market data and news</h3><p>Prices, calendars and headlines come from third-party providers and may be delayed, incomplete or wrong. Headline tone is produced by a keyword heuristic and has low reliability. Do not rely on this service as your only source.</p>
<h3>Accounts and payments</h3><p>Paid plans are fixed-length passes processed by Paystack; {brand} never sees or stores your card details. Keep your password private and use a unique one. Accounts that abuse the service may be suspended.</p>{refund}
<h3>Privacy</h3><p>We store your email address, a salted password hash, your plan and usage, payment references, sizing preferences, watchlist, alerts and any journal entries you create. Payments are handled by Paystack; transactional email is sent through our email provider; market data is fetched from our data providers. We do not sell personal data. You can download all your data and delete your account at any time from the Account page; payment records are retained for accounting and legal obligations. Contact {contact} for any privacy request.</p>
<h3>No warranty</h3><p>The service is provided "as is", without warranties of any kind, to the fullest extent permitted by law.</p>
<p><a href="/">Back to the app</a></p></div></main></body></html>"""
    return body.encode()
