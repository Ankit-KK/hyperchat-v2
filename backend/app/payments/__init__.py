"""Payments (Phase 7): Razorpay checkout, payment status, checkout confirmation and webhooks.

razorpay      REST client (Orders/Payments API) and signature checks
accounts      the two configured Razorpay accounts and their cached credential health
rules         pure checkout/completion rules (amounts, names, attachments, auto-approval)
repository    SQL (donations, order_lookup, payment health, webhook events)
service       the use-cases, including the one completion path shared by webhook, confirm and polling
"""
