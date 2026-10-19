"""Causal, read-only replay for the trversion4 trade-management audit.

The script never talks to an account or order endpoint.  It reads the exported
TradeJournal and, when available, public Binance Demo 1m klines.  CURRENT is
the exchange-observed result stored by the journal.  V2 keeps the same entry,
initial stop and final target and changes only the post-entry trailing policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd


DEMO_PUBLIC = "https://demo-fapi.binance.com"
ROUND_TRIP_COST_PCT = 0.0012
ACTIVATIONS = (1.0, 1.5, 2.0)
POST_EXIT_MINUTES = (15, 30, 60, 120)


def _read_json(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _utc(value):
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _public_json(path: str, params: dict):
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{DEMO_PUBLIC}{path}?{query}",
        headers={"User-Agent": "trversion4-read-only-replay/1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _exchange_ticks():
    payload = _public_json("/fapi/v1/exchangeInfo", {})
    ticks = {}
    for item in payload.get("symbols", []):
        for rule in item.get("filters", []):
            if rule.get("filterType") == "PRICE_FILTER":
                ticks[item["symbol"]] = float(rule.get("tickSize") or 0.0)
                break
    return ticks


def _fetch_klines(symbol: str, start: pd.Timestamp, end: pd.Timestamp, cache: Path):
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{symbol}_{int(start.timestamp())}_{int(end.timestamp())}_1m.csv"
    if target.exists():
        frame = pd.read_csv(target)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        return frame, "cache"

    rows, cursor, end_ms = [], int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    while cursor < end_ms:
        batch = _public_json(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": "1m", "startTime": cursor,
             "endTime": end_ms, "limit": 1500},
        )
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 60_000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
    if not rows:
        raise RuntimeError("NO_PUBLIC_KLINES")
    frame = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ])
    frame["timestamp"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[["timestamp", "open", "high", "low", "close", "volume"]]
    frame = frame.drop_duplicates("timestamp").sort_values("timestamp")
    frame.to_csv(target, index=False)
    return frame, "binance_demo_public"


def _local_klines(directory: Path):
    frame = pd.read_csv(directory / "trade_window_1m.csv")
    frame["timestamp"] = pd.to_datetime(frame["utc_time"], utc=True)
    return frame[["timestamp", "open", "high", "low", "close", "volume"]], "journal_only"


def _fifteen_minute(frame: pd.DataFrame):
    indexed = frame.set_index("timestamp")
    bars = indexed.resample("15min", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna().reset_index()
    return bars


def _atr(closed: pd.DataFrame):
    if len(closed) < 14:
        return None
    previous = closed["close"].shift(1)
    tr = pd.concat([
        closed["high"] - closed["low"],
        (closed["high"] - previous).abs(),
        (closed["low"] - previous).abs(),
    ], axis=1).max(axis=1)
    value = float(tr.rolling(14).mean().iloc[-1])
    return value if math.isfinite(value) and value > 0 else None


def _confirmed_swing(closed: pd.DataFrame, side: str, left=3, right=3):
    values = closed["low" if side == "BUY" else "high"].to_list()
    if len(values) < left + right + 5:
        return None
    found = []
    for index in range(left, len(values) - right):
        center = values[index]
        before, after = values[index-left:index], values[index+1:index+1+right]
        if side == "BUY" and center < min(before) and center < min(after):
            found.append(center)
        if side == "SELL" and center > max(before) and center > max(after):
            found.append(center)
    return float(found[-1]) if found else None


def _normalize_tick(value: float, tick: float):
    return round(value / tick) * tick if tick > 0 else value


def _directional_r(price: float, entry: float, risk: float, side: str):
    return ((price - entry) if side == "BUY" else (entry - price)) / risk


def _simulate_v2(frame, summary, tick, activation):
    meta, exit_details = summary["entry_meta"], summary["exit_details"]
    side = str(meta["side"]).upper()
    entry, initial_sl = float(meta["entry_price"]), float(meta["initial_sl"])
    target = float(meta.get("tp") or 0.0)
    risk = abs(entry - initial_sl)
    entry_at = _utc(meta["entry_time"])
    actual_exit_at = _utc(exit_details["exit_time"])
    horizon = actual_exit_at + pd.Timedelta(minutes=120)
    rows = frame[(frame.timestamp >= entry_at.floor("min")) & (frame.timestamp < horizon)].copy()
    bars15 = _fifteen_minute(frame)
    stop, best = initial_sl, entry
    changes, exit_price, exit_at, reason = [], None, None, "OPEN_AT_DATA_END"

    for row in rows.itertuples(index=False):
        stop_hit = row.low <= stop if side == "BUY" else row.high >= stop
        target_hit = bool(target) and (row.high >= target if side == "BUY" else row.low <= target)
        # Conservative ambiguity: protection is assumed to trigger first.
        if stop_hit:
            exit_price, exit_at, reason = stop, row.timestamp, "STRUCTURAL_TRAIL_OR_INITIAL_SL"
            break
        if target_hit:
            exit_price, exit_at, reason = target, row.timestamp, "FINAL_TARGET"
            break

        best = max(best, float(row.high)) if side == "BUY" else min(best, float(row.low))
        best_r = _directional_r(best, entry, risk, side)
        if best_r < activation:
            continue
        minute_close = row.timestamp + pd.Timedelta(minutes=1)
        closed = bars15[(bars15.timestamp + pd.Timedelta(minutes=15)) <= minute_close].tail(50)
        atr, swing = _atr(closed), _confirmed_swing(closed, side)
        if not atr or swing is None:
            continue
        candidate = swing - 0.30 * atr if side == "BUY" else swing + 0.30 * atr
        candidate = _normalize_tick(candidate, tick)
        profitable = candidate >= entry if side == "BUY" else candidate <= entry
        improves = candidate > stop if side == "BUY" else candidate < stop
        hysteresis = max(2.0 * tick, 0.15 * atr, 0.10 * risk)
        if not profitable or not improves or abs(candidate - stop) < hysteresis:
            continue
        # The replacement becomes active only after this 1m observation.
        changes.append({
            "at": minute_close.isoformat(), "old_sl": stop, "new_sl": candidate,
            "best_r": best_r, "confirmed_swing": swing, "atr": atr,
        })
        stop = candidate

    if exit_price is None:
        if rows.empty:
            return {"status": "INSUFFICIENT_MARKET_DATA", "activation_r": activation}
        exit_price, exit_at = float(rows.iloc[-1].close), rows.iloc[-1].timestamp
    gross_r = _directional_r(exit_price, entry, risk, side)
    cost_r = entry * ROUND_TRIP_COST_PCT / risk
    return {
        "status": reason, "activation_r": activation, "entry": entry,
        "initial_sl": initial_sl, "final_target": target, "exit": exit_price,
        "exit_at": exit_at.isoformat(), "gross_r": gross_r,
        "estimated_net_r": gross_r - cost_r, "estimated_cost_r": cost_r,
        "holding_minutes": (exit_at - entry_at).total_seconds() / 60.0,
        "sl_modifications": changes, "sl_modification_count": len(changes),
        "data_horizon": horizon.isoformat(),
    }


def _post_exit(frame, summary):
    meta, details = summary["entry_meta"], summary["exit_details"]
    side, entry = str(meta["side"]).upper(), float(meta["entry_price"])
    risk = abs(entry - float(meta["initial_sl"]))
    exit_at = _utc(details["exit_time"])
    result = {}
    for minutes in POST_EXIT_MINUTES:
        subset = frame[(frame.timestamp >= exit_at.floor("min")) &
                       (frame.timestamp < exit_at + pd.Timedelta(minutes=minutes))]
        if subset.empty:
            result[f"{minutes}m"] = {"available": False}
            continue
        favorable = float(subset.high.max()) if side == "BUY" else float(subset.low.min())
        adverse = float(subset.low.min()) if side == "BUY" else float(subset.high.max())
        mfe_r = _directional_r(favorable, entry, risk, side)
        min_directional_r = _directional_r(adverse, entry, risk, side)
        result[f"{minutes}m"] = {
            "available": True, "post_exit_mfe_r": mfe_r,
            "post_exit_min_directional_r": min_directional_r,
            "reached": {f"{threshold:g}R": mfe_r >= threshold for threshold in (0.5, 1.0, 1.5, 2.0)},
        }
    return result


def _structural_stop_diagnostic(summary, tick):
    meta = summary["entry_meta"]
    context = meta.get("decision_context") or {}
    setup = context.get("setup_context") or {}
    plan = meta.get("target_plan") or context.get("target_plan") or {}
    old_plan = plan.get("stop_plan") or {}
    side, entry = str(meta.get("side")).upper(), float(meta.get("entry_price") or 0.0)
    old_sl, atr = float(meta.get("initial_sl") or 0.0), float(meta.get("atr") or old_plan.get("atr") or 0.0)
    invalidation = float(context.get("invalidation_level") or old_plan.get("invalidation_level") or 0.0)
    anchor = setup.get("setup_anchor") or {}
    anchor_level = float(anchor.get("low" if side == "BUY" else "high") or 0.0)
    anchor_alias = bool(
        anchor_level and invalidation and abs(anchor_level - invalidation) <= max(tick, atr * 0.05)
    )
    if anchor_alias and not bool(anchor.get("is_thesis_invalidation", False)):
        return {
            "available": False,
            "entry_anchor": anchor_level,
            "entry_anchor_type": anchor.get("type"),
            "selected_thesis_invalidation": None,
            "legacy_invalidation_level": invalidation,
            "reason": "ENTRY_ANCHOR_ALIAS_NO_INDEPENDENT_LEVELS_PRESERVED",
            "note": (
                "The legacy invalidation equals the entry anchor.  The old journal stores only "
                "market-level counts, so V2 correctly refuses to invent an independent thesis level."
            ),
        }
    valid = entry > 0 and atr > 0 and invalidation > 0 and (
        invalidation < entry if side == "BUY" else invalidation > entry
    )
    if not valid:
        return {
            "available": False, "entry_anchor": anchor_level or None,
            "selected_thesis_invalidation": None, "reason": "MISSING_OR_INVALID_CAUSAL_INVALIDATION",
        }
    buffer = max(0.30 * atr, 2.0 * tick)
    raw = invalidation - buffer if side == "BUY" else invalidation + buffer
    if tick > 0:
        structural = math.floor(raw / tick) * tick if side == "BUY" else math.ceil(raw / tick) * tick
    else:
        structural = raw
    amount = float(meta.get("amount") or 0.0)
    equity = float(meta.get("wallet_equity_at_entry") or 0.0)
    risk_usd = amount * abs(entry - structural) + amount * entry * ROUND_TRIP_COST_PCT
    risk_pct = risk_usd / equity * 100.0 if equity > 0 else None
    return {
        "available": True, "entry_anchor": anchor_level or None,
        "entry_anchor_type": anchor.get("type"),
        "selected_thesis_invalidation": invalidation,
        "thesis_invalidation_source": "decision_context.invalidation_level",
        "volatility_buffer": buffer, "spread_component_available": False,
        "old_initial_sl": old_sl, "structural_v2_sl": structural,
        "old_distance_atr": abs(entry - old_sl) / atr,
        "v2_distance_atr": abs(entry - structural) / atr,
        "actual_risk_pct_at_recorded_size": risk_pct,
        "hard_cap_pass": risk_pct is not None and risk_pct <= 1.0,
        "invariant_ok": structural < invalidation if side == "BUY" else structural > invalidation,
        "note": "Diagnostic uses the causal explicit invalidation preserved in the journal; executable spread was not preserved.",
    }


def _audit_sl_changes(audit_roots):
    by_trade, seen = {}, set()
    for root in audit_roots:
        for path in root.rglob("decision_audit*.jsonl"):
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event_id = event.get("event_id")
                    if event_id in seen:
                        continue
                    seen.add(event_id)
                    if event.get("stage") != "TRAILING_STOP" or event.get("status") != "SL_REPLACED":
                        continue
                    trade_id = event.get("trade_id")
                    if trade_id:
                        by_trade.setdefault(trade_id, []).append({
                            "at": event.get("timestamp"), **(event.get("details") or {})
                        })
    return by_trade


def _aggregate(rows, selector):
    values = [selector(item) for item in rows if selector(item) is not None]
    if not values:
        return {"count": 0}
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "count": len(values), "net_r": sum(values), "mean_r": statistics.mean(values),
        "median_r": statistics.median(values), "wins": len(wins), "losses": len(losses),
        "average_winner_r": statistics.mean(wins) if wins else None,
        "average_loser_r": statistics.mean(losses) if losses else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "max_drawdown_r": max_dd,
    }


def _money(value):
    return f"{value:.6f}"


def _markdown(payload):
    lines = [
        "# تقرير Replay لإدارة الصفقات — trversion4",
        "",
        f"- المصدر: `{payload['source']['journal_root']}`",
        f"- endpoint العام للشموع فقط: `{DEMO_PUBLIC}`",
        f"- صفقات صالحة للمقارنة: **{payload['coverage']['valid_trades']}**",
        f"- ملفات Summary غير الصالحة/الناقصة: **{payload['coverage']['excluded_trades']}**",
        "- لا توجد أي account/order calls في السكربت.",
        "",
        "## النتيجة الإجمالية",
        "",
        "| السياسة | العدد | Net R | PF | Max DD R |",
        "|---|---:|---:|---:|---:|",
    ]
    current = payload["aggregate"]["CURRENT"]
    lines.append(f"| CURRENT الفعلي | {current.get('count', 0)} | {_money(current.get('net_r', 0))} | {current.get('profit_factor')} | {_money(current.get('max_drawdown_r', 0))} |")
    for key in ("V2_1.0R", "V2_1.5R", "V2_2.0R"):
        item = payload["aggregate"][key]
        lines.append(f"| {key} | {item.get('count', 0)} | {_money(item.get('net_r', 0))} | {item.get('profit_factor')} | {_money(item.get('max_drawdown_r', 0))} |")
    lines.extend(["", "## تفاصيل كل صفقة", ""])
    for item in payload["trades"]:
        lines.extend([
            f"### {item['symbol']} — {item['trade_id']}",
            "",
            f"- Entry `{item['entry']}` | Initial SL `{item['initial_sl']}` | Exit `{item['current']['exit']}` | CURRENT `{item['current']['net_r']:.4f}R`.",
            f"- MFE قبل الخروج `{item['current']['mfe_before_exit_r']:.4f}R`، وتعديلات SL المثبتة بالسجل: `{len(item['current']['confirmed_sl_modifications'])}`.",
            f"- Post-exit 120m MFE: `{item['post_exit'].get('120m', {}).get('post_exit_mfe_r')}`.",
            f"- Structural diagnostic: available=`{item['structural_stop'].get('available')}`; old=`{item['structural_stop'].get('old_initial_sl')}` → V2=`{item['structural_stop'].get('structural_v2_sl')}`; anchor=`{item['structural_stop'].get('entry_anchor')}`, invalidation=`{item['structural_stop'].get('selected_thesis_invalidation')}`, reason=`{item['structural_stop'].get('reason')}`.",
        ])
        for key, result in item["v2"].items():
            lines.append(f"- {key}: `{result.get('estimated_net_r')}`R، exit=`{result.get('exit')}`، SL changes=`{result.get('sl_modification_count')}`، status=`{result.get('status')}`.")
        lines.append("")
    lines.extend([
        "## قيود الدليل",
        "",
        "- CURRENT هو النتيجة الفعلية المخزنة؛ V2 counterfactual وليس تداولًا فعليًا.",
        "- ترتيب 1m محافظ: إذا لمس الوقف والهدف في الدقيقة نفسها يُحسب الوقف أولًا.",
        "- Full V2 structural-stop reconstruction غير ممكن لكل صفقة لأن Runtime القديم لم يحفظ جميع market_levels؛ التشخيص يستخدم invalidation السببي المحفوظ ولا يخترع OB أو Swing.",
        "- الصفقات المفتوحة عند نهاية نافذة الساعتين تُقيّم mark-to-market ولا تُعامل كإغلاق مؤكد.",
        "- العينة Development صغيرة ولا تكفي لاختيار 1.0/1.5/2.0R كسياسة نهائية.",
        "",
        "## اقتصاد حساب 120 USDT",
        "",
        "الهامش 1.20 USDT × 5 = 6 USDT notional. الرسوم المفترضة 0.12% round-trip = 0.0072 USDT، دون Funding.",
        "",
        "| حركة السعر | Gross PnL | Net PnL |",
        "|---:|---:|---:|",
    ])
    for row in payload["account_120_economics"]:
        lines.append(f"| {row['move_pct']:+g}% | {row['gross_pnl_usdt']:+.4f} | {row['net_pnl_usdt']:+.4f} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal-root", type=Path, required=True)
    parser.add_argument("--audit-root", type=Path, action="append", default=[])
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--no-network", action="store_true")
    args = parser.parse_args()

    ticks = {} if args.no_network else _exchange_ticks()
    audit_changes = _audit_sl_changes(args.audit_root)
    trades, excluded = [], []
    for directory in sorted(args.journal_root.iterdir()):
        summary_path = directory / "trade_summary.json"
        if not summary_path.exists():
            continue
        try:
            summary = _read_json(summary_path)
            meta, details = summary["entry_meta"], summary["exit_details"]
            initial_sl = float(meta.get("initial_sl") or 0.0)
            if initial_sl <= 0 or not details.get("exit_time"):
                raise ValueError("MISSING_INITIAL_SL_OR_EXIT")
            entry_at, exit_at = _utc(meta["entry_time"]), _utc(details["exit_time"])
            symbol_id = str(meta.get("symbol") or summary.get("symbol")).replace("/", "")
            start, end = entry_at - pd.Timedelta(hours=12), exit_at + pd.Timedelta(minutes=121)
            if args.no_network:
                frame, source = _local_klines(directory)
            else:
                try:
                    frame, source = _fetch_klines(symbol_id, start, end, args.cache)
                except Exception as exc:
                    frame, source = _local_klines(directory)
                    source = f"journal_fallback:{type(exc).__name__}"
            tick = float(ticks.get(symbol_id) or 0.0)
            current_r = float(details.get("final_r") or 0.0)
            trade_id = str(summary.get("trade_id") or meta.get("trade_id"))
            actual_changes = sorted(audit_changes.get(trade_id, []), key=lambda item: item.get("at") or "")
            if float(meta.get("current_sl") or meta.get("sl") or initial_sl) != initial_sl and not actual_changes:
                actual_changes.append({
                    "at": meta.get("current_sl_confirmed_at"), "old_sl": initial_sl,
                    "new_sl": float(meta.get("current_sl") or meta.get("sl")),
                    "source": "trade_summary_final_confirmed_sl_only",
                })
            item = {
                "trade_id": trade_id, "symbol": str(summary.get("symbol")),
                "side": str(meta["side"]).upper(), "entry": float(meta["entry_price"]),
                "initial_sl": initial_sl, "final_target": float(meta.get("tp") or 0.0),
                "market_data_source": source,
                "current": {
                    "exit": float(details.get("exit_price") or 0.0),
                    "exit_at": _utc(details["exit_time"]).isoformat(),
                    "exit_reason": details.get("exit_reason"), "net_r": current_r,
                    "gross_pnl_usdt": details.get("realized_pnl_gross"),
                    "net_pnl_usdt": details.get("pnl"), "commission": details.get("commission"),
                    "funding": details.get("funding"),
                    "mfe_before_exit_r": float(details.get("mfe_r") or meta.get("mfe_r") or 0.0),
                    "mae_before_exit_r": float(details.get("mae_r") or meta.get("mae_r") or 0.0),
                    "holding_minutes": (exit_at - entry_at).total_seconds() / 60.0,
                    "confirmed_sl_modifications": actual_changes,
                },
                "post_exit": _post_exit(frame, summary),
                "structural_stop": _structural_stop_diagnostic(summary, tick),
                "v2": {},
            }
            for activation in ACTIVATIONS:
                item["v2"][f"activation_{activation:.1f}R"] = _simulate_v2(
                    frame, summary, tick, activation
                )
            item["stop_then_run"] = bool(
                str(details.get("exit_reason")) == "STOP_LOSS"
                and any(
                    point.get("reached", {}).get("1R", False)
                    for point in item["post_exit"].values() if point.get("available")
                )
            )
            trades.append(item)
        except Exception as exc:
            excluded.append({"directory": directory.name, "reason": f"{type(exc).__name__}:{exc}"})

    aggregate = {"CURRENT": _aggregate(trades, lambda item: item["current"]["net_r"])}
    for activation in ACTIVATIONS:
        name, key = f"V2_{activation:.1f}R", f"activation_{activation:.1f}R"
        aggregate[name] = _aggregate(
            trades,
            lambda item, k=key: item["v2"][k].get("estimated_net_r"),
        )
    economics = []
    for move in (-10, -5, -2, -1, 1, 2, 5, 10):
        gross = 6.0 * move / 100.0
        economics.append({
            "move_pct": move, "gross_pnl_usdt": gross,
            "round_trip_fees_usdt": 6.0 * ROUND_TRIP_COST_PCT,
            "net_pnl_usdt": gross - 6.0 * ROUND_TRIP_COST_PCT,
        })
    code_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest().upper()
    payload = {
        "schema_version": 1,
        "source": {
            "journal_root": str(args.journal_root.resolve()),
            "public_market_endpoint": DEMO_PUBLIC,
            "public_market_only": True, "account_or_order_calls": False,
            "script_sha256": code_hash,
        },
        "assumptions": {
            "same_entry_initial_sl_and_final_target": True,
            "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
            "ambiguous_1m_bar": "STOP_FIRST",
            "v2_atr_buffer": 0.30, "v2_min_atr_improvement": 0.15,
            "v2_min_risk_improvement": 0.10,
            "activation_r_sensitivity": list(ACTIVATIONS),
        },
        "coverage": {"valid_trades": len(trades), "excluded_trades": len(excluded), "excluded": excluded},
        "aggregate": aggregate, "trades": trades,
        "account_120_economics": economics,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output_md.write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps({"valid": len(trades), "excluded": len(excluded), "aggregate": aggregate}, indent=2))


if __name__ == "__main__":
    main()
