"""Write workload. Runs INSIDE the lab's client network (python -m pgdrill.workload).

Every writer inserts (run_id, writer_id, seq) rows in autocommit mode and logs one JSON
line per event to --out:

  {"ev": "connect",  "t": ..., "w": 1, "srv": "10.89.78.11", "rec": false}
  {"ev": "ack",      "t": ..., "w": 1, "seq": 42, "srv": "10.89.78.11"}
  {"ev": "err",      "t": ..., "w": 1, "seq": 43, "phase": "commit", "sqlstate": null, "msg": "..."}
  {"ev": "stopping", "t": ...}                      # stop requested: end of the measured window
  {"ev": "inflight", "t": ..., "w": 2, "seq": 77}   # writer still blocked in COMMIT at stop
  {"ev": "stop",     "t": ..., "hung_writers": 1}

An "ack" means the server confirmed the commit. A commit-phase "err" carries the SQLSTATE
when the server answered; verify.py decides whether it was a clean rejection (e.g. 25006
read-only) or in doubt (connection lost, backend terminated: may or may not have committed).
"inflight" commits never got an answer and are in doubt too.

The connection string comes from the host (runner.py) and already carries the client
timeouts, so the report can state exactly what the clients were configured with.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import threading
import time

import psycopg

RETRY_AFTER_ERROR_S = 0.05


class EventLog:
    def __init__(self, path: str):
        self._f = open(path, "w", buffering=1 << 16)  # noqa: SIM115 - closed in close()
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


# writer id -> seq of the COMMIT currently in flight (absent when idle)
INFLIGHT: dict[int, int] = {}


def writer(wid: int, conninfo: str, run_id: str, interval: float, stop: threading.Event, log: EventLog) -> None:
    seq = 0
    conn = None
    srv = None
    while not stop.is_set():
        if conn is None:
            try:
                conn = psycopg.connect(conninfo + f" application_name=pgdrill-w{wid}", autocommit=True)
                srv, rec = conn.execute("select host(inet_server_addr()), pg_is_in_recovery()").fetchone()
                log.emit(ev="connect", t=time.time(), w=wid, srv=srv, rec=rec)
            except Exception as exc:  # noqa: BLE001 - any failure means "retry"
                log.emit(ev="err", t=time.time(), w=wid, phase="connect", msg=str(exc).splitlines()[0][:200])
                conn = None
                stop.wait(0.2)
                continue
        seq += 1
        INFLIGHT[wid] = seq
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
            with contextlib.suppress(Exception):
                conn.close()
            conn = None
            stop.wait(RETRY_AFTER_ERROR_S)
            continue
        finally:
            INFLIGHT.pop(wid, None)
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
    ap.add_argument("--join-timeout", type=float, default=10, help="total wait for writers at stop (s)")
    args = ap.parse_args(argv)

    log = EventLog(args.out)
    stop = threading.Event()
    log.emit(ev="start", t=time.time(), writers=args.writers)
    threads = {
        wid: threading.Thread(target=writer, args=(wid, args.conninfo, args.run_id, args.interval, stop, log),
                              daemon=True)
        for wid in range(1, args.writers + 1)
    }
    for th in threads.values():
        th.start()
    t0 = time.monotonic()
    while not os.path.exists(args.stop_file) and time.monotonic() - t0 < args.max_duration:
        time.sleep(0.2)
    log.emit(ev="stopping", t=time.time())
    stop.set()
    # One shared deadline: writers blocked on a hung COMMIT are daemons and get abandoned.
    deadline = time.monotonic() + args.join_timeout
    for th in threads.values():
        th.join(timeout=max(0.0, deadline - time.monotonic()))
    hung = [wid for wid, th in threads.items() if th.is_alive()]
    for wid in hung:
        if wid in INFLIGHT:
            log.emit(ev="inflight", t=time.time(), w=wid, seq=INFLIGHT[wid])
    log.emit(ev="stop", t=time.time(), hung_writers=len(hung))
    log.close()
    os._exit(0)


if __name__ == "__main__":
    main()
