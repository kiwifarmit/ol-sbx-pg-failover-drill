"""Podman lab: 1 etcd, 3 Patroni/PostgreSQL nodes, 1 HAProxy, static IPs on one network."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from string import Template

from .podman import PodmanError, exists, out, podman

REPO_ROOT = Path(__file__).resolve().parents[2]
LAB_DIR = REPO_ROOT / "lab"
SRC_DIR = REPO_ROOT / "src"

NETWORK = "pgdrill"
SUBNET = "10.89.77.0/24"
LABEL = "io.kiwifarm.pgdrill=1"

IMAGE = "localhost/pgdrill-patroni:latest"
ETCD_IMAGE = "quay.io/coreos/etcd:v3.5.17"
HAPROXY_IMAGE = "docker.io/library/haproxy:3.0"

ETCD = ("etcd", "10.89.77.5")
NODES = {"pg1": "10.89.77.11", "pg2": "10.89.77.12", "pg3": "10.89.77.13"}
HAPROXY = ("haproxy", "10.89.77.20")
IP_TO_NODE = {ip: name for name, ip in NODES.items()}


@dataclass(frozen=True)
class Mode:
    name: str
    sync: bool
    strict: bool
    dataguard: str


MODES = {
    "async": Mode("async", False, False, "Maximum Performance"),
    "sync": Mode("sync", True, False, "Maximum Availability"),
    "strict": Mode("strict", True, True, "Maximum Protection"),
}


@dataclass(frozen=True)
class Timers:
    ttl: int = 20
    loop_wait: int = 5
    retry_timeout: int = 5
    failsafe: bool = True


def _bool(v: bool) -> str:
    return "true" if v else "false"


class Lab:
    def __init__(self, workdir: Path):
        self.workdir = workdir.resolve()
        self.cfgdir = self.workdir / "lab"

    # ---------------------------------------------------------------- images
    def build(self, force: bool = False) -> None:
        if not force and exists("image", IMAGE):
            return
        podman("build", "-q", "-t", IMAGE, "-f", str(LAB_DIR / "Containerfile.patroni"), str(LAB_DIR), timeout=1800)
        for img in (ETCD_IMAGE, HAPROXY_IMAGE):
            if not exists("image", img):
                podman("pull", "-q", img, timeout=600)

    # ------------------------------------------------------------- lifecycle
    def _render(self, mode: Mode, timers: Timers) -> None:
        self.cfgdir.mkdir(parents=True, exist_ok=True)
        tmpl = Template((LAB_DIR / "patroni.yml.tmpl").read_text())
        for name, ip in NODES.items():
            (self.cfgdir / f"{name}.yml").write_text(
                tmpl.substitute(
                    name=name, ip=ip, etcd_ip=ETCD[1], subnet=SUBNET,
                    ttl=timers.ttl, loop_wait=timers.loop_wait, retry_timeout=timers.retry_timeout,
                    failsafe=_bool(timers.failsafe), sync=_bool(mode.sync), strict=_bool(mode.strict),
                )
            )
        servers = "\n".join(f"    server {n} {ip}:5432 check port 8008" for n, ip in NODES.items())
        (self.cfgdir / "haproxy.cfg").write_text(
            Template((LAB_DIR / "haproxy.cfg.tmpl").read_text()).substitute(
                primary_servers=servers, replica_servers=servers
            )
        )
        (self.cfgdir / "state.json").write_text(json.dumps({"mode": mode.name, "timers": timers.__dict__}))

    def state(self) -> dict | None:
        f = self.cfgdir / "state.json"
        return json.loads(f.read_text()) if f.exists() else None

    def up(self, mode: Mode, timers: Timers = Timers(), timeout: float = 180) -> float:
        self.down()
        self.build()
        self._render(mode, timers)
        podman("network", "create", "--subnet", SUBNET, "--label", LABEL, NETWORK)
        common = ["run", "-d", "--label", LABEL, "--network", NETWORK]
        name, ip = ETCD
        podman(
            *common, "--name", name, "--hostname", name, "--ip", ip, ETCD_IMAGE,
            "etcd", "--name", name, "--data-dir", "/etcd-data",
            "--listen-client-urls", "http://0.0.0.0:2379", "--advertise-client-urls", f"http://{ip}:2379",
            "--listen-peer-urls", "http://0.0.0.0:2380", "--initial-advertise-peer-urls", f"http://{ip}:2380",
            "--initial-cluster", f"{name}=http://{ip}:2380",
        )
        self.wait_etcd()
        for name, ip in NODES.items():
            podman(
                *common, "--name", name, "--hostname", name, "--ip", ip,
                "-v", f"{self.cfgdir / (name + '.yml')}:/etc/patroni.yml:ro", IMAGE,
            )
        name, ip = HAPROXY
        podman(
            *common, "--name", name, "--hostname", name, "--ip", ip,
            "-v", f"{self.cfgdir / 'haproxy.cfg'}:/usr/local/etc/haproxy/haproxy.cfg:ro", HAPROXY_IMAGE,
        )
        return self.wait_healthy(mode, timeout=timeout)

    def down(self) -> None:
        ids = out("ps", "-aq", "--filter", f"label={LABEL}", check=False)
        if ids:
            podman("rm", "-f", "-v", "-t", "0", *ids.split(), timeout=120)
        if exists("network", NETWORK):
            podman("network", "rm", "-f", NETWORK)

    def wait_etcd(self, timeout: float = 30) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            proc = podman("exec", ETCD[0], "etcdctl", "endpoint", "health", check=False, timeout=10)
            if proc.returncode == 0:
                return
            time.sleep(0.5)
        raise TimeoutError("etcd not healthy")

    # --------------------------------------------------------------- queries
    def container_state(self, name: str) -> str:
        """running | paused | exited | missing ..."""
        if not exists("container", name):
            return "missing"
        return out("inspect", "-f", "{{.State.Status}}", name, check=False) or "unknown"

    def cluster(self, prefer: list[str] | None = None) -> dict | None:
        """Patroni /cluster as seen by the first node that answers with a leader in it."""
        order = (prefer or []) + [n for n in NODES if n not in (prefer or [])]
        code = (
            "import urllib.request,sys\n"
            "try:\n"
            "  print(urllib.request.urlopen('http://127.0.0.1:8008/cluster',timeout=2).read().decode())\n"
            "except Exception as e:\n"
            "  sys.exit(1)\n"
        )
        fallback = None
        for name in order:
            if self.container_state(name) != "running":
                continue
            try:
                proc = podman("exec", name, "python", "-c", code, check=False, timeout=8)
            except PodmanError:
                continue
            if proc.returncode != 0:
                continue
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                continue
            if any(m.get("role") == "leader" for m in data.get("members", [])):
                return data
            fallback = fallback or data
        return fallback

    def leader(self) -> str | None:
        data = self.cluster()
        if not data:
            return None
        for m in data.get("members", []):
            if m.get("role") == "leader" and m.get("state") == "running":
                return m["name"]
        return None

    def replicas(self) -> list[str]:
        lead = self.leader()
        return [n for n in NODES if n != lead]

    def health(self, mode: Mode) -> tuple[bool, str, dict | None]:
        data = self.cluster()
        if not data:
            return False, "no Patroni node answered /cluster", None
        members = data.get("members", [])
        leaders = [m for m in members if m.get("role") == "leader" and m.get("state") == "running"]
        if len(leaders) != 1:
            return False, f"{len(leaders)} running leaders", data
        tl = leaders[0].get("timeline")
        reps = [m for m in members if m.get("role") != "leader"]
        streaming = [m for m in reps if m.get("state") == "streaming" and m.get("timeline") == tl]
        if len(streaming) != len(NODES) - 1:
            states = {m["name"]: (m.get("role"), m.get("state"), m.get("timeline")) for m in members}
            return False, f"replicas not all streaming on TL {tl}: {states}", data
        if mode.sync and not any(m.get("role") == "sync_standby" for m in reps):
            return False, "no sync_standby yet", data
        return True, "ok", data

    def wait_healthy(self, mode: Mode, timeout: float = 180) -> float:
        t0 = time.monotonic()
        reason = ""
        while time.monotonic() - t0 < timeout:
            ok, reason, _ = self.health(mode)
            if ok:
                return time.monotonic() - t0
            time.sleep(1)
        raise TimeoutError(f"lab not healthy after {timeout:.0f}s: {reason}")

    def wait_leader(self, exclude: str | None = None, timeout: float = 120) -> tuple[str, float]:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            lead = self.leader()
            if lead and lead != exclude:
                return lead, time.monotonic() - t0
            time.sleep(0.5)
        raise TimeoutError(f"no new leader (excluding {exclude}) after {timeout:.0f}s")

    def status_text(self) -> str:
        st = self.state()
        lines = [f"mode: {st['mode'] if st else 'unknown'}"]
        for name in [ETCD[0], *NODES, HAPROXY[0]]:
            lines.append(f"  container {name:8s} {self.container_state(name)}")
        data = self.cluster()
        if data:
            for m in data.get("members", []):
                lines.append(
                    f"  patroni   {m['name']:8s} {m.get('role', '?'):13s} {m.get('state', '?'):10s} "
                    f"TL={m.get('timeline', '?')} lag={m.get('lag', '-')}"
                )
        else:
            lines.append("  patroni   (no answer)")
        return "\n".join(lines)
