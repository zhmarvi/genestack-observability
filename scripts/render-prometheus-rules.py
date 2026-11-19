#!/usr/bin/env python3
"""Render Genestack rule files into native Prometheus rule files.

Rule files in this repository wrap their rule groups in the
`additionalPrometheusRulesMap` key that `kube-prometheus-stack` expects.
`promtool test rules` cannot load that format, so unit tests have no way to
reference a rule file directly.

This script strips the wrapper and writes the plain `groups:` document that
promtool understands.

Render every rule file into one document:

    python3 scripts/render-prometheus-rules.py \
        alerts/prometheus-alerts \
        -o tests/.rendered/all.yaml

Render a single rule file, which is what the test files under tests/ expect:

    python3 scripts/render-prometheus-rules.py \
        alerts/prometheus-alerts/node/node-disk-io-saturation.yaml \
        -o tests/.rendered/node-disk-io-saturation.yaml

Then run the matching test:

    promtool test rules tests/node-disk-io-saturation.test.yaml
"""

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Missing dependency: PyYAML", file=sys.stderr)
    print("Install it with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def iter_rule_files(paths):
    """Yield every rule file in the given files and directories."""
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            yield from sorted(set(path.rglob("*.yaml")) | set(path.rglob("*.yml")))
        elif path.is_file():
            yield path
        else:
            print(f"ERROR: No such file or directory: {path}", file=sys.stderr)
            sys.exit(1)


def extract_groups(path):
    """Return the rule groups contained in one rule file."""
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}

    rules_map = document.get("additionalPrometheusRulesMap")
    if rules_map is None:
        return []
    if not isinstance(rules_map, dict):
        print(
            f"WARNING: 'additionalPrometheusRulesMap' is not a mapping in {path}, skipping",
            file=sys.stderr,
        )
        return []

    groups = []
    for key, value in rules_map.items():
        if not isinstance(value, dict) or not isinstance(value.get("groups"), list):
            print(f"WARNING: '{key}' in {path} has no group list, skipping", file=sys.stderr)
            continue
        groups.extend(value["groups"])
    return groups


def main():
    parser = argparse.ArgumentParser(
        description="Render Genestack rule files into native Prometheus rule files."
    )
    parser.add_argument("paths", nargs="+", help="Rule files or directories to render.")
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Destination file. Parent directories are created as needed.",
    )
    args = parser.parse_args()

    groups = []
    seen = {}
    for path in iter_rule_files(args.paths):
        for group in extract_groups(path):
            name = group.get("name")
            # Separate source files may reuse a group name, which is fine when
            # each becomes its own PrometheusRule but invalid inside a single
            # rendered file. Suffix the repeats so promtool accepts the result;
            # group names carry no meaning for checks or unit tests.
            if name in seen:
                seen[name] += 1
                group = dict(group, name=f"{name}-{seen[name]}")
            else:
                seen[name] = 1
            groups.append(group)

    if not groups:
        print("ERROR: No rule groups found in the given paths.", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        yaml.safe_dump({"groups": groups}, handle, default_flow_style=False, sort_keys=False, width=4096)

    print(f"Wrote {len(groups)} group(s) to {output}")


if __name__ == "__main__":
    main()
