#!/usr/bin/env python3
"""
claas-agent — the supplier-side node agent (v0: report + heartbeat + listen)

What v0 does (all safe, no code execution):
  1. Signs in to Supabase as the supplier (email+password -> JWT).
  2. Detects local GPUs via nvidia-smi.
  3. Registers/updates a row in the `nodes` table.
  4. Heartbeats every 30s (nodes.last_heartbeat).
  5. Polls `assignments` for pending jobs on this node and logs what it
     WOULD run. Execution is deliberately not implemented yet — see
     EXECUTION_PLAN below and ROADMAP.md §6 before wiring it up.

Usage:
    python3 claas_agent.py --name "Dorm Rig 1"
    python3 claas_agent.py --list-gpus          # just print detected hardware

Env:
    CLAAS_SUPABASE_URL     project URL
    CLAAS_SUPABASE_ANON_KEY  anon/publishable key (safe to keep on the machine)
    CLAAS_AGENT_EMAIL      supplier account email
    CLAAS_AGENT_PASSWORD   supplier account password
    (or CLAAS_SERVICE_KEY to run in service mode during development)

Dependencies: requests  (pip install requests)
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------

def detect_gpus(nvidia_smi_output: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Parse `nvidia-smi --query-gpu=...` CSV output into structured specs.
    `nvidia_smi_output` is injectable for testing. Returns [] when no NVIDIA
    GPU (or no driver) is present — that's a valid, reportable state.
    """
    if nvidia_smi_output is None:
        try:
            nvidia_smi_output = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=name,memory.total,driver_version,compute_cap",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=15,
            ).stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

    gpus: List[Dict[str, Any]] = []
    for line in nvidia_smi_output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            gpus.append({
                "model": parts[0],
                "vram_gb": round(float(parts[1]) / 1024, 1),
                "driver": parts[2],
                "compute_cap": parts[3] if len(parts) > 3 else None,
            })
        except ValueError:
            continue
    # collapse identical cards into {"model", "vram_gb", "count"}
    counts: Dict[tuple, Dict[str, Any]] = {}
    for g in gpus:
        key = (g["model"], g["vram_gb"])
        counts.setdefault(key, {**g, "count": 0})
        counts[key]["count"] += 1
    return list(counts.values())


def sample_telemetry(nvidia_smi_output: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Live per-GPU telemetry: utilization %, temp C, power W, VRAM usage.
    Polled each heartbeat and written to nodes.benchmark — the beginning of a
    signal plane (utilization/temperature/power as first-class data, the way
    InferMesh treats them; see COMPETITIVE_INTEL.md §3). Injectable output
    for offline tests, same contract as detect_gpus.
    """
    if nvidia_smi_output is None:
        try:
            nvidia_smi_output = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=index,utilization.gpu,temperature.gpu,power.draw,"
                 "memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=15,
            ).stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []

    out: List[Dict[str, Any]] = []
    for line in nvidia_smi_output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            out.append({
                "gpu": int(parts[0]),
                "util_pct": float(parts[1]),
                "temp_c": float(parts[2]),
                "power_w": float(parts[3]),
                "vram_used_gb": round(float(parts[4]) / 1024, 1),
                "vram_total_gb": round(float(parts[5]) / 1024, 1),
            })
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# Minimal Supabase REST client (auth + the few calls the agent needs)
# ---------------------------------------------------------------------------

class SupabaseAgent:
    """Thin REST wrapper: sign in as a user, then CRUD with RLS in force."""

    def __init__(self, base_url: str, anon_key: str,
                 email: Optional[str] = None, password: Optional[str] = None,
                 service_key: Optional[str] = None) -> None:
        self.base = base_url.rstrip("/")
        self.anon_key = service_key or anon_key
        self.token: Optional[str] = None
        self.user: Optional[Dict[str, Any]] = None
        if service_key:
            print("⚠  running with the SERVICE key — RLS bypassed. Dev mode only.")
            self.token = service_key
        elif email and password:
            self._sign_in(email, password)
        else:
            raise SystemExit("Need either agent email+password or a service key.")

    def _sign_in(self, email: str, password: str) -> None:
        r = requests.post(
            f"{self.base}/auth/v1/token?grant_type=password",
            headers={"apikey": self.anon_key, "Content-Type": "application/json"},
            json={"email": email, "password": password}, timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        self.token = data["access_token"]
        self.user = data.get("user")
        print(f"✓ signed in as {self.user['email'] if self.user else email}")

    def _headers(self) -> Dict[str, str]:
        return {
            "apikey": self.anon_key,
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def upsert_node(self, node_id: Optional[str], name: str,
                    gpus: List[Dict[str, Any]], region: Optional[str]) -> str:
        """Register this machine (insert) or refresh it (update). Returns node id."""
        payload: Dict[str, Any] = {
            "name": name,
            "gpus": gpus,
            "status": "online",
            "agent_version": "0.1.0",
            "last_heartbeat": _now_iso(),
        }
        if region:
            payload["region"] = region
        if node_id:
            r = requests.patch(f"{self.base}/rest/v1/nodes?id=eq.{node_id}",
                               headers=self._headers(), json=payload, timeout=15)
            r.raise_for_status()
            return node_id
        payload["id"] = str(uuid.uuid4())
        r = requests.post(f"{self.base}/rest/v1/nodes",
                          headers=self._headers(), json=payload, timeout=15)
        r.raise_for_status()
        return r.json()[0]["id"]

    def heartbeat(self, node_id: str,
                  telemetry: Optional[List[Dict[str, Any]]] = None) -> None:
        payload: Dict[str, Any] = {"last_heartbeat": _now_iso(), "status": "online"}
        if telemetry is not None:
            payload["benchmark"] = {"telemetry": telemetry, "sampled_at": _now_iso()}
        r = requests.patch(f"{self.base}/rest/v1/nodes?id=eq.{node_id}",
                           headers=self._headers(), json=payload, timeout=15)
        r.raise_for_status()

    def pending_assignments(self, node_id: str) -> List[Dict[str, Any]]:
        r = requests.get(
            f"{self.base}/rest/v1/assignments"
            f"?select=*&node_id=eq.{node_id}&status=eq.pending"
            f"&order=created_at.asc&limit=5",
            headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def update_assignment(self, assignment_id: str, fields: Dict[str, Any]) -> None:
        r = requests.patch(f"{self.base}/rest/v1/assignments?id=eq.{assignment_id}",
                           headers=self._headers(), json=fields, timeout=15)
        r.raise_for_status()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


# ---------------------------------------------------------------------------
# EXECUTION PLAN — read before implementing (ROADMAP.md §6)
# ---------------------------------------------------------------------------
# The command we will eventually run for an assignment looks like:
#
#   docker run --rm --interactive \
#     --gpus all \                      # GPU passthrough (weakens isolation — accept + disclose)
#     --user 1000:1000 \                # never root
#     --cap-drop ALL --security-opt no-new-privileges \
#     --memory 24g --pids-limit 256 \   # resource ceilings
#     --network claas-net \             # dedicated bridge; egress filtered
#     <CURATED_IMAGE>                   # ONLY images from the platform allowlist
#
# Non-negotiables: never mount /var/run/docker.sock, never --privileged,
# never run an arbitrary buyer-supplied image (curated names only),
# always wrap the exposed port in an authenticated tunnel, always heartbeat.
# ---------------------------------------------------------------------------

def run_job(assignment: Dict[str, Any]) -> None:
    """v0 stub: log intent. Actual execution lands after the security review."""
    print(f"  [job {assignment['id'][:8]}] would run image '{assignment['image']}' "
          f"for buyer {assignment['buyer_id'][:8]} — execution not implemented in v0")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="CLaaS supplier node agent (v0)")
    ap.add_argument("--name", default="My Node", help="Display name for this machine")
    ap.add_argument("--region", default=None, help="e.g. us-east")
    ap.add_argument("--list-gpus", action="store_true", help="Print detected GPUs and exit")
    ap.add_argument("--once", action="store_true", help="Single pass (register + one poll)")
    args = ap.parse_args()

    gpus = detect_gpus()
    if args.list_gpus:
        print(json.dumps(gpus, indent=2) if gpus
              else "No NVIDIA GPUs detected (or nvidia-smi missing).")
        return 0
    print(f"✓ detected {sum(g.get('count', 1) for g in gpus)} GPU(s): "
          + (", ".join(f"{g['count']}x {g['model']} ({g['vram_gb']}GB)" for g in gpus)
             or "none"))

    sb = SupabaseAgent(
        os.environ.get("CLAAS_SUPABASE_URL", ""),
        os.environ.get("CLAAS_SUPABASE_ANON_KEY", ""),
        email=os.environ.get("CLAAS_AGENT_EMAIL"),
        password=os.environ.get("CLAAS_AGENT_PASSWORD"),
        service_key=os.environ.get("CLAAS_SERVICE_KEY"),
    )

    node_id = sb.upsert_node(None, args.name, gpus, args.region)
    print(f"✓ node registered: {node_id}")

    running = True

    def _stop(signum, frame):
        nonlocal running
        running = False
        print("\n… shutting down (node will go offline at next heartbeat miss)")

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while running:
        try:
            sb.heartbeat(node_id, telemetry=sample_telemetry())
            for a in sb.pending_assignments(node_id):
                print(f"→ pending job {a['id'][:8]} (image {a['image']})")
                sb.update_assignment(a["id"], {"status": "running",
                                               "started_at": _now_iso(),
                                               "last_heartbeat": _now_iso()})
                run_job(a)
                sb.update_assignment(a["id"], {"status": "succeeded",
                                               "finished_at": _now_iso()})
        except requests.RequestException as exc:
            print(f"⚠ network error: {exc} — retrying")
        if args.once:
            break
        time.sleep(30)

    return 0


if __name__ == "__main__":
    sys.exit(main())
