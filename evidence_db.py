"""
evidence_db.py: one way to open the evidence database (Supabase Postgres) for every script.

The session pooler allows 15 clients at once (pool_size). On 4 Nov 2026 twelve Propose values jobs held
twelve, and the narrative evaluation and the generic reader, started next to them, failed at once with
EMAXCONNSESSION. connect() waits for a free place instead: it retries "max clients reached" for up to
WAIT_MINUTES, with a growing pause, and fails on any other error straight away.
"""
import os
import time

WAIT_MINUTES = 30


def connect(url=None, connect_timeout=30, wait_minutes=WAIT_MINUTES, sleep=time.sleep):
    import psycopg
    url = (url or os.environ["SUPABASE_DB_URL"]).strip()
    deadline, pause = time.time() + wait_minutes * 60, 15
    while True:
        try:
            # keepalives: a connection whose other end is gone is noticed in about a minute. Without them a writer
            # waited for hours on a dead connection (11 Nov 2026: the night's reader hung at its last step after the
            # database dropped every connection, and was killed with what it had read unwritten).
            return psycopg.connect(url, connect_timeout=connect_timeout, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3)
        except psycopg.OperationalError as e:
            full = "max clients" in str(e) or "EMAXCONNSESSION" in str(e) or "too many clients" in str(e)
            if not full or time.time() + pause > deadline:
                raise
            print(f"  the evidence database is full (15 clients); waiting {pause} s for a free place", flush=True)
            sleep(pause)
            pause = min(pause * 2, 120)
