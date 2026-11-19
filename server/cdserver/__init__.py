"""Server-side mini version of card discovery (docs/architecture.md §13).

Same shape as production, on one machine:
  api        FastAPI: Gmail connect, status, cards, decisions (login is a separate service)
  worker     claims jobs from Postgres (FOR UPDATE SKIP LOCKED) and talks to Gmail
  scheduler  enqueues daily syncs, reaps stuck jobs
Stand-ins: a local master key instead of AWS KMS; /api/dev/login instead of the login service.
"""
