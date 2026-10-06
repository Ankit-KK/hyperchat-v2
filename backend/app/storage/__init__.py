"""Object storage for user media (Cloudflare R2, S3 API).

  r2.py      ObjectStore protocol, the R2 implementation (boto3) and key rules
  memory.py  in-memory ObjectStore for tests

Only the server chooses object keys (see r2.WRITABLE_KEY); clients never send one. Every environment
has its own bucket, and its R2 token is scoped to that bucket only.
"""
