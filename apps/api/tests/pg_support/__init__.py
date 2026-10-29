"""Reusable PostgreSQL test support for the API suite.

``auth_support`` is the single auth fixture: one app per test, real PostgreSQL,
never a skip. Later lanes extend this package rather than building their own app.
"""
