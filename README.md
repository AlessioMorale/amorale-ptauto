# ptauto — declarative networks for Cisco Packet Tracer

[![CI](https://github.com/alessiomorale/amorale-ptauto/actions/workflows/ci.yml/badge.svg)](https://github.com/alessiomorale/amorale-ptauto/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/amorale-ptauto.svg)](https://pypi.org/project/amorale-ptauto/)

Describe a network in YAML; `ptauto` builds it in a running Packet Tracer,
keeps it that way, and gives a pytest suite the fixtures to prove it works.

The package on PyPI is `amorale-ptauto`; the import stays `import ptauto`
either way — `amorale-` is only a namespacing prefix on the distribution name.
The CLI answers to either name, so no install is needed to try it:

```bash
uvx amorale-ptauto status     # is Packet Tracer reachable right now?
uvx amorale-ptauto models     # what device models can a spec use?
```

To follow along with the example below, clone this repository first — it
needs the spec and test files in it:

```bash
git clone https://github.com/alessiomorale/amorale-ptauto
cd amorale-ptauto

uvx amorale-ptauto validate examples/two-site-guest-wifi.yaml     # check the file
uvx amorale-ptauto apply    examples/two-site-guest-wifi.yaml     # build it in Packet Tracer
uvx amorale-ptauto apply    examples/two-site-guest-wifi.yaml     # ...and again: nothing happens
uvx amorale-ptauto test     examples/two-site-guest-wifi.yaml tests_network/two_site/
```

`uvx amorale-ptauto ...` re-resolves the environment each call (uv caches it,
so repeat calls are fast); for a persistent `ptauto` on your PATH instead:

```bash
pip install amorale-ptauto        # or: uv tool install amorale-ptauto
ptauto validate examples/two-site-guest-wifi.yaml
```

Working on ptauto itself from a clone, rather than the published package:

```bash
uv sync
uv run ptauto validate examples/two-site-guest-wifi.yaml
```

Running `apply` a second time reports *"Nothing to do — Packet Tracer matches
the specification"* and sends nothing over the bridge. That is the point of the
tool: the YAML file is the network, and any drift — an address changed in the
GUI, an interface shut, a DNS record deleted — shows up as a named difference on
the next run.

## Requirements

* Python 3.12+ and [uv](https://docs.astral.sh/uv/)
* Cisco Packet Tracer 8.x/9.x, open, with the **MCP Control Center** extension
  loaded (*Extensions > MCP BUILDER*) — the same extension the
  [MCP-Packet-Tracer](https://github.com/Mats2208/MCP-Packet-Tracer) project
  installs

`ptauto status` says whether it can see Packet Tracer and how.

## How it talks to Packet Tracer

ptauto reuses `packet-tracer-mcp`'s connectivity rather than inventing its own:
the local HTTP command bridge the extension's webview polls, its shared-token
authentication, and the file mailbox the PT Script Engine drains when the
extension window is closed. `ptauto.transport` is the client side of those two
channels; one command travels over exactly one of them, never both.

The device catalog (models, their real port names, cabling rules) is imported
from the same project, so a model that PT accepts is a model ptauto accepts.

## The specification

Three sections — components, connections, configurations — as in
[`examples/two-site-guest-wifi.yaml`](examples/two-site-guest-wifi.yaml):

```yaml
version: 1

components:                      # what exists
  Router-A:  {model: 2911, position: [280, 120]}
  SW-A:      {model: 2960-24TT, position: [160, 280]}
  PC-A1:     {model: PC-PT, position: [80, 420]}

connections:                     # how it is cabled
  - [Router-A:g0/0, SW-A:g0/1]
  - [SW-A:fa0/1, PC-A1:fa0]

configurations:                  # what it is configured with
  Router-A:
    interfaces:
      GigabitEthernet0/0:
        address: 192.168.1.1/27
        description: Site A LAN
    dhcp:
      excluded: [192.168.1.1]
      pools:
        SITE_A:
          network: 192.168.1.0/27
          default_router: 192.168.1.1
  PC-A1:
    dhcp_client: true
```

Notes on the schema:

* **Ports may be abbreviated.** `g0/0`, `Gi0/0` and `GigabitEthernet0/0` are the
  same port; connections are direction-independent.
* **Cable types are inferred** from the two device categories, and can be
  overridden per connection with `cable:`.
* **`components` can carry its own `config:` block**, in which case
  `configurations` is optional. Where both describe a device they are merged,
  and `configurations` wins.
* **Unknown keys are errors.** A mistyped setting fails the file rather than
  doing nothing quietly.

### What a device can be given

| section     | applies to                      | keys                                                                                                                                   |
| ----------- | ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| IOS devices | routers, switches               | `hostname`, `interfaces`, `vlans`, `dhcp`, `static_routes`, `default_gateway`, `domain_lookup`, `banner`, `enable_secret`, `extra_cli` |
| hosts       | PCs, servers, printers, laptops | `dhcp_client`, `address`, `gateway`, `dns`                                                                                             |
| servers     | Server-PT only                  | `services.dns` (records), `services.http` (pages, port)                                                                                |

Interfaces take `address`, `description`, `shutdown`, and for switch ports
`mode: access|trunk`, `vlan:`, `trunk_vlans:`, plus `extra:` for anything the
schema does not model.

### Bare CLI, for anything the schema does not model

`extra_cli` on a router or switch takes raw IOS commands, applied at
global-config scope after everything else. Write it as a `|` block exactly the
way `show running-config` would print it — indentation is what tells ptauto a
line is a submode's child rather than a new global command:

```yaml
configurations:
  Router-A:
    extra_cli: |
      line vty 0 4
       login
       transport input telnet
      service timestamps log datetime msec
```

ptauto groups these the same way IOS does — one block per top-level line, with
its indented children checked against *that* line's section — and enters/exits
the submode itself, so `line vty 0 4` does not need a trailing `exit`. Each line
is still verified literally, so a value IOS rewrites on the way in (a plaintext
`password` once `service password-encryption` is on, the way `enable_secret` is
always stored hashed) will show as pending on every run rather than being
reported as converged when it is not; put the one credential ptauto does
understand in `enable_secret` instead of `extra_cli` for that reason.

`ptauto validate` checks all of it before anything is sent to PT: unknown
models, ports a model does not have, a port cabled twice, duplicate addresses, a
host whose gateway is outside its own subnet, a DHCP pool pointing at a
default-router no device owns, host settings on a router, IOS settings on a PC.

## Commands

| command                   | what it does                                                                                                          |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `ptauto validate SPEC`    | parse and cross-check the file; never touches PT                                                                      |
| `ptauto plan SPEC`        | what `apply` would change, and why (exit 2 if anything would)                                                         |
| `ptauto apply SPEC`       | make PT match the file; `--prune` also removes what the file does not describe, `--save FILE.pkt` saves the workspace |
| `ptauto show [SPEC]`      | what PT currently has                                                                                                 |
| `ptauto render SPEC`      | the IOS configuration the file implies, without touching PT                                                           |
| `ptauto destroy SPEC`     | remove the devices the file describes                                                                                 |
| `ptauto test SPEC [PATH]` | run a pytest suite against the live network                                                                           |
| `ptauto status`           | how ptauto can reach PT right now                                                                                     |
| `ptauto models [TEXT]`    | the device models a file can use                                                                                      |

## How idempotency is decided

Nothing is re-applied blindly. Before each run ptauto reads what PT has and
compares it with the specification:

| part of the spec   | what is compared against                                                   |
| ------------------ | -------------------------------------------------------------------------- |
| devices, positions | the workspace's device list and centre coordinates                         |
| cables             | the link list, by endpoint pair, direction-independent                     |
| IOS configuration  | the device's own configuration text, block by block                        |
| host addressing    | the port's IP/mask, the DHCP flag, and the gateway/DNS in the device's XML |
| server services    | the DNS record database and the HTTP service's state                       |

For IOS devices each piece of configuration is a block with both the commands
that apply it and the evidence that proves it is already applied — so `no
shutdown`, which never appears in a configuration, is checked as *the absence of
`shutdown`* rather than as a line to look for. ptauto reads the device's saved
configuration, issuing a `write memory` first so that what it reads is what the
device is actually running, including changes someone made by hand in the GUI.

When something genuinely cannot be verified, the plan says `(unverified)`
instead of claiming the change was needed.

## Testing a network

The pytest fixtures ship with the package — no `conftest.py` required:

```python
def test_the_two_sites_can_reach_each_other(pt_network):
    assert pt_network.ping("PC-A1", "PC-B1").ok

def test_nothing_has_drifted(pt_network):
    assert pt_network.plan().is_empty
```

```bash
pytest --pt-spec examples/two-site-guest-wifi.yaml tests_network/two_site/
pytest --pt-spec examples/two-site-guest-wifi.yaml --pt-apply tests_network/two_site/   # build first
```

| fixture      | what it gives                                                                                          |
| ------------ | ------------------------------------------------------------------------------------------------------ |
| `pt_network` | the network under test: `ping`, `ping_hostname`, `host`, `interface`, `services`, `plan`, `address_of` |
| `pt_client`  | the raw `PTClient` for anything the facade does not cover                                              |
| `pt_spec`    | the parsed specification                                                                               |
| `pt_ping`    | `pt_ping("A", "B")` — asserts, with a readable failure                                                 |

Options: `--pt-spec PATH`, `--pt-apply` (build before testing), `--pt-require`
(fail instead of skip when PT is not running). Without `--pt-require` a suite
skips when Packet Tracer is closed, so it stays runnable in CI.

`pt_network.ping` repeats a partial result once: the first packet between two
hosts is always lost to ARP resolution in Packet Tracer, and that is a warm-up
artefact rather than a fault. `pt_network.ping(..., retry_partial=False)` shows
the raw first attempt.

## Using it as a library

```python
from ptauto import load_spec, PTClient, Planner, Applier

spec = load_spec("network.yaml")
client = PTClient()

plan = Planner(client, spec).build()
for action in plan.actions:
    print(action.summary, "—", action.reason)

report = Applier(client).run(plan)
print(report.ok, len(report.applied))

print(client.ping("PC-Admin1", "192.168.30.2").verdict)
```

## Layout

```
src/ptauto/
  transport.py   the two channels into Packet Tracer (reused from packet-tracer-mcp)
  client.py      typed operations: topology, devices, links, IOS, hosts, services, ping
  model.py       the YAML schema
  loader.py      parsing, merging and cross-validation
  ios.py         IOS rendering, and the evidence that proves it is applied
  plan.py        the diff engine
  apply.py       execution, in an order the network can survive
  testing.py     the facade a test suite talks to
  pytest_plugin.py  the fixtures, registered as a pytest plugin
  cli.py         the `ptauto` command
examples/        a worked specification
tests/           unit tests — no Packet Tracer needed
tests_network/   acceptance tests — run against a live Packet Tracer
```

Run the unit tests with `uv run pytest`; they use a Packet Tracer stand-in and
do not need the simulator.

## Continuous integration and releasing

`.github/workflows/ci.yml` runs on every push and pull request: the offline
unit suite (`tests/`) on Python 3.12 and 3.13, `ptauto validate` against the
example spec, and a packaging check (`uv build` + `twine check --strict`).
`tests_network/` — the acceptance suite — is deliberately not run there: it
needs a real, GUI Packet Tracer instance with the MCP Control Center extension
open, which no hosted runner can provide.

`.github/workflows/publish.yml` builds and publishes to PyPI whenever a GitHub
Release is published, using [trusted publishing][trusted-publishing] — OIDC,
not a stored API token, so there is no secret in this repository to rotate or
leak. That needs a one-time link on PyPI's side before the first release:

1. Push this repository to GitHub and publish the first release manually, or
   [create the PyPI project first][first-release] some other way, so
   `amorale-ptauto` exists on PyPI to attach a publisher to.
2. On [pypi.org][pypi-publishing], under the project's *Publishing* settings,
   add a trusted publisher with:
   - Owner: `alessiomorale` (or whatever this repo ends up under)
   - Repository name: `amorale-ptauto`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
3. From then on, publishing a GitHub Release (with a version-matching tag,
   e.g. `v0.2.0` after bumping `version` in `pyproject.toml`) builds and
   uploads automatically — no further action needed on PyPI's side.

[trusted-publishing]: https://docs.pypi.org/trusted-publishers/
[pypi-publishing]: https://pypi.org/manage/account/publishing/
[first-release]: https://docs.pypi.org/trusted-publishers/adding-a-publisher/#adding-a-pending-trusted-publisher-for-pypi

`.github/dependabot.yml` keeps both the Python dependencies and the workflow
actions themselves on a weekly update check.

## Known limits

* Packet Tracer must be open with the extension loaded; there is no headless mode.
* An error inside PT's Script Engine opens a modal dialog that freezes the
  bridge until it is dismissed. ptauto guards every command it sends, so it does
  not cause one, but a dialog opened by something else will make ptauto time
  out — `ptauto status` will say so.
* `--prune` never removes PT's own infrastructure objects (the power
  distribution device), and by default a model mismatch is reported rather than
  replaced.
