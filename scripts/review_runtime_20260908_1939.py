"""Read the three supplied ZIPs offline; write only local review artifacts."""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
STEM = "runtime_review_20260908_1939"
ADEN = timezone(timedelta(hours=3))

def local(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ADEN).strftime("%Y-%m-%d %H:%M:%S")

def counts(rows):
    return dict(sorted(Counter(e["stage"] + ":" + e["status"] for e in rows).items()))

def select(rows, stage, status=None):
    return [e for e in rows if e["stage"] == stage and (status is None or e["status"] == status)]

def canon(symbol):
    return (symbol or "").split(":")[0].replace("/", "")

def build():
    events, archives, seen, activity = [], [], set(), []
    duplicates = malformed = 0
    for part in ("01", "02", "03"):
        path = ROOT / f"runtime_logs_24h_20260908_1939_part{part}.zip"
        with ZipFile(path) as z:
            corrupt = z.testzip()
            if corrupt:
                raise ValueError(f"Corrupt member: {path.name}/{corrupt}")
            archives.append({"name": path.name, "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "crc_ok": True,
                "members": {i.filename: i.file_size for i in z.infolist()},
                "exporter_summary": z.read("summary.txt").decode("utf-8")})
            activity.extend(z.read("bot_activity_24h.log").decode("utf-8").splitlines())
            for line in z.open("decision_audit_24h.jsonl"):
                try:
                    e = json.loads(line)
                    event_id = e["event_id"]
                except (ValueError, KeyError):
                    malformed += 1
                    continue
                if event_id in seen:
                    duplicates += 1
                    continue
                seen.add(event_id)
                events.append(e)
    events.sort(key=lambda e: (e["timestamp"], e["event_sequence"]))
    latest = max(select(events, "RUN_SESSION"), key=lambda e: e["timestamp"])
    current = [e for e in events if e["run_id"] == latest["run_id"]]
    byrun = defaultdict(list)
    for e in events:
        byrun[e["run_id"]].append(e)
    runs = []
    for run_id, rows in byrun.items():
        session = next(iter(select(rows, "RUN_SESSION")), None)
        seq = [e["event_sequence"] for e in rows]
        runs.append({"run_id": run_id, "first": rows[0]["timestamp"], "last": rows[-1]["timestamp"],
            "events": len(rows), "sequence_min": min(seq), "sequence_max": max(seq),
            "internal_sequence_gaps": max(seq) - min(seq) + 1 - len(set(seq)),
            "duplicate_sequences": len(seq) - len(set(seq)),
            "code_identity": session["details"].get("code_identity") if session else None,
            "configuration": session["details"].get("configuration") if session else None,
            "stage_counts": counts(rows)})
    uni = {e["scan_id"]: e["details"] for e in select(current, "SCANNER_UNIVERSE")}
    def cohort(e):
        d = uni.get(e["scan_id"])
        if d is None:
            return "unmapped"
        return "activity_added" if canon(e["symbol"]) in {canon(s) for s in d["added_vs_liquidity"]} else "liquidity_retained"
    mapping = {e["decision_id"]: e.get("thesis_id") or e.get("setup_id") for e in select(current, "SETUP_CREATED")}
    def identity(e):
        return e.get("thesis_id") or e.get("setup_id") or e["details"].get("setup_thesis_id") or mapping.get(e["decision_id"])
    stages = [("SETUP_CREATED", "ACTIVE"), ("QUALITY_FILTER", "PASS"), ("RR_FILTER", "PASS"), ("ENTRY_ZONE_TOUCH", "TOUCHED"), ("ENTRY_CONFIRMATION", "PASS")]
    sets = [{identity(e) for e in select(current, s, st)} - {None} for s, st in stages]
    disjoint = [len(sets[i] - sets[i + 1]) for i in range(len(sets) - 1)]
    assert all(b <= a for a, b in zip(sets, sets[1:]))
    assert sum(disjoint) + len(sets[-1]) == len(sets[0])
    pending = []
    for e in select(current, "SNIPER", "PENDING"):
        related = [x for x in current if x.get("trade_id") == e["trade_id"]]
        confirmations = select(related, "ENTRY_CONFIRMATION")
        terminal = [x for x in select(related, "SNIPER") if x["status"] != "PENDING"]
        plan = confirmations[0]["details"]["target_plan"]
        pending.append({"trade_id": e["trade_id"], "thesis_id": identity(e), "symbol": e["symbol"],
            "cohort": cohort(e), "created_utc": e["timestamp"], "created_aden": local(e["timestamp"]),
            "side": plan["stop_plan"]["side"], "entry": plan["entry"], "sl": plan["sl"], "tp": plan["tp"], "rr": plan["rr"],
            "touches": [{"timestamp": x["timestamp"], **x["details"]} for x in select(related, "ENTRY_ZONE_TOUCH")],
            "confirmations": [{"timestamp": x["timestamp"], "event_id": x["event_id"], **x["details"]["confirmation"]} for x in confirmations],
            "terminal": [{"timestamp": x["timestamp"], "status": x["status"], "details": x["details"]} for x in terminal],
            "evidence_event_ids": [x["event_id"] for x in related if x["stage"] in {"SNIPER", "ENTRY_ZONE_TOUCH", "ENTRY_CONFIRMATION", "ENTRY_PRICE_BASIS", "INVALIDATION_STATE"}]})
    shadow = {}
    for e in select(current, "ACTIVE_ALTCOIN_SHADOW"):
        if not e["status"].startswith("OUTCOME_"):
            continue
        p, o = e["details"]["plan"], e["details"]["outcome"]
        shadow[p["plan_id"]] = {"symbol": e["symbol"], "event_id": e["event_id"],
            "execution_enabled": e["details"]["execution_enabled"], "reported_at": e["timestamp"],
            "plan": {k: p.get(k) for k in ("plan_id", "setup_id", "side", "filled_at", "observed_at", "observation_delay_seconds", "entry", "stop", "target", "round_trip_cost_bps")},
            "recorded_outcome": o}
    resources = select(current, "RESOURCE_SNAPSHOT")
    warm = [e["details"]["resource"] for e in resources if e["details"]["resource"]["uptime_seconds"] >= 3600]
    resource_summary = {k: {"first_after_one_hour": warm[0][k], "last": warm[-1][k], "min_after_one_hour": min(x[k] for x in warm), "max_after_one_hour": max(x[k] for x in warm)}
        for k in ("rss_mb", "threads", "file_descriptors", "cgroup_current_mb", "cgroup_oom_events", "cgroup_oom_kill_events")}
    levels = Counter()
    errors = []
    monitor_warnings = 0
    for line in activity:
        match = re.search(r" - (WARNING|ERROR|CRITICAL) - (.*)", line)
        if not match:
            continue
        level, message = match.groups()
        levels[level] += 1
        if level in {"ERROR", "CRITICAL"}:
            errors.append({"timestamp": line[:24], "message": message})
        if level == "WARNING" and "[Network Monitor]" in message:
            monitor_warnings += 1
    net = [x["recorded_outcome"]["net_r"] for x in shadow.values()]
    total = counts(current)
    assert malformed == 0 and duplicates == 0
    assert all(x["internal_sequence_gaps"] == x["duplicate_sequences"] == 0 for x in runs)
    assert total["SCAN_STARTED:STARTED"] == total["HYBRID_DECISION:HOLD"] + total["HYBRID_DECISION:BUY"] + total["HYBRID_DECISION:SELL"] + total.get("SCAN_SKIP:PENDING_ORDER", 0)
    assert all(d["selected_count"] == len(d["targets"]) == 50 for d in uni.values())
    assert all(len(p["terminal"]) == 1 for p in pending)
    assert all(not x["execution_enabled"] for x in shadow.values())
    result = {
        "method": "Offline raw audit events, deduplicated by event_id; current run isolated; no market download or counterfactual replay.",
        "archives": archives, "unique_events": len(events), "malformed": malformed, "duplicates": duplicates,
        "first_event_utc": events[0]["timestamp"], "last_event_utc": events[-1]["timestamp"],
        "first_event_aden": local(events[0]["timestamp"]), "last_event_aden": local(events[-1]["timestamp"]),
        "runs": runs, "all_stage_counts": counts(events),
        "current_run_id": latest["run_id"], "code_identity": latest["details"]["code_identity"],
        "configuration": latest["details"]["configuration"],
        "entry_margin_percent_logged": "entry_margin_percent" in latest["details"]["configuration"],
        "current_stage_counts": total,
        "theses_reaching_stages": {s + ":" + st: len(v) for (s, st), v in zip(stages, sets)},
        "disjoint_deepest_stage_counts": dict(zip(("never_quality_passed", "quality_passed_never_rr_passed", "rr_passed_never_touched", "touched_without_confirmation_pass"), disjoint)),
        "quality_rejection_gates": dict(Counter(e["details"]["gates"]["terminal_gate"] for e in select(current, "QUALITY_FILTER", "REJECTED"))),
        "confirmation_failed_gates_overlapping": dict(Counter(g for e in select(current, "ENTRY_CONFIRMATION") for g in e["details"]["confirmation"]["failed_gates"])),
        "universe": {"scans": len(uni), "activity_status": dict(Counter(d["activity_status"] for d in uni.values())),
            "new_unique_symbols": len({s for d in uni.values() for s in d["added_vs_liquidity"]}),
            "new_symbol_attempts": sum(len(d["added_vs_liquidity"]) for d in uni.values()),
            "additions_min": min(len(d["added_vs_liquidity"]) for d in uni.values()),
            "additions_max": max(len(d["added_vs_liquidity"]) for d in uni.values()),
            "cohort_counts": {c: counts([e for e in current if e["symbol"] and cohort(e) == c]) for c in ("activity_added", "liquidity_retained", "unmapped")}},
        "pending_plans": pending,
        "shadow_outcomes": list(shadow.values()), "shadow_net_r_sum": sum(net), "shadow_net_r_mean": sum(net)/len(net),
        "resource_summary_after_one_hour": resource_summary,
        "last_resource_snapshot": resources[-1],
        "activity_log_levels_all_runs": dict(levels), "network_monitor_warnings_all_runs": monitor_warnings,
        "activity_errors_all_runs": errors,
        "limitations": [
            "Requested 24 hours; supplied audit data starts after prior cleanup, covering about 19 hours.",
            "No exchange fills or account statement in these ZIPs; zero executions means no execution events observed.",
            "Activity cohort includes symbols absent from same-scan liquidity-only baseline; displaced symbols were not evaluated, so this is not a full control experiment.",
            "Shadow entries are simulated historical minute opens observed around 33 seconds later; no executable fill validation, live PnL, or profitability proof.",
            "Entry margin 1 percent exists in deployed source default but is absent from RUN_SESSION configuration; no actual fill verifies applied margin.",
        ],
        "integrity_checks": "CRC, JSON parsing, event uniqueness, per-run sequence continuity, scan conservation, thesis set nesting, universe size and terminal plan reconciliation passed.",
    }
    return result

def write_report(r):
    c = r["current_stage_counts"]
    text = [
        "مراجعة سجل 8 سبتمبر 2026 — الإصدار 1.6.9",
        "",
        "البوت واصل الفحص، لكن لم يسجل تنفيذ أي صفقة. إضافة النشاط اللحظي أوصلت خمس فرص إضافية إلى انتظار الدخول؛ شروط التأكيد منعت التنفيذ.",
        "",
        f"الملفات الثلاثة تحتوي {r['unique_events']:,} حدثًا فريدًا. التغطية الفعلية من {r['first_event_aden']} إلى {r['last_event_aden']} بتوقيت عدن، وليست 24 ساعة كاملة بسبب تنظيف السجل السابق. اسم التصدير 1939 يشير إلى UTC؛ آخر حدث يوافق 22:39 بتوقيت عدن.",
        "",
        "التشغيل الحالي بدأ 04:25:24 بتوقيت عدن، واستمر نحو 18 ساعة و14 دقيقة حتى نهاية التصدير. بقية الملفات تشمل نهاية 1.6.7، ثم 1.6.8، وتشغيلًا قصيرًا لـ1.6.9 قبل تطبيق إعداد عدد الصفقات.",
        "",
        "النتيجة في التشغيل الحالي:",
        "",
        "| المرحلة | العدد |",
        "|---|---:|",
        "| جولات مكتملة، 50 عملة في الجولة | 73 |",
        "| محاولات فحص، منها حالتان تجاوزهما الفحص لوجود أمر افتراضي معلّق | 3650 |",
        "| قرارات HOLD | 3513 |",
        "| إشارات BUY / SELL، تتكرر بعض أفكارها بين الجولات | 135 |",
        "| أفكار مستقلة بمعرّف thesis | 45 |",
        "| رفض بسبب فترة انتظار فكرة سابقة | 4 |",
        "| رفض جودة الدخول / اجتيازها | 92 / 39 |",
        "| رفض العائد مقابل المخاطرة / اجتيازه | 32 / 7 |",
        "| خطط معلقة / خطط لمست منطقة الدخول | 7 / 4 |",
        "| اجتياز التأكيد / تنفيذ مسجل | 0 / 0 |",
        "",
        "بعد إزالة تكرار الفكرة عبر الجولات: 23 فكرة لم تجتز الجودة؛ 15 اجتازت الجودة ولم تجتز العائد/المخاطرة؛ 3 اجتازت الاثنين ولم تلمس منطقة الدخول؛ و4 لمست المنطقة دون اجتياز التأكيد. المجموع 45.",
        "",
        "أسباب رفض الجودة في المحاولات الـ92: انخفاض ATR عن 0.7% في 61، الدخول قرب طرف الشمعة في 21، امتداد اتجاهي كبير في 7، وحركة كبيرة دون تصحيح في 3. هذه محاولات متكررة وليست 92 فرصة مستقلة.",
        "",
        "مصير الخطط السبع، والأوقات بتوقيت عدن. الأسعار خطط مشروطة وليست تنفيذات:",
        "",
        "| العملة | الاتجاه | بدء الانتظار | الدخول المخطط | وقف الخسارة | الهدف | النهاية |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    explanations = {
        "USELESS/USDT": "لمست المنطقة؛ أُلغي البيع بإغلاق 15m فوق مستوى الإبطال",
        "4/USDT": "لمست المنطقة؛ أُلغي البيع بإغلاق 15m فوق مستوى الإبطال",
        "FIL/USDT": "لمست المنطقة؛ انتهت مهلة التأكيد دون تأكيد الاتجاه والمشاركة",
        "DASH/USDT": "لمست المنطقة؛ انتهت المهلة دون اجتماع الحجم وسحب السيولة",
        "INJ/USDT": "أُلغي لأن السعر بلغ الهدف قبل الدخول؛ لم تُسجل لمسة للمنطقة",
        "MEGA/USDT": "انتهت 30 دقيقة دون لمسة للمنطقة",
        "MAGMA/USDT": "انتهت 30 دقيقة دون لمسة للمنطقة",
    }
    for p in r["pending_plans"]:
        side = "شراء" if p["side"] == "buy" else "بيع"
        end_time = local(p["terminal"][0]["timestamp"])[11:]
        text.append(f"| {p['symbol']} | {side} | {p['created_aden'][11:]} | {p['entry']:.8g} | {p['sl']:.8g} | {p['tp']:.8g} | {end_time}: {explanations[p['symbol']]} |")
    text += [
        "",
        "سُجل 15 تقييمًا للتأكيد: شرط المشاركة فشل في جميعها، واتجاه/استعادة المستوى فشل في 12، وموقع الدخول في 7؛ الأسباب تتداخل ولا تُجمع كفرص منفصلة.",
        "",
        "DASH حالة محددة تستحق تجربة لاحقة: عند 19:00:07 بتوقيت عدن تحقّق سحب السيولة دون حجم كافٍ، وعند 19:05:18 تحقّق الحجم دون سحب سيولة في تلك الشمعة. باقي بوابات التأكيد اجتازت في التقييمين، لكن البروفايل الصارم والكود يشترطان اجتماع الدليلين في التقييم نفسه. هذا سلوك القاعدة الحالية؛ لا يثبت وحده خطأ برمجيًا أو أن السماح بالدخول كان سيحقق ربحًا. كما رُفض سعر DASH مرة بسبب اختلاف سعر التنفيذ عن المرجع، ثم اجتاز هذا الحاجز ولمس المنطقة.",
        "",
        "إضافة النشاط اللحظي إلى قائمة الفحص:",
        "",
        "| المصدر وقت الجولة | محاولات الفحص | إشارات اتجاهية | اجتياز الجودة | خطط وصلت للانتظار |",
        "|---|---:|---:|---:|---:|",
        "| عملات أضافها النشاط خارج قائمة السيولة الأصلية للجولة | 1184 | 33 | 16 | 5 |",
        "| عملات بقيت من قائمة السيولة | 2466 | 102 | 23 | 2 |",
        "",
        "الخطط الخمس الإضافية: FIL وDASH وINJ وMEGA وMAGMA. كانت بيانات النشاط FRESH في الجولات الـ73، وأضيفت 12–19 عملة لكل جولة مع بقاء الإجمالي 50. عدد العملات المختلفة المضافة خلال الفترة 219. هذا أثر مثبت في اكتشاف المرشحين؛ العملات المستبعدة من قائمة السيولة لم تُفحص بالتوازي، فلا يمكن حساب تحسن كامل مقابل النسخة السابقة من هذا السجل وحده.",
        "",
        "مسار Active Altcoin التجريبي سجل ست نتائج لخطط افتراضية مختلفة، والتنفيذ فيه معطّل:",
        "",
        "| العملة | الاتجاه | الدخول الافتراضي بتوقيت عدن | النتيجة | صافي R وفق النموذج |",
        "|---|---|---|---|---:|",
    ]
    for x in r["shadow_outcomes"]:
        p, o = x["plan"], x["recorded_outcome"]
        text.append(f"| {x['symbol']} | {'شراء' if p['side']=='LONG' else 'بيع'} | {local(p['filled_at'])[11:]} | {'هدف' if o['status']=='TP_FIRST' else 'وقف'} | {o['net_r']:+.3f} |")
    text += [
        "",
        f"ثلاثة أهداف وثلاثة وقوف؛ مجموع صافي النتائج {r['shadow_net_r_sum']:+.3f}R ومتوسطها {r['shadow_net_r_mean']:+.3f}R بعد تكلفة افتراضية 12 نقطة أساس للذهاب والعودة. R هو مقدار المخاطرة المخطط لكل محاكاة، وليس نسبة من الحساب أو ربحًا فعليًا. أسعار الدخول افتتاح دقائق تاريخية رُصدت بعدها بنحو 33 ثانية، لذلك لا تضمن السعر الذي كان يمكن تنفيذه فعليًا. ست حالات لا تثبت ربحية مستمرة.",
        "",
        "مسار Momentum في التشغيل الحالي سجل 24 إعدادًا أصليًا و7 إعدادات مرتبطة بخطط SMC؛ العدد في جميع التشغيلات معًا 26 إعدادًا أصليًا. اجتاز التأكيد إعداد أصلي واحد لـCOLLECT ثم رُفض لغياب هدف سيولة معاكس فعلي؛ لم تتأهل خطة مكتملة أو تُسجل نتيجة تنفيذ فيه.",
        "",
        "حالة التشغيل والإعدادات:",
        "",
        "- سجل التشغيل الحالي يثبت الإصدار 1.6.9، commit e510b39a037386c27602e89a89923bfb53d62b9e، ووضع TESTNET، والحد 10 صفقات والرافعة 5x. الهامش الافتراضي 1% موجود في مصدر الإصدار، لكن حقل الهامش غير مدرج في لقطة إعدادات السجل؛ لا توجد صفقة يمكن منها التحقق من الحجم المطبق. risk_percent=0.25 هو سقف المخاطرة عند الوقف، وهو إعداد مختلف عن الهامش.",
        "- ثلاثة أخطاء شبكة لجلب شموع في الملفات كلها: RAYSOL مرتان خلال 1.6.8، وW مرة خلال 1.6.9. سُجل أيضًا 16 تحذيرًا من مراقب الاتصال. لا تثبت هذه الأخطاء سببًا لتعطل الجولات أو منع الخطط السبع؛ التشغيل الحالي أكمل جولاته.",
        "- لا أحداث نفاد ذاكرة أو إعادة تشغيل لاحقة ظاهرة في التشغيل الحالي. بعد أول ساعة، RSS بين 338.10 و352.49 MiB، وآخر قراءة 343.93 MiB؛ لا يظهر نمو مستمر في الذاكرة خلال هذه النافذة.",
        "- آخر لقطة تخزين: مستخدم 186.07 MiB، متاح 237.59 MiB، والسجلات النشطة 86.41 MiB. المقادير عند نهاية التصدير، وليست قراءة لحظية الآن.",
        "",
        "أولوية العمل التالية: تقييم قاعدة تجميع أدلة التأكيد عبر نافذة زمنية قصيرة على حالات مستقلة، بدءًا من DASH، مع بقاء الإبطال وانتهاء المهلة. يجب فصل هذه التجربة عن التنفيذ إلى أن تُقاس آثارها؛ كثرة الصفقات أو تخفيف الفلاتر وحدهما لا يثبتان التحسن. لم تغيّر هذه المراجعة استراتيجية التشغيل.",
        "",
        "التحقق: CRC للأرشيفات سليم؛ لا سطور JSON تالفة أو أحداث مكررة أو فجوات داخل تسلسل كل تشغيل. تم التحقق من اتساق أعداد الفحص ومراحل الأفكار وإغلاق كل الخطط السبع. لم تُحذف الأرشيفات أو تُعدّل ملفات البوت.",
        "",
        "النتائج التفصيلية ومعرّفات الأحداث وبصمات الملفات: audit_exports/runtime_review_20260908_1939.json",
        "إعادة الإنتاج دون شبكة: python scripts/review_runtime_20260908_1939.py",
    ]
    return "\n".join(text) + "\n"

if __name__ == "__main__":
    result = build()
    output = ROOT / "audit_exports" / (STEM + ".json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = ROOT / "RUNTIME_REVIEW_20260908_1939_AR.md"
    report.write_text(write_report(result), encoding="utf-8")
    print(json.dumps({"report": str(report), "evidence": str(output), "events": result["unique_events"],
        "current_stage_counts": {k: v for k, v in result["current_stage_counts"].items() if k.startswith(("SCAN_ROUND:", "HYBRID_DECISION:", "QUALITY_FILTER:", "RR_FILTER:", "ENTRY_CONFIRMATION:"))},
        "theses": result["disjoint_deepest_stage_counts"], "checks": result["integrity_checks"]}, ensure_ascii=True, indent=2))
