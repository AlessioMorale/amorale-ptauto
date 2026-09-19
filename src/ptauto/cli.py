"""The `ptauto` command line.

    ptauto validate  network.yaml      check the file, touching nothing
    ptauto plan      network.yaml      what would change, and why
    ptauto apply     network.yaml      make Packet Tracer match the file
    ptauto show      network.yaml      what Packet Tracer currently has
    ptauto test      network.yaml      run the pytest suite against it
    ptauto status                      is Packet Tracer reachable?
    ptauto models                      what can be placed

`plan` and `apply` read the same state and produce the same decisions; `apply`
is `plan` followed by execution, which is why running it twice on an unchanged
file does nothing the second time.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .apply import Applier, ordered_actions, wait_for_dhcp
from .catalog import list_models
from .client import PTClient
from .errors import PtAutoError
from .loader import load_spec
from .plan import Plan, Planner
from .transport import BridgeTransport


def _fail(message: str) -> None:
    errors.print(Text(str(message), style="bold red"))
    raise typer.Exit(code=1)


class PtAutoGroup(typer.core.TyperGroup):
    """Turns any ptauto error into one clean line instead of a traceback."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except PtAutoError as exc:
            _fail(str(exc))


console = Console()
errors = Console(stderr=True)

app = typer.Typer(
    cls=PtAutoGroup,
    add_completion=False,
    no_args_is_help=True,
    help="Declarative, idempotent network building and testing for Cisco Packet Tracer.",
)

SpecArgument = Annotated[
    Path, typer.Argument(help="Path to the YAML network specification", show_default=False)
]
PruneOption = Annotated[
    bool,
    typer.Option(
        "--prune/--no-prune",
        help="Also remove devices and cables that the spec does not describe",
    ),
]


def _client() -> PTClient:
    client = PTClient(BridgeTransport())
    if not client.is_connected():
        from .transport import NO_PT_MESSAGE

        _fail(NO_PT_MESSAGE)
    return client


def _load(path: Path):
    try:
        return load_spec(path)
    except PtAutoError as exc:
        _fail(str(exc))


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _print_plan(plan: Plan, verbose: bool = False) -> None:
    for warning in plan.warnings:
        console.print(f"[yellow]warning[/yellow] {warning}")

    if plan.is_empty:
        console.print("[green]Nothing to do: Packet Tracer matches the specification.[/green]")
        if verbose and plan.unchanged:
            for item in plan.unchanged:
                console.print(f"  [dim]= {item}[/dim]")
        return

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("action")
    table.add_column("why", style="dim", overflow="fold")

    for index, action in enumerate(ordered_actions(plan), start=1):
        style = "red" if action.destructive else "cyan"
        marker = "" if action.verified else " [yellow](unverified)[/yellow]"
        table.add_row(
            str(index), f"[{style}]{action.summary}[/{style}]{marker}", action.reason
        )
    console.print(table)

    destructive = plan.destructive_actions
    counts = f"{len(plan.actions)} change(s)"
    if destructive:
        counts += f", [red]{len(destructive)} destructive[/red]"
    console.print(f"\n{counts}; {len(plan.unchanged)} item(s) already in place.")
    if verbose:
        for item in plan.unchanged:
            console.print(f"  [dim]= {item}[/dim]")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


@app.command()
def validate(spec: SpecArgument) -> None:
    """Parse and cross-check a specification. Does not talk to Packet Tracer."""
    network = _load(spec)
    console.print(f"[green]{spec}[/green] is valid.")
    table = Table(box=None, pad_edge=False, show_header=True, header_style="bold")
    table.add_column("device")
    table.add_column("model")
    table.add_column("configuration", style="dim", overflow="fold")
    for name, component in network.components.items():
        settings = network.settings_for(name)
        described = []
        if settings.interfaces:
            described.append(
                ", ".join(
                    f"{port}={iface.address}"
                    for port, iface in settings.interfaces.items()
                    if iface.address
                )
            )
        if settings.dhcp and settings.dhcp.pools:
            described.append(f"DHCP pools: {', '.join(settings.dhcp.pools)}")
        if settings.extra_cli:
            described.append(f"{len(settings.extra_cli)} bare CLI line(s)")
        if settings.dhcp_client:
            described.append("DHCP client")
        elif settings.address:
            described.append(settings.address)
        if settings.services:
            services = [
                name_
                for name_, value in (("DNS", settings.services.dns), ("HTTP", settings.services.http))
                if value
            ]
            described.append("services: " + ", ".join(services))
        table.add_row(name, component.model, "; ".join(filter(None, described)))
    console.print(table)
    console.print(
        f"{len(network.components)} device(s), {len(network.connections)} cable(s)."
    )


@app.command()
def plan(
    spec: SpecArgument,
    prune: PruneOption = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Also list what already matches")] = False,
) -> None:
    """Show what `apply` would change, and why."""
    network = _load(spec)
    client = _client()
    try:
        result = Planner(client, network, prune=prune).build()
    except PtAutoError as exc:
        _fail(str(exc))
    _print_plan(result, verbose=verbose)
    raise typer.Exit(code=0 if result.is_empty else 2)


@app.command()
def apply(
    spec: SpecArgument,
    prune: PruneOption = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask before destructive changes")] = False,
    save: Annotated[Optional[str], typer.Option("--save", help="Save the workspace to this .pkt when done")] = None,
    wait_dhcp: Annotated[bool, typer.Option("--wait-dhcp/--no-wait-dhcp", help="Wait for DHCP clients to take a lease")] = True,
    keep_going: Annotated[bool, typer.Option("--keep-going", help="Continue after a failed action")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan only, change nothing")] = False,
) -> None:
    """Make Packet Tracer match the specification. Safe to run repeatedly."""
    network = _load(spec)
    client = _client()

    try:
        result = Planner(client, network, prune=prune).build()
    except PtAutoError as exc:
        _fail(str(exc))

    _print_plan(result)
    target = save or network.project.save_as

    if dry_run:
        if not result.is_empty:
            console.print("\n[dim]--dry-run: nothing was sent to Packet Tracer.[/dim]")
        raise typer.Exit(code=0)

    if result.is_empty:
        # Nothing to change is not a reason to skip an explicit --save: the
        # workspace already matches the spec, and the person asked for a .pkt.
        if target:
            path = client.save_project(str(Path(target).expanduser().resolve()))
            console.print(f"\nSaved to [green]{path}[/green]")
        raise typer.Exit(code=0)

    if result.destructive_actions and not yes:
        console.print()
        if not typer.confirm(
            f"{len(result.destructive_actions)} change(s) delete something. Continue?"
        ):
            raise typer.Exit(code=1)

    console.print()
    applier = Applier(
        client,
        on_action=lambda action: console.print(f"  [cyan]->[/cyan] {action.summary}"),
        keep_going=keep_going,
    )
    report = applier.run(result)

    for failure in report.failures:
        errors.print(f"[red]failed[/red] {failure.action.summary}\n        {failure.detail}")

    if report.ok and wait_dhcp:
        clients = [
            name
            for name in network.components
            if network.settings_for(name).dhcp_client
        ]
        if clients:
            console.print("\nWaiting for DHCP leases…")
            leases = wait_for_dhcp(
                client,
                clients,
                on_lease=lambda device, address: console.print(
                    f"  [green]{device}[/green] {address}"
                ),
            )
            for device, address in leases.items():
                if not address:
                    console.print(
                        f"  [yellow]{device}[/yellow] no lease yet, check the pool "
                        f"and the cabling"
                    )

    if report.ok and target:
        path = client.save_project(str(Path(target).expanduser().resolve()))
        console.print(f"\nSaved to [green]{path}[/green]")

    console.print(
        f"\n{len(report.applied)} change(s) applied"
        + (f", [red]{len(report.failures)} failed[/red]" if report.failures else "")
        + "."
    )
    raise typer.Exit(code=0 if report.ok else 1)


@app.command()
def show(
    spec: Annotated[Optional[Path], typer.Argument(help="Optional spec, to mark which devices belong to it")] = None,
) -> None:
    """Print what Packet Tracer currently has in its workspace."""
    client = _client()
    network = _load(spec) if spec else None
    topology = client.topology()

    table = Table(box=None, show_header=True, header_style="bold", pad_edge=False)
    table.add_column("device")
    table.add_column("model")
    table.add_column("addressed interfaces", overflow="fold")
    if network:
        table.add_column("in spec", justify="center")

    for device in topology.devices.values():
        addressed = ", ".join(
            f"{port.name}={port.ip}/{port.mask}"
            for port in device.ports.values()
            if port.has_address
        )
        row = [device.name, device.model, addressed or "[dim]none[/dim]"]
        if network:
            row.append("[green]yes[/green]" if device.name in network.components else "[dim]no[/dim]")
        table.add_row(*row)
    console.print(table)

    console.print(f"\n[bold]{len(topology.links)} cable(s)[/bold]")
    for link in topology.links:
        console.print(f"  {link.device_a}:{link.port_a} <-> {link.device_b}:{link.port_b}")


@app.command()
def destroy(
    spec: SpecArgument,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask")] = False,
) -> None:
    """Delete every device this specification describes."""
    network = _load(spec)
    client = _client()
    topology = client.topology()
    present = [name for name in network.components if name in topology.devices]
    if not present:
        console.print("Nothing to remove: none of the spec's devices are in the workspace.")
        raise typer.Exit(code=0)

    console.print(f"This deletes {len(present)} device(s): {', '.join(present)}")
    if not yes and not typer.confirm("Continue?"):
        raise typer.Exit(code=1)
    for name in present:
        client.delete_device(name)
        console.print(f"  [red]-[/red] {name}")
    console.print(f"{len(present)} device(s) removed.")


@app.command()
def test(
    spec: SpecArgument,
    path: Annotated[Optional[Path], typer.Argument(help="Test file or directory (default: tests/)")] = None,
    apply_first: Annotated[bool, typer.Option("--apply", help="Build the network before testing")] = False,
    pytest_args: Annotated[Optional[list[str]], typer.Argument(help="Extra arguments passed to pytest")] = None,
) -> None:
    """Run a pytest suite against the network described by `spec`."""
    _load(spec)  # fail early on a bad file, before pytest starts
    command = [sys.executable, "-m", "pytest", "--pt-spec", str(spec), "--pt-require"]
    if apply_first:
        command.append("--pt-apply")
    if path:
        command.append(str(path))
    command.extend(pytest_args or [])
    console.print(f"[dim]{' '.join(command)}[/dim]\n")
    raise typer.Exit(code=subprocess.call(command))


@app.command()
def status() -> None:
    """Report how ptauto can reach Packet Tracer right now."""
    state = BridgeTransport().status()
    table = Table(box=None, show_header=False, pad_edge=False)
    table.add_row("channel", state.channel or "[red]none[/red]")
    table.add_row("bridge on port", f"{state.port} ({state.bridge_identity})")
    table.add_row("PT polling", "[green]yes[/green]" if state.pt_connected else "[red]no[/red]")
    table.add_row("file bridge", "alive" if state.file_bridge_alive else "[dim]idle[/dim]")
    table.add_row("token id", state.token_id)
    if state.detail.get("last_poll_ago") is not None:
        table.add_row("last poll", f"{state.detail['last_poll_ago']}s ago")
    console.print(table)

    if state.usable:
        client = PTClient(BridgeTransport(port=state.port))
        topology = client.topology()
        console.print(
            f"\n[green]Connected[/green] over {state.channel}: "
            f"{len(topology.devices)} device(s), {len(topology.links)} cable(s) in the workspace."
        )
    else:
        from .transport import NO_PT_MESSAGE

        console.print()
        errors.print(Text(NO_PT_MESSAGE, style="yellow"))
        raise typer.Exit(code=1)


@app.command()
def models(
    search: Annotated[Optional[str], typer.Argument(help="Only show models matching this text")] = None,
) -> None:
    """List the device models a specification can use."""
    for category, names in sorted(list_models().items()):
        shown = [n for n in names if not search or search.lower() in n.lower()]
        if not shown:
            continue
        console.print(f"[bold]{category}[/bold]: {', '.join(shown)}")


@app.command()
def render(spec: SpecArgument, device: Annotated[Optional[str], typer.Argument(help="Only this device")] = None) -> None:
    """Print the IOS configuration a spec implies, without touching PT.

    Useful for a report appendix, and for reading what ptauto is about to send.
    """
    from .catalog import model_info
    from .ios import build_cli, render_blocks

    network = _load(spec)
    for name, component in network.components.items():
        if device and name != device:
            continue
        info = model_info(component.model)
        if not info.is_ios:
            continue
        blocks = render_blocks(name, network.settings_for(name), info.category)
        console.print(f"[bold]! ---- {name} ({info.pt_type}) ----[/bold]")
        console.print(build_cli(blocks))
        console.print()


if __name__ == "__main__":
    app()
