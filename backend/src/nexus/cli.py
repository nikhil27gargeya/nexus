from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console
from rich.panel import Panel

from nexus import pipeline
from nexus.apps.fixtures import iter_scenarios
from nexus.config import settings
from nexus.models import NexusEvent

if TYPE_CHECKING:
    from nexus.seed.plan import SeedPlan

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Nexus: prove it, or admit nothing.")
console = Console()
MARKS = {"pass": "[green]✓[/]", "fail": "[red]✗[/]", "skipped": "[dim]—[/]"}


def _args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={str(v)[:24]}" for k, v in args.items())


def render(event: NexusEvent) -> None:
    d = event.data
    match event.type:
        case "run.started":
            a = d["account"]
            console.rule(f"[bold]{a['name']}[/] · {a['user_id']} · {a['region']} · model {d['model']} · {d['data']}")
        case "thought":
            console.print(f"   [dim italic]{d['text'][:160]}[/]")
        case "step":
            status = "" if d["ok"] else " [red](error)[/]"
            console.print(f"[dim]{d['n']:>3}[/] [cyan]{d['tool']}[/]({_args(d['args'])}) → {d['summary']}{status}")
        case "rejected":
            console.print(f"    [yellow]rejected:[/] {d['reason']}")
        case "verdict":
            console.print(f"\n[bold]verdict[/] {d['claim']} · confidence {d['confidence']:.2f}")
            console.print(f"   [dim]{d['reasoning']}[/]")
        case "gate":
            console.print(f"  {MARKS[d['status']]} {d['name']:<28} [dim]{d['reason']}[/]")
        case "decision":
            if d["remediate"]:
                console.print("\n[bold green]Proven: our failure caused this. Nexus remediates.[/]")
            elif d["claim"] == "no_harm":
                console.print("\n[bold cyan]No proof of harm. No admission of fault; standard offer only.[/]")
            else:
                console.print(f"\n[bold cyan]Harm found, but not proven to have caused this (stopped at gate "
                              f"{d['stopped_at']}). Standard offer only.[/]")
        case "remediation":
            body = "\n".join([d["letter"], "", *[f"• {p['text']}" for p in d["proof"]],
                              "", d["fixed_line"] or "", d["credit_line"],
                              "", f"[{d['accept_label']}]  [{d['decline_label']}]"])
            console.print(Panel(body, title=d["headline"], border_style="green"))
        case "run.error" | "agent.error":
            console.print(f"[red]error:[/] {d['message']}")
        case "run.done":
            offer = d.get("offer")
            if offer:
                body = f"{offer['body']}\n\n[{offer['accept_label']}]  [{offer['decline_label']}]"
                console.print(Panel(body, title=offer["headline"], border_style="cyan"))
                console.print(f"[dim]blocked naive message: [strike]{d['blocked_naive_message']}[/strike][/]")
            console.print(f"[dim]outcome={d['outcome']}[/]")


@app.command()
def run(
    scenario: str = typer.Argument(help="Scenario id, e.g. fathom"),
    fake: bool = typer.Option(False, "--fake", help="Scripted agent, no API key needed"),
    live: bool = typer.Option(False, "--live", help="Read Sentry, Datadog, Mixpanel and Stripe instead of fixtures"),
    as_json: bool = typer.Option(False, "--json", help="Print raw events as JSON lines"),
) -> None:
    """Run Nexus on one cancellation."""
    for event in pipeline.run(scenario, fake=fake, live=live):
        if as_json:
            print(event.model_dump_json())
        else:
            render(event)


def render_seed_plan(plan: "SeedPlan", chosen: set[str], send: bool) -> None:
    from nexus.seed.plan import sentry_events, sentry_fix_releases

    console.rule(f"Seed plan · cancel date {plan.cancel} · {'[red]SENDING[/]' if send else 'dry run'}")
    if "sentry" in chosen:
        total = sum(len(sentry_events(f)) for f in plan.fixtures)
        console.print(f"[bold]Sentry[/] {settings.sentry_org}/{settings.sentry_project}: {total} error events")
        for f in plan.fixtures:
            for issue in f["sentry"]["issues"]:
                console.print(f"   {f['account']['name']:<15} {issue['type']} ×{issue['user_count']} "
                              f"on {next(iter(issue['daily']))}")
            for rel in sentry_fix_releases(f):
                console.print(f"   release {rel['version']} dated {rel['date_released'][:10]} "
                              f"resolves {rel['issue_type']}")
    if "datadog" in chosen:
        for inc in plan.incidents:
            console.print(f"[bold]Datadog[/] incident '{inc['title']} ({inc['region']})' "
                          f"{inc['start']} → {inc['end'][11:]}, resolved")
    if "mixpanel" in chosen:
        console.print(f"[bold]Mixpanel[/] project {settings.mixpanel_project_id}: {len(plan.mixpanel_events)} events "
                      "(sign_up_completed, feature_used)")
        for f in plan.fixtures:
            mix = ", ".join(f"{k} {round(v * 100)}%" for k, v in f["mixpanel"]["feature_share"].items())
            console.print(f"   {f['account']['name']:<15} {mix} · active {f['mixpanel']['active_members']}")
    if "stripe" in chosen:
        console.print("[bold]Stripe[/] test mode: product Mergeline Pro ($30/mo), coupon 20% off × 3 months, "
                      + ", ".join(f["account"]["name"] for f in plan.fixtures) + " with Pro subscriptions")


@app.command()
def seed(
    only: str = typer.Option("all", "--app", help="sentry | datadog | mixpanel | stripe | all"),
    send: bool = typer.Option(False, "--send", help="Write to the live apps. Without it, only prints the plan."),
) -> None:
    """Seed the demo customers into Sentry, Datadog, Mixpanel and Stripe, dated relative to today."""
    from nexus.seed.plan import build_plan

    chosen = {"sentry", "datadog", "mixpanel", "stripe"} if only == "all" else {only}
    plan = build_plan(settings.fixtures_dir, datetime.now(UTC).date())
    render_seed_plan(plan, chosen, send)
    if not send:
        console.print("\n[dim]Dry run: nothing was sent. Add --send to write to the live apps.[/]")
        return

    from nexus.seed.send import send_plan

    for progress in send_plan(plan, settings, chosen):
        if progress.ok is None:
            console.print(f"[bold]{progress.app.title()}[/]")
        elif progress.ok:
            console.print(f"   [green]✓[/] {progress.message}")
        else:
            console.print(f"   [red]✗ {progress.app}:[/] {progress.message}")


@app.command("eval")
def eval_command(
    runs: int = typer.Option(1, help="Runs per scenario"),
    fake: bool = typer.Option(False, "--fake", help="Scripted agent, no API key needed"),
) -> None:
    """Run every fixture scenario and grade outcomes; writes evals/results/latest.md."""
    from nexus.evals import run_evals, write_report

    rows = run_evals(runs=runs, fake=fake)
    report = write_report(rows, "fake" if fake else settings.agent_model)
    for r in rows:
        mark = "[green]✓[/]" if r.correct else "[red]✗[/]"
        console.print(f"{mark} {r.scenario:<16} expected {r.expected_outcome:<15} got {r.outcome:<15} "
                      f"claim {r.claim} {r.confidence} · {r.steps} steps · {r.seconds}s")
    false_remediations = sum(r.false_remediation for r in rows)
    console.print(f"[bold]{sum(r.correct for r in rows)}/{len(rows)} correct · {false_remediations} false "
                  f"remediations[/] → {report}")
    if false_remediations:
        raise typer.Exit(1)


@app.command()
def verify(recording: str = typer.Argument(help="Path to a recorded run (.jsonl)")) -> None:
    """Re-read every evidence ID a recorded run cited, directly from the live apps."""
    from pathlib import Path

    from nexus.verify import verify_recording

    lines, ok = verify_recording(Path(recording))
    for line in lines:
        console.print(line)
    if not ok:
        raise typer.Exit(1)


@app.command("stripe-reset")
def stripe_reset() -> None:
    """Undo demo clicks in Stripe test mode: reverse Nexus credits, remove discounts and scheduled cancellations."""
    from nexus.apps.stripe_actions import StripeActions

    actions = StripeActions(settings)
    for _, fixture in iter_scenarios(settings.fixtures_dir):
        account = fixture["account"]
        console.print(f"{account['name']:<16} " + ", ".join(actions.reset(account["user_id"])))


@app.command()
def serve(port: int = 8765) -> None:
    """Serve the demo page and the streaming API on this machine only (http://127.0.0.1:8765). The API has no auth."""
    import uvicorn

    uvicorn.run("nexus.api.main:app", host="127.0.0.1", port=port)


@app.command()
def scenarios() -> None:
    """List available scenarios."""
    for scenario, data in iter_scenarios(settings.fixtures_dir):
        console.print(f"{scenario:<22} expected {data.get('expected', '?')}")
