"""One-shot database probes. Run INSIDE the lab network (python -m pgdrill.probe ...)."""

from __future__ import annotations

import argparse
import json
import time

DDL = """
create table if not exists ledger (
    run_id    text   not null,
    writer_id int    not null,
    seq       bigint not null,
    client_ts double precision not null,
    primary key (run_id, writer_id, seq)
)
"""


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("now")
    p = sub.add_parser("init")
    p.add_argument("--conninfo", required=True)
    p = sub.add_parser("ledger")
    p.add_argument("--conninfo", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "now":
        print(repr(time.time()))
        return

    import psycopg

    with psycopg.connect(args.conninfo + " connect_timeout=5", autocommit=True) as conn:
        if args.cmd == "init":
            conn.execute(DDL)
            print("ok")
        elif args.cmd == "ledger":
            rows = conn.execute(
                "select writer_id, seq from ledger where run_id = %s", (args.run_id,)
            ).fetchall()
            # Current timeline of a primary = first 8 hex digits of its current WAL file name.
            tl = conn.execute(
                "select ('x' || substr(pg_walfile_name(pg_current_wal_lsn()), 1, 8))::bit(32)::int"
            ).fetchone()[0]
            srv = conn.execute("select host(inet_server_addr())").fetchone()[0]
            with open(args.out, "w") as f:
                json.dump({"server": srv, "timeline": tl, "rows": rows}, f)
            print(len(rows))


if __name__ == "__main__":
    main()
