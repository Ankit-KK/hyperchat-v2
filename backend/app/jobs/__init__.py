"""Durable PostgreSQL-backed background jobs (Phase 8).

queue    enqueue (inside the caller's transaction), claim (FOR UPDATE SKIP LOCKED + lease), finish/retry
retry    the backoff policy and the job outcome types handlers return
runner   the worker loop (python -m app.worker): N slots, lease fencing, SIGTERM handling
"""
