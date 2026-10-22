"""Small helpers shared by the bin/ scripts that run on the host."""
import csv
import io
import math
import subprocess

DB_CONTAINER = "snowhackers-db-1"


def psql_csv(sql):
    """Run a SELECT in the snowhackers Postgres container; return rows as lists of strings."""
    out = subprocess.run(["docker", "exec", DB_CONTAINER, "psql", "-U", "snowuser", "-d", "snow", "-At",
                          "-c", f"COPY ({sql}) TO STDOUT WITH CSV"], check=True, capture_output=True, text=True).stdout
    return list(csv.reader(io.StringIO(out)))


def km(a, b):
    """Great-circle distance in km between (lat, lon) pairs."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))
