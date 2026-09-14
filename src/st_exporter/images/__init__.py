"""Pricebook image upload — bytes to TrueQuote, never through the Sheet.

The Export Store's ``image_refs`` column carries identifiers only
(``CONTRACT-pricebook-tabs.md``). The bytes those identifiers name travel over
TrueQuote's authenticated ``POST {outbox}/pricebook-image`` endpoint, downloaded
on the contractor's own runner and pushed with a Machine Token of scope
``image_upload`` — the same auth pattern as the CRM Outbox drain, a *different*
token.
"""
