"""Podman lab: 1 etcd, 3 Patroni/PostgreSQL nodes, 1 HAProxy on two networks.

- cluster network: etcd, replication, Patroni REST between peers
- client network:  HAProxy, the workload runner and probes, and the PostgreSQL nodes

The split lets a fault cut a node off from the cluster (DCS and peers) while
clients can still reach it, which is what a real split-brain risk looks like.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from string import Template

from .podman import PodmanError, exists, out, podman

REPO_ROOT = Path(__file__).resolve().parents[2]
LAB_DIR = REPO_ROOT / "lab"
SRC_DIR = REPO_ROOT / "src"

LAB_VERSION = 3  # bump when the topology changes so an old lab is never reused
LABEL = "io.kiwifarm.pgdrill=1"

CLUSTER_NET, CLUSTER_SUBNET = "pgdrill-cluster", "10.89.77.0/24"
CLIENT_NET, CLIENT_SUBNET = "pgdrill-client", "10.89.78.0/24"
LEGACY_NETWORKS = ("pgdrill",)

ETCD_IMAGE = "quay.io/coreos/etcd:v3.5.17"
HAPROXY_IMAGE = "docker.io/library/haproxy:3.0"
# Tag the image by its Containerfile so edits trigger a rebuild.
IMAGE = "localhost/pgdrill-patroni:" + hashlib.sha256(
    (LAB_DIR / "Containerfile.patroni").read_bytes()
).hexdigest()[:12]

ETCD = ("etcd", "10.89.77.5")
NODES = {"pg1": "10.89.77.11", "pg2": "10.89.77.12", "pg3": "10.89.77.13"}  # cluster side
CLIENT_IPS = {"pg1": "10.89.78.11", "pg2": "10.89.78.12", "pg3": "10.89.78.13"}  # client side
HAPROXY = ("haproxy", "10.89.78.20")
IP_TO_NODE = {ip: n for n, ip in NODES.items()} | {ip: n for n, ip in CLIENT_IPS.items()}


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

    def validate(self) -> None:
        # Patroni's documented constraint; otherwise the leader key can expire mid-retry.
        if self.loop_wait + 2 * self.retry_timeout > self.ttl:
            raise ValueError(
                f"loop_wait + 2*retry_timeout must be <= ttl "
                f"(got {self.loop_wait} + 2*{self.retry_timeout} > {self.ttl})"
            )


def _bool(v: bool) -> str:
    return "true" if v else "false"


class Lab:
    def __init__(self, workdir: Path):
        self.workdir = workdir.resolve()
        self.cfgdir = self.workdir / "lab"

    # ---------------------------------------------------------------- images
    def build(self, force: bool = False) -> None:
        if force or not exists("image", IMAGE):
            podman("build", "-q", "-t", IMAGE, "-f", str(LAB_DIR / "Containerfile.patroni"), str(LAB_DIR),
                   timeout=1800)
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
                    name=name, ip=ip, etcd_ip=ETCD[1],
                    cluster_subnet=CLUSTER_SUBNET, client_subnet=CLIENT_SUBNET,
                    ttl=timers.ttl, loop_wait=timers.loop_wait, retry_timeout=timers.retry_timeout,
                    failsafe=_bool(timers.failsafe), sync=_bool(mode.sync), strict=_bool(mode.strict),
                )
            )
        servers = "\n".join(f"    server {n} {ip}:5432 check port 8008" for n, ip in CLIENT_IPS.items())
        (self.cfgdir / "haproxy.cfg").write_text(
            Template((LAB_DIR / "haproxy.cfg.tmpl").read_text()).substitute(
                primary_servers=servers, replica_servers=servers
            )
        )
        (self.cfgdir / "state.json").write_text(
            json.dumps({"version": LAB_VERSION, "mode": mode.name, "timers": asdict(timers)})
        )

    def state(self) -> dict | None:
        f = self.cfgdir / "state.json"
        return json.loads(f.read_text()) if f.exists() else None

    def up(self, mode: Mode, timers: Timers = Timers(), timeout: float = 180) -> float:
        timers.validate()
        self.down()
        self.build()
        self._render(mode, timers)
        # --internal: no gateway, so podman cannot route between the two networks via the host.
        # Without it a node cut off from the cluster network still reaches etcd through the
        # client network's gateway, and a "partition" isolates nothing.
        podman("network", "create", "--internal", "--subnet", CLUSTER_SUBNET, "--label", LABEL, CLUSTER_NET)
        podman("network", "create", "--internal", "--subnet", CLIENT_SUBNET, "--label", LABEL, CLIENT_NET)
        run = ["run", "-d", "--label", LABEL]
        name, ip = ETCD
        podman(
            *run, "--name", name, "--hostname", name, "--network", f"{CLUSTER_NET}:ip={ip}", ETCD_IMAGE,
            "etcd", "--name", name, "--data-dir", "/etcd-data",
            "--listen-client-urls", "http://0.0.0.0:2379", "--advertise-client-urls", f"http://{ip}:2379",
            "--listen-peer-urls", "http://0.0.0.0:2380", "--initial-advertise-peer-urls", f"http://{ip}:2380",
            "--initial-cluster", f"{name}=http://{ip}:2380",
        )
        self.wait_etcd()
        for name, ip in NODES.items():
            podman(
                *run, "--name", name, "--hostname", name,
                "--network", f"{CLUSTER_NET}:ip={ip}", "--network", f"{CLIENT_NET}:ip={CLIENT_IPS[name]}",
                "-v", f"{self.cfgdir / (name + '.yml')}:/etc/patroni.yml:ro", IMAGE,
            )
        name, ip = HAPROXY
        podman(
            *run, "--name", name, "--hostname", name, "--network", f"{CLIENT_NET}:ip={ip}",
            "-v", f"{self.cfgdir / 'haproxy.cfg'}:/usr/local/etc/haproxy/haproxy.cfg:ro", HAPROXY_IMAGE,
        )
        return self.wait_healthy(mode, timeout=timeout)

    def down(self) -> None:
        ids = out("ps", "-aq", "--filter", f"label={LABEL}", check=False)
        if ids:
            podman("rm", "-f", "-v", "-t", "0", *ids.split(), timeout=120)
        for net in (CLUSTER_NET, CLIENT_NET, *LEGACY_NETWORKS):
            if exists("network", net):
                podman("network", "rm", "-f", net)

    def matches(self, mode: Mode, timers: Timers) -> bool:
        st = self.state()
        return bool(st) and st.get("version") == LAB_VERSION and st.get("mode") == mode.name \
            and st.get("timers") == asdict(timers)

    def wait_etcd(self, timeout: float = 60) -> None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                proc = podman("exec", ETCD[0], "etcdctl", "endpoint", "health", check=False, timeout=10)
                if proc.returncode == 0:
                    return
            except PodmanError:
                pass  # exec itself can be slow while the container starts: keep trying
            time.sleep(0.5)
        raise TimeoutError(f"etcd not healthy after {timeout:.0f}s")

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
            "except Exception:\n"
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
