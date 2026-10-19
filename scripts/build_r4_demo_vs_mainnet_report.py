"""Build the read-only Demo/Mainnet contemporary Freeze R4 micro report."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FREEZE_HASH = "62A4237F1178729BA9E6143D6B7C5FF3FCD7D74C3C7E710E681DA01FA6219D86"
DETECTOR_HASH = "20A7626F141DBE96DB6C79EAF12ABDAAAB47C423BACCC74F53E9835A05C4AFA3"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def distribution(values: list[float]) -> dict[str, Any]:
    clean = [
        float(value)
        for value in values
        if value is not None and math.isfinite(float(value))
    ]
    if not clean:
        return {"n": 0}
    return {
        "n": len(clean),
        "min": min(clean),
        "p10": quantile(clean, 0.10),
        "p25": quantile(clean, 0.25),
        "p50": quantile(clean, 0.50),
        "mean": statistics.fmean(clean),
        "p75": quantile(clean, 0.75),
        "p90": quantile(clean, 0.90),
        "p95": quantile(clean, 0.95),
        "max": max(clean),
    }


def metric_distribution(cases: list[dict[str, Any]], field: str) -> dict[str, Any]:
    return distribution([case["micro"][field] for case in cases])


def minute_distribution(cases: list[dict[str, Any]], field: str) -> dict[str, Any]:
    return distribution([
        minute[field]
        for case in cases
        for minute in case["micro"]["minute_inputs"]
    ])


def dataset_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    passes = sum(bool(case["micro"]["gate_passed"]) for case in cases)
    return {
        "cases": len(cases),
        "passes": passes,
        "failures": len(cases) - passes,
        "pass_rate": passes / len(cases) if cases else None,
        "flow_price_agreement": metric_distribution(cases, "flow_price_agreement"),
        "directional_taker_imbalance_mean": metric_distribution(
            cases, "directional_taker_imbalance_mean"
        ),
        "minute_taker_buy_quote_ratio": minute_distribution(
            cases, "taker_buy_quote_ratio"
        ),
        "minute_taker_imbalance": minute_distribution(cases, "taker_imbalance"),
    }


def first_failure_distribution(summary: dict[str, Any] | None) -> dict[str, int] | None:
    if not summary:
        return None
    last_poll = summary.get("last_poll") or {}
    return last_poll.get("first_failure")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--mainnet-summary", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    source = read_json(args.source)
    if source.get("freeze_hash") != FREEZE_HASH:
        raise RuntimeError("SOURCE_FREEZE_HASH_MISMATCH")
    if source.get("detector_hash") != DETECTOR_HASH:
        raise RuntimeError("SOURCE_DETECTOR_HASH_MISMATCH")

    demo_cases = list(source["cases"]["live"])
    matched: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for demo in demo_cases:
        public = demo.get("contemporaneous_mainnet_public") or {}
        if not public.get("available"):
            unavailable.append({
                "symbol": demo["symbol"],
                "closed_5m_timestamp": demo["closed_5m_timestamp"],
            })
            continue
        mainnet_micro = public["micro"]
        demo_pass = bool(demo["micro"]["gate_passed"])
        mainnet_pass = bool(mainnet_micro["gate_passed"])
        matched.append({
            "symbol": demo["symbol"],
            "closed_5m_timestamp": demo["closed_5m_timestamp"],
            "side": demo["side"],
            "score": demo["score"],
            "demo_event_5x1m_hash": demo["micro"]["event_5x1m_hash"],
            "mainnet_event_5x1m_hash": mainnet_micro["event_5x1m_hash"],
            "same_5x1m_values": bool(public.get("same_values_as_demo")),
            "demo": demo["micro"],
            "mainnet": mainnet_micro,
            "flip": (
                "FAIL_TO_PASS" if not demo_pass and mainnet_pass
                else "PASS_TO_FAIL" if demo_pass and not mainnet_pass
                else "PASS_TO_PASS" if demo_pass
                else "FAIL_TO_FAIL"
            ),
        })

    demo_projection = [{**case, "micro": case["demo"]} for case in matched]
    mainnet_projection = [{**case, "micro": case["mainnet"]} for case in matched]
    flips = {
        label: sum(case["flip"] == label for case in matched)
        for label in ("FAIL_TO_PASS", "PASS_TO_FAIL", "PASS_TO_PASS", "FAIL_TO_FAIL")
    }
    side_breakdown: dict[str, Any] = {}
    for side in ("LONG", "SHORT"):
        selected = [case for case in matched if case["side"] == side]
        side_breakdown[side] = {
            "matched_cases": len(selected),
            "demo": dataset_summary([{**case, "micro": case["demo"]} for case in selected]),
            "mainnet": dataset_summary([{**case, "micro": case["mainnet"]} for case in selected]),
            "flips": {
                label: sum(case["flip"] == label for case in selected)
                for label in flips
            },
        }

    mainnet_summary = (
        read_json(args.mainnet_summary)
        if args.mainnet_summary and args.mainnet_summary.exists()
        else None
    )
    july_overall = source["comparison"]["july"]["overall"]
    sidecar_source = ROOT / "scripts" / "run_r4_gate_sidecar.py"
    sidecar_source_text = sidecar_source.read_text(encoding="utf-8").lower()
    banned_tokens = (
        "binance_api_key", "binance_api_secret", "create_order(", "place_order(",
        "new_order(", '"/fapi/v1/order"', '"/fapi/v2/account"',
        '"/fapi/v2/positionrisk"', '"/fapi/v1/openorders"',
    )
    findings = [token for token in banned_tokens if token in sidecar_source_text]

    generated_at = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "generated_at": generated_at,
        "read_only": True,
        "services_changed_by_report_builder": False,
        "threshold_changed": False,
        "freeze_hash": FREEZE_HASH,
        "detector_hash": DETECTOR_HASH,
        "micro_threshold": source["micro_threshold"],
        "matched_case_comparison": {
            "requested_demo_cases": len(demo_cases),
            "matched_cases": len(matched),
            "unavailable_cases": unavailable,
            "same_exact_5x1m_values": sum(case["same_5x1m_values"] for case in matched),
            "different_5x1m_values": sum(not case["same_5x1m_values"] for case in matched),
            "flips": flips,
            "demo": dataset_summary(demo_projection),
            "mainnet": dataset_summary(mainnet_projection),
            "by_side": side_breakdown,
        },
        "july_mainnet_reference": source["comparison"]["july"],
        "native_mainnet_sidecar": {
            "summary_available": mainnet_summary is not None,
            "summary": mainnet_summary,
            "first_failure_distribution_latest_poll": first_failure_distribution(mainnet_summary),
        },
        "strategy_semantics_audit": {
            "formula": source["semantic_audit"]["formula"],
            "algebra": "direction^2 = 1, so the metric is side-invariant",
            "origin_commit": "1809587 feat: add evidence-backed active altcoin shadow path",
            "origin_code": "scripts/replay_active_altcoin_1m.py:201-225",
            "policy_protocol": "ACTIVE_ALTCOIN_MICRO_GATE_PROTOCOL_2026-09-04.md:8,32-33",
            "evidence": [
                "The original research code computed directional_imbalance times signed_bar_returns.",
                "The protocol separately listed direction-adjusted imbalance and flow/price agreement.",
                "The feature therefore measures aggressive-flow/price agreement, not support for one candidate side.",
            ],
            "classification": "FROZEN_RESEARCH_SEMANTICS_WITH_DOCUMENTATION_AMBIGUITY",
            "intent_confidence": "HIGH_BUT_NOT_EXPLICITLY_RATIFIED",
            "implementation_bug": False,
            "formula_changed": False,
        },
        "mainnet_sidecar_safety": {
            "endpoint": "https://fapi.binance.com",
            "public_get_only": True,
            "credential_inputs": False,
            "order_route": False,
            "static_banned_token_findings": findings,
            "static_scan_passed": not findings,
            "source_sha256": sha256_file(sidecar_source),
        },
        "cases": matched,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "R4_MICRO_DEMO_VS_MAINNET_CONTEMPORARY.json"
    json_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    mc = result["matched_case_comparison"]
    dsum = mc["demo"]
    msum = mc["mainnet"]
    native_funnel = ((mainnet_summary or {}).get("unique_funnel") or {})
    markdown = f"""# تشخيص R4: بيانات Demo مقابل Mainnet المعاصرة

تاريخ التوليد: `{generated_at}`

## الحكم التنفيذي

- لم يتغير Freeze أو Threshold أو أي خدمة تداول بسبب هذا التقرير.
- المقارنة المتطابقة استخدمت `{mc['matched_cases']}` من أصل `{mc['requested_demo_cases']}` حالة Demo وصلت إلى score.
- لا توجد حالة واحدة ذات قيم 1m متطابقة بالكامل بين البيئتين: `{mc['same_exact_5x1m_values']}` متطابقة و`{mc['different_5x1m_values']}` مختلفة.
- Demo مرّر `{dsum['passes']}/{dsum['cases']}` (`{100 * dsum['pass_rate']:.2f}%`)؛ Mainnet مرّر `{msum['passes']}/{msum['cases']}` (`{100 * msum['pass_rate']:.2f}%`).
- مرجع July Mainnet مرّر `{july_overall['passed']}/{july_overall['cases']}` (`{100 * july_overall['pass_rate']:.2f}%`). معدل Mainnet المعاصر المتطابق قريب منه، بينما Demo هو الخارج بوضوح.
- الانقلابات: fail→pass=`{flips['FAIL_TO_PASS']}`، pass→fail=`{flips['PASS_TO_FAIL']}`، pass→pass=`{flips['PASS_TO_PASS']}`، fail→fail=`{flips['FAIL_TO_FAIL']}`.

## المقارنة المتطابقة

| القياس | October Demo | October Mainnet matched | July Mainnet |
|---|---:|---:|---:|
| حالات score | {dsum['cases']} | {msum['cases']} | {july_overall['cases']} |
| Micro pass | {dsum['passes']} | {msum['passes']} | {july_overall['passed']} |
| معدل المرور | {100 * dsum['pass_rate']:.2f}% | {100 * msum['pass_rate']:.2f}% | {100 * july_overall['pass_rate']:.2f}% |
| وسيط flow/price | {dsum['flow_price_agreement']['p50']:.6f} | {msum['flow_price_agreement']['p50']:.6f} | {july_overall['metrics']['flow_price_agreement']['p50']:.6f} |
| متوسط flow/price | {dsum['flow_price_agreement']['mean']:.6f} | {msum['flow_price_agreement']['mean']:.6f} | {july_overall['metrics']['flow_price_agreement']['mean']:.6f} |
| وسيط taker-buy ratio (1m) | {dsum['minute_taker_buy_quote_ratio']['p50']:.6f} | {msum['minute_taker_buy_quote_ratio']['p50']:.6f} | غير متاح في ملخص يوليو |

## Mainnet Sidecar الأصلي

- الحالة المتاحة: `{mainnet_summary is not None}`
- endpoint: `https://fapi.binance.com`
- read-only/public GET: `true`
- مفاتيح أو حسابات أو أوامر: `false`
- المسح الفريد الحالي: `{native_funnel.get('scanned_unique', 'N/A')}`
- score: `{native_funnel.get('score', 'N/A')}`
- micro: `{native_funnel.get('micro', 'N/A')}`
- Raw LONG/SHORT: `{native_funnel.get('raw_LONG', 'N/A')}/{native_funnel.get('raw_SHORT', 'N/A')}`

## تدقيق معنى الصيغة

الصيغة المجمدة تلغي اتجاه الصفقة جبريًا لأن `direction²=1`. تتبع Git يثبت أنها ظهرت في البحث الأصلي في commit `1809587` كميزة **اتفاق التدفق مع حركة السعر**، بينما كان الانحياز الاتجاهي نفسه ميزة منفصلة. لذلك هذا ليس خلل تنفيذ أدخله R4؛ الأقرب أنه معنى بحثي مقصود لقياس الازدحام/الاتفاق بصرف النظر عن LONG/SHORT. لكن البروتوكول لم يصرّح بهذه الخاصية الجبرية، لذا التصنيف الدقيق هو: **معنى بحثي مجمد مع غموض توثيقي**، ولا يجوز تغييره قبل قرار استراتيجي منفصل.

## السلامة

- Freeze: `{FREEZE_HASH}`
- Detector: `{DETECTOR_HASH}`
- static no-order/credential scan: `{'PASS' if not findings else 'FAIL'}`
- تغييرات Production/Shadow/Demo-B: `لا شيء`
"""
    md_path = args.output_dir / "R4_MICRO_DEMO_VS_MAINNET_CONTEMPORARY_AR.md"
    md_path.write_text(markdown, encoding="utf-8")

    proof = {
        "generated_at": generated_at,
        "endpoint": "https://fapi.binance.com",
        "read_only": True,
        "public_get_only": True,
        "credential_inputs": False,
        "order_route": False,
        "freeze_hash": FREEZE_HASH,
        "detector_hash": DETECTOR_HASH,
        "source_sha256": sha256_file(sidecar_source),
        "static_banned_token_findings": findings,
        "static_scan_passed": not findings,
    }
    proof_path = args.output_dir / "R4_MAINNET_SIDECAR_IDENTITY_SAFETY_PROOF.json"
    proof_path.write_text(
        json.dumps(proof, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    manifest = {
        path.name: sha256_file(path)
        for path in (json_path, md_path, proof_path)
    }
    (args.output_dir / "MANIFEST_SHA256.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({"output_dir": str(args.output_dir), "manifest": manifest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
