"""Write workload. Runs INSIDE the lab network (python -m pgdrill.workload).

Every writer inserts (run_id, writer_id, seq) rows in autocommit mode and logs one JSON
line per event to --out:

  {"ev": "connect", "t": ..., "w": 1, "srv": "10.89.77.11", "rec": false}
  {"ev": "ack",     "t": ..., "w": 1, "seq": 42, "srv": "10.89.77.11"}
  {"ev": "err",     "t": ..., "w": 1, "seq": 43, "phase": "commit", "msg": "..."}

An "ack" means the server confirmed the commit. A commit-phase "err" carries the SQLSTATE
when the server answered; verify.py decides whether it was a clean rejection (e.g. 25006
read-only) or in doubt (connection lost, backend terminated: may or may not have committed). The drill compares acks with the ledger
table on the surviving primary afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time

import psycopg

TIMEOUTS = (
    " connect_timeout=2 tcp_user_timeout=3000 keepalives=1"
    " keepalives_idle=1 keepalives_interval=1 keepalives_count=2"
)


class EventLog:
    def __init__(self, path: str):
        self._f = open(path, "w", buffering=1 << 16)
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()

    def emit(self, **ev) -> None:
        line = json.dumps(ev, separators=(",", ":")) + "\n"
        with self._lock:
            self._f.write(line)
            now = time.monotonic()
            if now - self._last_flush > 0.5:
                self._f.flush()
                self._last_flush = now

    def close(self) -> None:
        with self._lock:
            self._f.flush()
            os.fsync(self._f.fileno())
            self._f.close()


def writer(wid: int, conninfo: str, run_id: str, interval: float, stop: threading.Event, log: EventLog) -> None:
    seq = 0
    conn = None
    srv = None
    while not stop.is_set():
        if conn is None:
            try:
                conn = psycopg.connect(conninfo + TIMEOUTS + f" application_name=pgdrill-w{wid}", autocommit=True)
                srv, rec = conn.execute("select host(inet_server_addr()), pg_is_in_recovery()").fetchone()
                log.emit(ev="connect", t=time.time(), w=wid, srv=srv, rec=rec)
            except Exception as exc:  # noqa: BLE001 - any failure means "retry"
                log.emit(ev="err", t=time.time(), w=wid, phase="connect", msg=str(exc).splitlines()[0][:200])
                conn = None
                stop.wait(0.2)
                continue
        seq += 1
        try:
            conn.execute(
                "insert into ledger (run_id, writer_id, seq, client_ts) values (%s, %s, %s, %s)",
                (run_id, wid, seq, time.time()),
            )
            log.emit(ev="ack", t=time.time(), w=wid, seq=seq, srv=srv)
        except Exception as exc:  # noqa: BLE001
            log.emit(ev="err", t=time.time(), w=wid, seq=seq, phase="commit", srv=srv,
                     sqlstate=getattr(exc, "sqlstate", None),
                     msg=str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
            continue
        if interval:
            stop.wait(interval)
    if conn is not None:
        conn.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conninfo", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--writers", type=int, default=4)
    ap.add_argument("--interval", type=float, default=0.005, help="pause between commits per writer (s)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--stop-file", required=True)
    ap.add_argument("--max-duration", type=float, default=600)
    args = ap.parse_args(argv)

    log = EventLog(args.out)
    stop = threading.Event()
    log.emit(ev="start", t=time.time(), writers=args.writers)
    threads = [
        threading.Thread(target=writer, args=(i, args.conninfo, args.run_id, args.interval, stop, log), daemon=True)
        for i in range(1, args.writers + 1)
    ]
    for th in threads:
        th.start()
    t0 = time.monotonic()
    while not os.path.exists(args.stop_file) and time.monotonic() - t0 < args.max_duration:
        time.sleep(0.2)
    stop.set()
    # Writers blocked on a hung COMMIT are daemons: give them a moment, then exit anyway.
    for th in threads:
        th.join(timeout=10)
    log.emit(ev="stop", t=time.time(), hung_writers=sum(th.is_alive() for th in threads))
    log.close()
    os._exit(0)


if __name__ == "__main__":
    main()
