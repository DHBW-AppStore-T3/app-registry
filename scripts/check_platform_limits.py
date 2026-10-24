#!/usr/bin/env python3
"""Compare the validator's ceilings (cpu, memory, storage per container/volume) with the deployed platform.

    check_platform_limits.py [values.yaml-url]

The limits are configuration of the platform (podApps.maxCpu / maxMemory / maxStorage in the Helm chart of
DHBW-AppStore-T3/deployment, branch main), not part of the contract parser, so the contract-drift test cannot see
them. A difference is printed as a GitHub warning: with ceilings above the platform's, the CI accepts specs
the AppStore rejects; below it, the CI rejects specs the AppStore would run. Exit code 1 only if the chart cannot
be read.  Dependencies: PyYAML.
"""
from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_appstore_yaml import MAX_CPU_M, MAX_MEM_MI, MAX_STORAGE_MI, cpu_m, mem_mi  # noqa: E402

URL = "https://raw.githubusercontent.com/DHBW-AppStore-T3/deployment/main/k8s/charts/appstore/values.yaml"


def main(argv: list[str]) -> int:
    url = argv[0] if argv else URL
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            pod_apps = yaml.safe_load(r.read())["podApps"]
        chart = (cpu_m(str(pod_apps["maxCpu"])), mem_mi(str(pod_apps["maxMemory"])), mem_mi(str(pod_apps["maxStorage"])))
    except (urllib.error.URLError, OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        print(f"::error::cannot read the platform limits from {url}: {exc}")
        return 1
    validator = (MAX_CPU_M, MAX_MEM_MI, MAX_STORAGE_MI)
    print(f"validator: {validator[0]}m / {validator[1]}Mi / {validator[2]}Mi   deployed chart: {chart[0]}m / {chart[1]}Mi / {chart[2]}Mi")
    if chart != validator:
        print("::warning::the validator's ceilings differ from the deployed Helm chart (podApps.maxCpu/maxMemory/maxStorage); "
              "until they are equal the CI and the AppStore disagree about specs near the limit")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
