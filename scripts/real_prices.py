#!/usr/bin/env python3
"""
real_prices.py — pull REAL market prices for GPU-hours and compare them with
the CLaaS simulated market. Answers: "is our simulated pricing realistic?"

Sources (both free):
  1. Azure Retail Prices API — keyless, no account, documented & stable.
     https://learn.microsoft.com/azure/cost-management-billing/manage/retail-prices-azure-api
  2. Vast.ai bundles API — real peer-marketplace asking prices. Keyless queries
     usually work from residential IPs; their WAF sometimes blocks datacenter
     IPs. For reliable access create a free account and:
         export VAST_API_KEY=...     # https://vast.ai/developers/api

Also reads the local CLaaS engine (http://localhost:8000/market) when running,
to print the sim-vs-reality comparison.

Stdlib only — no dependencies. Run from the project root:

    python3 scripts/real_prices.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = {"User-Agent": "claas-mvp/0.1 (market reality check)"}
AZURE_API = "https://prices.azure.com/api/retail/prices"
VAST_API = "https://console.vast.ai/api/v0/bundles"
SIM_URL = "http://localhost:8000/market"
REGION = os.environ.get("CLaaS_REGION", "eastus")

# Azure VM SKUs to track: (sku, gpu model, GPUs per VM)
AZURE_SKUS = [
    ("ND96is_H100_v5", "H100 80GB", 8),
    ("NC40ads_H100_v5", "H100 80GB", 1),
    ("NC24ads_A100_v4", "A100 80GB", 1),
    ("ND96asr_v4", "A100 40GB", 8),
    ("NC4as_T4_v3", "T4 16GB", 1),
]


# ----------------------------------------------------------------- helpers --
def http_json(url: str, payload: dict | None = None, timeout: int = 20):
    """GET or POST a URL and return parsed JSON, or raise."""
    if payload is None:
        req = urllib.request.Request(url, headers=UA)
    else:
        body = json.dumps(payload).encode()
        headers = dict(UA)
        headers["Content-Type"] = "application/json"
        key = os.environ.get("VAST_API_KEY")
        if key and "vast" in url:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def norm(s: str) -> str:
    """Normalize a sku for comparison (Azure's API strips underscores)."""
    return s.replace("_", "").replace(" ", "").lower()


# ------------------------------------------------------------------ Azure --
def azure_fetch_family(family: str) -> list[dict]:
    """Fetch all Consumption VM price rows whose skuName contains `family`."""
    filt = (
        f"serviceName eq 'Virtual Machines' and priceType eq 'Consumption' "
        f"and armRegionName eq '{REGION}' and contains(skuName,'{family}')"
    )
    params = urllib.parse.urlencode({"$filter": filt, "currencyCode": "'USD'", "$top": 100})
    items: list[dict] = []
    url = f"{AZURE_API}?{params}"
    for _ in range(3):  # follow pagination (up to 3 pages is plenty here)
        page = http_json(url)
        items.extend(page.get("Items", []))
        url = page.get("NextPageLink")
        if not url:
            break
    return items


def azure_rows() -> list[dict]:
    """Collapse Azure rows into per-GPU-hour on-demand + spot prices."""
    best: dict[tuple[str, str, int, bool], float] = {}
    items: list[dict] = []
    for fam in ("H100", "A100", "T4"):
        try:
            items.extend(azure_fetch_family(fam))
        except Exception as exc:
            print(f"  ! Azure query for {fam} failed: {exc}")
    for it in items:
        if "Windows" in it.get("productName", ""):
            continue  # Linux pricing is the comparable baseline
        sku, price = it.get("skuName", ""), it.get("retailPrice")
        if price is None:
            continue
        is_spot = "spot" in sku.lower()
        key_sku = norm(sku).replace("spot", "")
        for want, model, gpus in AZURE_SKUS:
            if key_sku == norm(want):
                key = (want, model, gpus, is_spot)
                if key not in best or price < best[key]:
                    best[key] = price
    rows = []
    for (want, model, gpus, is_spot), price in sorted(best.items()):
        rows.append({
            "model": f"{model} · {gpus}x", "kind": "spot" if is_spot else "on-demand",
            "per_gpu": price / gpus, "vm_total": price,
        })
    return rows


# ------------------------------------------------------------------- Vast --
def vast_rows() -> list[dict] | None:
    """
    Best-effort fetch of real peer-marketplace asks from Vast.ai.
    Returns None (not []) when the endpoint is unreachable/WAF'd so callers
    can distinguish 'no offers' from 'source unavailable'.
    """
    names = ["H100", "H100_SXM", "RTX 4090"]
    for gpu in names:
        payload = {
            "limit": 50,
            "type": "on-demand",
            "gpu_name": {"eq": gpu},
            "rentable": {"eq": True},
            "order": [["dph_total", "asc"]],
        }
        try:
            data = http_json(VAST_API, payload)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                return None  # WAF / rate limit — needs residential IP or API key
            continue
        except Exception:
            continue
        offers = data.get("offers") or []
        if not offers:
            continue
        per_gpu = [o["dph_total"] / max(1, o.get("num_gpus", 1))
                   for o in offers if o.get("dph_total")]
        if not per_gpu:
            continue
        return [{
            "model": gpu.replace("_", " "),
            "n": len(per_gpu),
            "min": min(per_gpu),
            "median": statistics.median(per_gpu),
        }]
    return None


# -------------------------------------------------------------------- sim --
def sim_state() -> dict | None:
    try:
        return http_json(SIM_URL, timeout=3)
    except Exception:
        return None


# -------------------------------------------------------------------- main --
def main() -> int:
    print("=" * 74)
    print("CLaaS reality check — real market GPU prices vs. our simulation")
    print("=" * 74)

    print(f"\n[1] Azure Retail Prices API  (region: {REGION}, Linux, keyless)")
    az = azure_rows()
    if az:
        print(f"  {'Model':<12}{'Type':<12}{'$/GPU-hr':>10}{'Full VM $/hr':>14}")
        for r in az:
            print(f"  {r['model']:<12}{r['kind']:<12}{r['per_gpu']:>10.2f}{r['vm_total']:>14.2f}")
    else:
        print("  ! no rows returned (API unreachable?)")

    print("\n[2] Vast.ai peer marketplace (live asking prices)")
    vr = vast_rows()
    if vr:
        for r in vr:
            print(f"  {r['model']:<12} median ask ${r['median']:.2f}/GPU-hr   "
                  f"cheapest ${r['min']:.2f}   (n={r['n']} offers)")
    else:
        print("  ! unavailable from this network (Vast's WAF blocks datacenter IPs).")
        print("    Try from a residential connection, or set VAST_API_KEY (free account):")
        print("    https://vast.ai/developers/api")

    sim = sim_state()
    print("\n[3] CLaaS simulation (local engine)")
    if sim:
        print(f"  simulated fair value ${sim['fair_value']:.2f}/GPU-hr   "
              f"clock {sim['simulated_clock']}   demand {sim['demand_label']} "
              f"(x{sim['demand_multiplier']:.2f})")
        for hw, ref in (sim.get("reference_prices") or {}).items():
            print(f"    {hw:<12} ≈ ${ref:.2f}")
    else:
        print("  ! engine not running on :8000 — start it with  ./run.sh  and re-run")

    print("\n" + "-" * 74)
    print("Read this:")
    print("  * Hyperscaler on-demand list prices (Azure) are the ceiling of the")
    print("    market; their SPOT prices show how deeply capacity discounts when")
    print("    it would otherwise idle.")
    print("  * Peer marketplaces (Vast/RunPod class) clear well below hyperscaler")
    print("    list — that band is where a CLaaS-style order book would live.")
    print("  * If our simulated spot trades inside that band, the demo is telling")
    print("    a credible story. Compare [3] against [1]-[2] above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
