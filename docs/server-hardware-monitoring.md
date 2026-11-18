# Server hardware monitoring

Design proposal for monitoring and alerting on server hardware health across
HPE, Dell, and generic vendors such as Supermicro.

Nothing in this branch is deployed yet. The phased plan lists the concrete
changes and the decisions needed before implementation starts.

## What exists today

Hardware collection runs on each host as cron jobs that write `.prom` files into
`/opt/node_exporter/textfile_collector`, installed by
`ansible/playbooks/custom_exporters.yml` in
[rackerlabs/genestack](https://github.com/rackerlabs/genestack). That directory
is mounted into the node-exporter DaemonSet through
`--collector.textfile.directory`, configured in
[`../helm-configs/opentelemetry-kube-stack/opentelemetry-kube-stack-helm-overrides.yaml`](../helm-configs/opentelemetry-kube-stack/opentelemetry-kube-stack-helm-overrides.yaml),
so the series arrive with `job="node-exporter"` and the `k8s_node_name` label
the existing rules key off.

| Collector | Vendor gate | Metrics | Alerts |
|---|---|---|---|
| `ssacli_exporter.py` | `system_vendor == "HP"` | `hp_smart_array_*` | [`../alerts/prometheus-alerts/hpraid/`](../alerts/prometheus-alerts/hpraid/) |
| `perccli.py` | `system_vendor == "Dell Inc."` | `megaraid_*` | [`../alerts/prometheus-alerts/megaraid/`](../alerts/prometheus-alerts/megaraid/) |
| `md_info_detail.sh` | `system_vendor == "Dell Inc."` | `node_md_*` | [`../alerts/prometheus-alerts/node/`](../alerts/prometheus-alerts/node/) |
| `nvme_metrics.py` | `system_vendor == "Dell Inc."` | `nvme_*` | [`../alerts/prometheus-alerts/node/`](../alerts/prometheus-alerts/node/) |
| `multipathd_info` | compute nodes | `multipath_*` | [`../alerts/prometheus-alerts/node/`](../alerts/prometheus-alerts/node/) |
| node-exporter built-ins | all hosts | `node_bonding_*`, `node_network_*` | [`../alerts/prometheus-alerts/node/`](../alerts/prometheus-alerts/node/) |

RAID controllers, logical and physical drives, md arrays, NVMe, and multipath are
therefore already covered on HP and Dell.

## Metric contract

Rules in this repository depend on metric and label names produced by scripts in
the genestack repository. Neither side has CI that would catch a rename, so these
names are a contract: changing a producer requires the matching rule change in
the same window.

| Metric prefix | Produced by | Vendor gate | Interval | Consumed by |
|---|---|---|---|---|
| `hp_smart_array_*` | `ansible/playbooks/extra/custom_exporters/ssacli_exporter.py` | `system_vendor == "HP"` | 5m | [`../alerts/prometheus-alerts/hpraid/`](../alerts/prometheus-alerts/hpraid/) |
| `megaraid_*` | `ansible/playbooks/extra/custom_exporters/perccli.py` | `system_vendor == "Dell Inc."` | 5m | [`../alerts/prometheus-alerts/megaraid/`](../alerts/prometheus-alerts/megaraid/) |
| `node_md_*` | `ansible/playbooks/extra/custom_exporters/md_info_detail.sh` | `system_vendor == "Dell Inc."` | 3m | [`../alerts/prometheus-alerts/node/`](../alerts/prometheus-alerts/node/) |
| `nvme_*` | `ansible/playbooks/extra/custom_exporters/nvme_metrics.py` | `system_vendor == "Dell Inc."` | 3m | [`../alerts/prometheus-alerts/node/`](../alerts/prometheus-alerts/node/) |
| `multipath_*` | `ansible/playbooks/extra/custom_exporters/multipathd_info` | `openstack_compute_nodes` group | 5m | [`../alerts/prometheus-alerts/node/`](../alerts/prometheus-alerts/node/) |

All of the above are installed by `ansible/playbooks/custom_exporters.yml`; the
vendor CLI tools they shell out to are installed separately by
`ansible/roles/host_setup/tasks/raid_cli_tools.yml`, which runs as part of
`host-setup.yml`.

Every rule asserts `job="node-exporter"` and keys host identity off
`k8s_node_name`, both of which come from the DaemonSet rather than the scripts.

Two failure modes are invisible by construction, because a textfile collector
that never writes produces no series rather than a zero value:

- A host whose vendor gate does not match gets no metrics and no alert about it.
- A cron job that fails, or a vendor CLI that is absent, leaves the last `.prom`
  file in place until it is overwritten.

`node-textfile-collector-scrape-error` catches a malformed `.prom` file but not
an absent one. An absence check would need a rule per expected series scoped to
the hosts that should have it, which needs a label identifying vendor or role on
the node; no such label exists today.

## Gaps

| Gap | Detail |
|---|---|
| Chassis health | Nothing reports CPU, DIMM, power supply, fan, or temperature health on any vendor |
| Generic vendors | No collection path at all for Supermicro or other non-HP, non-Dell hardware |
| HP controller battery | `ssacli_exporter.py` emits no battery or capacitor metric, so the HP equivalent of `megaraid-bbus-status` cannot be written |
| HP controller cache | `hp_smart_array_controller_cache_status` is collected but no alert rule uses it |
| Disk IO saturation | The recording rule `instance_device:node_disk_io_time_seconds:rate5m` exists but no alert uses it |
| Single-slave bonds | `node-bonding-degraded` compares `node_bonding_slaves` to `node_bonding_active`, so a bond configured with one slave looks healthy |
| LACP state | node-exporter exposes no aggregator or partner-churn state, so LACP-specific failures are invisible |
| HPE vendor gate | `raid_cli_tools.yml` accepts both `hp` and `hpe`, so `ssacli` installs on Gen10 and later, but `custom_exporters.yml` gates the cron job on `system_vendor == "HP"` exactly. On an `HPE`-branded host the CLI is present and no `hp_smart_array_*` series are ever produced, with nothing to indicate it |
| md and NVMe coverage | Both collectors sit inside the Dell-only block although neither is vendor-specific |
| Silent collector failure | A collector that never runs produces no series, so a broken or ungated host is indistinguishable from healthy hardware. No alert covers the absence |

## Recommended architecture

Two collection planes with distinct responsibilities.

```text
In-band plane (per host)
  vendor CLI + script -> .prom file -> node-exporter textfile -> Prometheus
  owns: RAID, drives, SMART, md, NVMe, multipath, bonding

Out-of-band plane (central)
  exporter Deployment -> IPMI/RMCP or Redfish -> BMC -> Prometheus
  owns: chassis health, CPUs, DIMMs, power supplies, fans, temperatures, SEL
```

The split follows what each plane can see. In-band tooling has the clearest view
of RAID topology and per-drive detail and needs no credentials, but it dies with
the host. The BMC reports chassis health uniformly across vendors and keeps
answering when the host is down or wedged, which is when a hardware alert matters
most.

For the out-of-band plane, start with
[prometheus-community/ipmi_exporter](https://github.com/prometheus-community/ipmi_exporter).
It is the only option that covers HPE, Dell, and Supermicro through one code
path, it is a Prometheus community project with a
[community Helm chart](https://github.com/prometheus-community/helm-charts/tree/main/charts/prometheus-ipmi-exporter),
and its multi-target design means one Deployment polls the whole fleet. Its
weakness is granularity: sensor state tells you something is faulted, not which
DIMM.

Add [mrlhansen/idrac_exporter](https://github.com/mrlhansen/idrac_exporter) later
where that granularity matters. It speaks Redfish to both iDRAC and iLO and
exposes per-component health, so one exporter serves HPE and Dell. Supermicro
stays on IPMI.

Neither option needs a vendor agent such as OMSA or AMS installed on the hosts,
which is the main maintenance cost of the alternatives
([galexrt/dellhw_exporter](https://github.com/galexrt/dellhw_exporter), an
`hpasmcli` wrapper, or SNMP with vendor MIBs).

Both BMC exporters implement the multi-target pattern, which this repository
already has precedent for in the F5 SNMP work and the openstack-api-exporter
chart: an exporter Deployment with no device identity of its own, BMC credentials
in a Secret mounted as its config, and a `ScrapeConfig` or `Probe` that relabels
each BMC address into the `target` parameter. Target inventory is
environment-specific, so it belongs in the environment overrides repository with
an `.example` file here.

## Metrics and alerts

### Phase 1, metrics already flowing

| Alert | Expression |
|---|---|
| `hp-smart-array-controller-cache-status` | `hp_smart_array_controller_cache_status{job="node-exporter"} < 1` |
| `node-disk-io-saturation` | `instance_device:node_disk_io_time_seconds:rate5m > 0.9` |
| `node-bonding-insufficient-slaves` | `node_bonding_slaves < 2` |

### Phase 3, IPMI chassis health

New directory `alerts/prometheus-alerts/hardware/`, on
[`ipmi_exporter` metrics](https://github.com/prometheus-community/ipmi_exporter/blob/master/docs/metrics.md).

| Alert | Expression |
|---|---|
| `hardware-bmc-unreachable` | `ipmi_up == 0` |
| `hardware-ipmi-sensor-state` | `ipmi_sensor_state > 1` |
| `hardware-ipmi-temperature` | `ipmi_temperature_state > 1` |
| `hardware-ipmi-fan-failure` | `ipmi_fan_speed_state > 1 or ipmi_chassis_cooling_fault_state > 0` |
| `hardware-ipmi-power-supply` | `ipmi_sensor_state{type=~".*Power Supply.*"} > 1` |
| `hardware-ipmi-voltage` | `ipmi_voltage_state > 1` |
| `hardware-ipmi-drive-fault` | `ipmi_chassis_drive_fault_state > 0` |
| `hardware-ipmi-chassis-power-off` | `ipmi_chassis_power_state == 0` |
| `hardware-sel-events-growing` | `increase(ipmi_sel_logs_count[1h]) > 0` |

Supporting series available for dashboards rather than alerts:
`ipmi_temperature_celsius`, `ipmi_fan_speed_rpm`, `ipmi_voltage_volts`,
`ipmi_power_watts`, `ipmi_dcmi_power_consumption_watts`, `ipmi_bmc_info`.

### Phase 4, Redfish component health

On [`idrac_exporter` metrics](https://github.com/mrlhansen/idrac_exporter#list-of-metrics),
for per-component attribution on HPE and Dell.

| Alert | Expression |
|---|---|
| `hardware-system-health` | `idrac_system_health != 0` |
| `hardware-memory-module-health` | `idrac_memory_module_health != 0` |
| `hardware-cpu-health` | `idrac_cpu_health != 0` |
| `hardware-power-supply-health` | `idrac_power_supply_health != 0` |
| `hardware-fan-health` | `idrac_sensors_fan_health != 0` |
| `hardware-storage-drive-health` | `idrac_storage_drive_health != 0` |
| `hardware-storage-drive-wear` | `idrac_storage_drive_life_left_percent < 10` |
| `hardware-controller-cache-health` | `idrac_storage_controller_cache_health != 0` |
| `hardware-controller-battery-health` | `idrac_dell_controller_battery_health != 0` |
| `hardware-network-port-down` | `idrac_network_port_link_up == 0` |

### Conventions

Every rule follows the existing repository pattern: one file per concern,
`warning` and `critical` variants of the same expression at different `for`
durations, `group_name`, `node_hostname`, `correlation_key`, and the
`default_runbook_url` and `default_dashboard_url` placeholders. The hpraid and
megaraid rules use 5m for `warning` and 15m for `critical`; conditions that are
transient by design get longer windows, as `megaraid-bbus-status` does with 30m
and 24h.

Each new rule file needs a promtool test in `tests/`, following
[`../tests/f5-bigip-ha.test.yaml`](../tests/f5-bigip-ha.test.yaml).

## Phased plan

1. Close the gaps on metrics already flowing: the three Phase 1 alerts above,
   with tests. No new deployment, no credentials.
2. Fix collection coverage in the genestack repository: accept `HPE` alongside
   `HP`, move the md and NVMe cron jobs out of the Dell-only block, and add a
   battery metric to `ssacli_exporter.py`. Then add the matching HP battery
   alert here, mirroring `megaraid-bbus-status`.
3. Stand up the out-of-band plane: an `install-ipmi-exporter.sh` installer and
   `helm-configs/ipmi-exporter/` override following the existing component
   installers, registration in
   [`../bin/observability-components.yaml`](../bin/observability-components.yaml),
   a target-inventory `.example` file, the `alerts/prometheus-alerts/hardware/`
   rules with tests, and a dashboard under `etc/grafana-dashboards/`.
4. Optional depth: `idrac_exporter` for HPE and Dell component health, and
   [smartctl_exporter](https://github.com/prometheus-community/smartctl_exporter)
   for per-drive SMART on hosts with no vendor RAID CLI.

## Decisions needed

1. Is a network route from the observability namespace to the BMC management
   network available, or must the BMC plane run outside the cluster?
2. Where do BMC credentials come from, and is one credential set per vendor
   acceptable, or is it per site or per host?
3. Should BMC target inventory be static per environment, or discovered from the
   Ironic API?
4. Is `ipmi_exporter` the right fleet-wide baseline, or should Redfish be the
   baseline with IPMI only as the Supermicro fallback?
5. Should Phase 1 ship on its own first, since it needs none of the above?

Upstream documentation referenced above was paraphrased and summarised. Content
was rephrased for compliance with licensing restrictions.
