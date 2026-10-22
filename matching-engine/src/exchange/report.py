"""Render the metrics dict produced by ``run.build`` into REPORT.md and site.json.

Pure functions of the metrics: no I/O, no computation beyond formatting, so the rendered
files are fully determined by ``metrics.json``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

Metrics = dict[str, Any]

NS_PER_US: Final = 1_000


def fmt_usd(x: float) -> str:
    """``$1,234.50`` / ``-$1,234.50``."""
    # The sign goes before the dollar sign: -$1.00, not $-1.00.
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def _us(ns: float) -> str:
    """Nanoseconds to microseconds, formatted with one decimal place."""
    return f"{ns / NS_PER_US:.1f}"


def _counts(counts: dict[str, int]) -> str:
    """Join a count mapping as ``name 1,234, name 5``, keeping its order."""
    return ", ".join(f"{k} {v:,}" for k, v in counts.items())


def _benchmark_section(m: Metrics) -> list[str]:
    """Markdown lines for the engine benchmark: throughput, latency, depth and replay check."""
    b = m["benchmark"]
    lat = b["latency_ns"]
    d = b["depth"]
    return [
        "## Engine benchmark",
        "",
        f"Deterministic synthetic flow of {b['orders']:,} commands (seed {b['seed']}).",
        "",
        "| metric | value |",
        "|---|---|",
        f"| throughput (best of {len(b['throughput_runs'])}) | "
        f"{b['throughput_orders_per_sec']:,} orders/s |",
        f"| latency p50 | {_us(lat['p50'])} µs |",
        f"| latency p99 | {_us(lat['p99'])} µs |",
        f"| latency p99.9 | {_us(lat['p99_9'])} µs |",
        f"| latency max | {_us(lat['max'])} µs |",
        f"| events per command | {b['events_per_command']} |",
        f"| mean book depth (levels bid / ask) | {d['mean_bid_levels']} / {d['mean_ask_levels']} |",
        f"| mean / max resting orders | {d['mean_resting_orders']} / {d['max_resting_orders']} |",
        f"| journal replay reproduces book hash | {b['replay']['hash_match']} |",
        "",
        f"Command mix: {_counts(b['command_mix'])}.",
        "",
        f"Events emitted: {_counts(b['events'])}.",
        "",
    ]


def _backtest_section(m: Metrics) -> list[str]:
    """Markdown lines comparing both strategies: PnL decomposition, risk, fills, inventory."""
    bt = m["backtest"]
    mm, mo = bt["market_maker"], bt["momentum"]
    cfg = bt["config"]
    lines = [
        f"## Backtest (seed {cfg['seed']}, {cfg['steps']:,} steps = {mm['days']} sim days)",
        "",
        f"Fees: taker ${cfg['taker_fee_usd_per_share']:.4f}/sh, maker rebate "
        f"${cfg['maker_rebate_usd_per_share']:.4f}/sh. Strategy orders arrive "
        f"{cfg['strategy_latency_steps']} step after the decision, behind that step's "
        "background flow.",
        "",
        "| | market maker | momentum |",
        "|---|---|---|",
    ]

    def row(label: str, a: object, c: object) -> None:
        """Append one markdown table row with a value per strategy."""
        lines.append(f"| {label} | {a} | {c} |")

    def usd_row(label: str, pick: Callable[[Metrics], float]) -> None:
        """Append one markdown table row with a dollar value per strategy."""
        row(label, fmt_usd(pick(mm)), fmt_usd(pick(mo)))

    # Net first, then the costs-ignored view and the two components that bridge it to net.
    usd_row("**net PnL**", lambda s: s["net_pnl_usd"])
    usd_row("PnL with costs ignored (fills at mid, no fees)", lambda s: s["costs_ignored_pnl_usd"])
    usd_row(
        "  spread capture / slippage",
        lambda s: s["pnl_decomposition_usd"]["spread_and_slippage"],
    )
    usd_row("  fees & rebates", lambda s: s["pnl_decomposition_usd"]["fees_and_rebates"])
    row("Sharpe (net, annualised daily)", mm["sharpe_net"], mo["sharpe_net"])
    row("Sharpe (costs ignored)", mm["sharpe_costs_ignored"], mo["sharpe_costs_ignored"])
    usd_row("max drawdown (net)", lambda s: s["max_drawdown_usd"])
    row(
        "fills / filled shares",
        f"{mm['fills']:,} / {mm['filled_shares']:,}",
        f"{mo['fills']:,} / {mo['filled_shares']:,}",
    )
    row("fill ratio (filled / offered shares)", mm["fill_ratio"], mo["fill_ratio"])
    row("maker share of volume", mm["maker_share"], mo["maker_share"])
    row(
        "inventory mean abs / max abs / final",
        "{mean_abs} / {max_abs} / {final}".format(**mm["inventory"]),
        "{mean_abs} / {max_abs} / {final}".format(**mo["inventory"]),
    )
    # Queue position exists only for resting orders, so momentum (IOC only) has no value.
    row("avg shares queued ahead when booked", mm["avg_queue_ahead_shares"], "n/a (IOC only)")
    lines += ["", f"Regime mix (steps): {_counts(mm['regime_steps'])}.", ""]
    return lines


def _robustness_section(m: Metrics) -> list[str]:
    """Markdown lines showing sign and Sharpe stability across the robustness seeds."""
    rob = m["seed_robustness"]
    # Every strategy ran the same seeds, so the first strategy's rows give the header list.
    seeds = [r["seed"] for r in next(iter(rob.values()))["rows"]]
    lines = [
        f"## Seed robustness ({len(seeds)} seeds: {', '.join(map(str, seeds))})",
        "",
        "| strategy | net PnL > 0 | costs-ignored PnL > 0 | mean Sharpe net "
        "| mean Sharpe costs ignored |",
        "|---|---|---|---|---|",
    ]
    for name, s in rob.items():
        lines.append(
            f"| {name} | {s['net_positive_seeds']}/{s['seeds']} | "
            f"{s['costs_ignored_positive_seeds']}/{s['seeds']} | {s['mean_sharpe_net']} | "
            f"{s['mean_sharpe_costs_ignored']} |"
        )
    lines += [
        "",
        "| strategy | seed | net PnL | costs-ignored PnL | Sharpe net | Sharpe costs ignored |",
        "|---|---|---|---|---|---|",
    ]
    for name, s in rob.items():
        for r in s["rows"]:
            lines.append(
                f"| {name} | {r['seed']} | {fmt_usd(r['net_pnl_usd'])} | "
                f"{fmt_usd(r['costs_ignored_pnl_usd'])} | {r['sharpe_net']} | "
                f"{r['sharpe_costs_ignored']} |"
            )
    return lines


def report_md(m: Metrics) -> str:
    """The human-readable results report (REPORT.md)."""
    env = m["environment"]
    runtime = m["runtime_seconds"]
    lines = [
        "# Matching engine + backtester: results",
        "",
        f"Generated by `python -m exchange.run` (Python {env['python']}, "
        f"{env['platform']}). All numbers below come from that run.",
        "",
        *_benchmark_section(m),
        *_backtest_section(m),
        *_robustness_section(m),
        "",
        f"Total run time: {runtime['total']} s (benchmark {runtime['benchmark']} s).",
        "",
    ]
    return "\n".join(lines)


def site_json(m: Metrics) -> dict[str, Any]:
    """The portfolio-site card for this project (site.json)."""
    b = m["benchmark"]
    lat = b["latency_ns"]
    mm = m["backtest"]["market_maker"]
    mo = m["backtest"]["momentum"]
    rob_mo = m["seed_robustness"]["momentum"]
    # Only the net curve is charted here; the costs-ignored curve stays in metrics.json.
    s = m["series"]["momentum"]
    return {
        "slug": "exchange",
        "title": "Limit order book matching engine + event-driven backtester",
        "tagline": (
            "A price-time priority matching engine with sequenced events and deterministic "
            "replay, and a backtester whose strategies trade through it with real queue "
            "position, fees and slippage."
        ),
        "lane": "Trading & market infrastructure",
        "stack": ["Python", "Hypothesis", "pytest"],
        "metrics": [
            {
                "label": "Engine throughput",
                "value": f"{b['throughput_orders_per_sec']:,}",
                "unit": "orders/s",
                "note": f"pure Python, single thread, {b['orders']:,}-command synthetic flow",
            },
            {
                "label": "p99 per-order latency",
                "value": _us(lat["p99"]),
                "unit": "µs",
                "note": f"p50 {_us(lat['p50'])} µs, p99.9 {_us(lat['p99_9'])} µs",
            },
            {
                "label": "Replay determinism",
                "value": "identical" if b["replay"]["hash_match"] else "MISMATCH",
                "unit": "",
                "note": "journal replay reproduces the book hash; property-tested",
            },
            {
                "label": "Momentum Sharpe: costs ignored vs net",
                "value": f"{mo['sharpe_costs_ignored']} → {mo['sharpe_net']}",
                "unit": "",
                "note": f"net PnL positive on {rob_mo['net_positive_seeds']}/{rob_mo['seeds']} "
                f"seeds vs {rob_mo['costs_ignored_positive_seeds']}/{rob_mo['seeds']} "
                "with costs ignored",
            },
            {
                "label": "Market maker PnL: at mid vs net",
                "value": f"{fmt_usd(mm['costs_ignored_pnl_usd'])} → {fmt_usd(mm['net_pnl_usd'])}",
                "unit": "",
                "note": "adverse selection at mid; spread capture + rebates pay for it",
            },
        ],
        "highlights": [
            "Limit, market, IOC, FOK, post-only, cancel and cancel/replace with correct "
            "priority rules (size-down keeps priority; size-up or reprice loses it)",
            "Self-trade prevention (cancel newest), gap-free sequence numbers on every "
            "ack, fill, cancel, reject and L2 increment",
            "Hypothesis property tests: never crossed, quantity conserved, price-time "
            "priority, replay and event-sourced rebuild produce identical book hashes",
            "Backtester trades through the real engine: queue position, latency, fees, "
            "slippage, and an exact integer PnL decomposition (mid / spread / fees)",
        ],
        "limitations": [
            f"Pure Python: {b['throughput_orders_per_sec']:,} orders/s and tens of µs tail "
            "latency on a laptop, not the millions/s and sub-µs of a C++/Rust engine on a "
            "pinned core",
            "Synthetic market: background participants are stylised, so strategy PnL "
            "shows mechanics and cost sensitivity, not tradeable alpha",
            "Single instrument, no auctions, halts, or iceberg/hidden orders",
        ],
        "series": {
            "name": "Momentum strategy equity (USD): net of costs",
            "x": s["step"],
            "y": s["net_equity_usd"],
        },
    }
