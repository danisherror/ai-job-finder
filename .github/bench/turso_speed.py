"""Temporary: measures Turso speed from GitHub's servers (same region as the database)."""
import os
import time

from src import broadcast, store

c = store.connect()
t = time.time(); c.execute("SELECT 1").fetchone(); print(f"tiny query: {time.time() - t:.3f}s")
t = time.time()
n = len(c.execute("""SELECT job_key, title, company, location_raw, cities, posted_at, first_seen FROM jobs
                     WHERE lifecycle='open' AND india_eligible='yes' AND backfill=0""").fetchall())
print(f"light query, direct: {n} rows {time.time() - t:.1f}s")
t = time.time()
try:
    i, g = broadcast.pick_lists(c)
    print(f"/latest query (35 MB), direct: {len(i)}+{len(g)} jobs {time.time() - t:.1f}s")
except Exception as e:
    print(f"/latest query, direct: FAILED after {time.time() - t:.1f}s: {str(e)[:120]}")

os.environ["TURSO_REPLICA"] = "replica/jobs.db"
t = time.time(); r = store.connect(); print(f"local copy, first sync: {time.time() - t:.1f}s")
t = time.time(); i, g = broadcast.pick_lists(r); print(f"/latest query, local copy: {time.time() - t:.1f}s")
r.execute("CREATE TABLE IF NOT EXISTS _probe (k INTEGER PRIMARY KEY, v TEXT)"); r.commit()
t = time.time()
with r:
    for k in range(500):
        r.execute("INSERT OR REPLACE INTO _probe VALUES (?, ?)", (k, "x"))
print(f"500 writes, local copy: {time.time() - t:.1f}s")
t = time.time()
with c:
    for k in range(100):
        c.execute("INSERT OR REPLACE INTO _probe VALUES (?, ?)", (k, "y"))
print(f"100 writes, direct: {time.time() - t:.1f}s")
r.execute("DROP TABLE _probe"); r.commit()
