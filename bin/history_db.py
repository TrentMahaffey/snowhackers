"""Per-image snow readings (model predictions + hand/AI labels) in Postgres.

Table cam_prediction: one row per image (prefix, name), `rec` holds the record exactly as the
old cam_predictions_history.json did ({prefix, ts, name, depth_inches, confidence, source, ...}).
A label row is never overwritten by a model row.

Host scripts use this through `docker exec snowhackers-db-1 psql` (no psycopg2 on the host):

    python3 bin/history_db.py export FILE      # all records -> JSON list (newest first)
    python3 bin/history_db.py upsert FILE      # JSON list -> table (labels win over model rows)
    python3 bin/history_db.py replace FILE     # JSON list becomes the whole table (one transaction)
    python3 bin/history_db.py stats
"""
import csv
import io
import json
import subprocess
import sys

DB = ["docker", "exec", "-i", "snowhackers-db-1", "psql", "-U", "snowuser", "-d", "snow",
      "-v", "ON_ERROR_STOP=1", "-q", "-At"]

DDL = """
CREATE TABLE IF NOT EXISTS cam_prediction (
  id         bigserial PRIMARY KEY,
  prefix     text NOT NULL,
  name       text NOT NULL,
  source     text NOT NULL,
  ts         text,                      -- capture time, UTC, 'YYYY-MM-DD HH:MM:SS' (from the filename)
  rec        json NOT NULL,             -- full record; json (not jsonb) keeps it byte-for-byte
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (prefix, name)
);
CREATE INDEX IF NOT EXISTS cam_prediction_prefix_ts_idx ON cam_prediction (prefix, ts DESC);
CREATE INDEX IF NOT EXISTS cam_prediction_ts_idx ON cam_prediction ((coalesce(ts, '')) DESC, id);
"""


def _psql(script, capture=True):
    r = subprocess.run(DB + ["-f", "-"], input=script, text=True, capture_output=capture)
    if r.returncode:
        raise SystemExit(f"psql failed: {r.stderr.strip()}")
    return r.stdout


def load():
    """All records, newest first (same shape/order as the old JSON file)."""
    out = _psql(DDL + "SELECT coalesce(json_agg(rec), '[]') FROM cam_prediction;\n")
    rows = json.loads(out)
    rows.sort(key=lambda r: (r.get("ts") or "", r.get("name") or ""), reverse=True)  # faster than ORDER BY here
    return rows


def _copy_block(rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for i, r in enumerate(rows):
        if not (r.get("prefix") and r.get("name")):
            continue
        w.writerow([i, r["prefix"], r["name"], r.get("source") or "model", r.get("ts") or "",
                    json.dumps(r, separators=(",", ":"))])
    return ("CREATE TEMP TABLE _in (ord int, prefix text, name text, source text, ts text, rec json);\n"
            "COPY _in FROM STDIN WITH (FORMAT csv);\n" + buf.getvalue() + "\\.\n")


_INSERT = """
INSERT INTO cam_prediction (prefix, name, source, ts, rec)
SELECT DISTINCT ON (prefix, name) prefix, name, source, nullif(ts, ''), rec FROM _in ORDER BY prefix, name, ord DESC
ON CONFLICT (prefix, name) DO UPDATE
  SET source = EXCLUDED.source, ts = EXCLUDED.ts, rec = EXCLUDED.rec, updated_at = now()
  WHERE cam_prediction.source <> 'label' OR EXCLUDED.source = 'label';
"""


def upsert(rows):
    """Insert/update records; a model row never replaces a label row. Returns rows written."""
    out = _psql(DDL + "BEGIN;\n" + _copy_block(rows) + _INSERT.rstrip().rstrip(";") + " RETURNING 1;\nCOMMIT;\n")
    return sum(1 for l in out.splitlines() if l.strip() == "1")


def replace(rows):
    """Make `rows` the entire table, atomically."""
    _psql(DDL + "BEGIN;\n" + _copy_block(rows) + "DELETE FROM cam_prediction;\n" + _INSERT + "COMMIT;\n")


def stats():
    return _psql(DDL + "SELECT source, count(*) FROM cam_prediction GROUP BY 1 ORDER BY 1;\n").strip()


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("export", "upsert", "replace", "stats"):
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "stats":
        print(stats())
    elif cmd == "export":
        rows = load()
        with open(sys.argv[2], "w") as f:
            f.write(json.dumps(rows, indent=2) + "\n")
        print(f"exported {len(rows)} rows")
    else:
        rows = json.load(open(sys.argv[2]))
        if cmd == "upsert":
            print(f"upserted {upsert(rows)} of {len(rows)} rows")
        else:
            replace(rows)
            print(f"replaced table with {len(rows)} rows")
