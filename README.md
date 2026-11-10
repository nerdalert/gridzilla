# Gridzilla

Gridzilla is a standalone, non-destructive performance harness for a persistent
three-site Grid + llm-d-sim Kind environment. It attaches to an environment
created by Grid's signals combined-site xtask; it never provisions or deletes
clusters. llm-d-sim is the only pressure source. Dynamo is not used.

The setup xtask's validated `handoff.json` is the authority for cluster names,
kubeconfigs, endpoints, resource identities, credentials file paths, and image
IDs. Gridzilla refuses implicit kubeconfig contexts and records the handoff
with secret values redacted.

See [docs/usage.md](docs/usage.md) for installation, the handoff contract,
profiles, safety bounds, and command examples. The [redacted handoff
template](examples/handoff.redacted.json) shows the producer's v1alpha1 shape;
replace every path and identity with the setup run's actual handoff before use.

## Quick start

```sh
gridzilla doctor --handoff /path/to/handoff.json
gridzilla smoke --handoff /path/to/handoff.json
gridzilla run --handoff /path/to/handoff.json --profile profiles/baseline.json
gridzilla report evidence/<run-id>
```

`run` leaves the environment running. `restore` restores only the simulator
configuration snapshot recorded by that benchmark. It does not delete cluster
resources.

## Development

```sh
python3 -m unittest discover -s tests -v
K6_BIN="$PWD/.tools/k6-v1.8.2/k6" python3 -m unittest discover -s tests -v
npm ci
npm run build
npm run test:ui
```

The supported k6 release and both archive and Linux amd64 binary checksums are
pinned in `tools/`. Install it with `tools/install-k6.sh`.
