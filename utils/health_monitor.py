import time
from datetime import datetime
from core.logger import logger
from core.config import Config
from utils.runtime_health import resource_snapshot

_PROCESS_START_TIME = time.time()

class HealthMonitor:
    def __init__(self, client, state_manager, notifier):
        self.client = client
        self.state_manager = state_manager
        self.notifier = notifier
        self.start_time = _PROCESS_START_TIME
        
    def check_system_health(self):
        """قياس العملية والحاوية؛ system RAM% كان مضللاً داخل Railway."""
        try:
            snapshot = resource_snapshot()
            snapshot["uptime_hours"] = snapshot["uptime_seconds"] / 3600
            return snapshot
        except Exception as e:
            logger.error(f"خطأ في قراءة صحة النظام: {e}")
            return None

    def check_exchange_connection(self):
        """فحص جودة الاتصال بباينانس والـ Latency"""
        try:
            start_ping = time.time()
            # فحص خفيف للتأكد من الشبكة
            self.client.exchange.fetch_time()
            latency_ms = (time.time() - start_ping) * 1000
            return {"status": "متصل 🟢", "latency": latency_ms}
        except Exception as e:
            logger.warning(f"تحذير من اتصال باينانس: {e}")
            return {"status": "مفصول 🔴", "latency": 0}

    def generate_health_report(self):
        sys_health = self.check_system_health()
        conn_health = self.check_exchange_connection()
        from utils.operational_snapshot import build_operational_snapshot
        operational = build_operational_snapshot(
            self.state_manager, client=self.client
        )
        
        # جلب عدد الصفقات المعلقة (Sniper) والمفتوحة
        virtual_count = len(self.state_manager.get_virtual_orders())
        active_count = len(self.state_manager.get_active_symbols())
        locked_count = len(self.state_manager.get("pair_locks", {}))
        
        msg = "🩺 <b>تقرير صحة البوت (Health Monitor)</b> 🩺\n\n"
        runtime = operational.get("runtime") or {}
        mode = operational.get("mode") or {}
        proposal_summary = operational.get("proposal_summary") or {}
        protection = operational.get("protection_summary") or {}
        msg += (
            f"🏷️ <b>Runtime:</b> <code>{runtime.get('app_version', 'unknown')}"
            f" • {str(runtime.get('commit') or 'local')[:7]}</code>\n"
            f"🎛️ <b>Mode:</b> <code>"
            f"{'DEMO' if mode.get('use_testnet') else 'LIVE'} | "
            f"execution={'ON' if mode.get('demo_execution_enabled') else 'OFF'} | "
            f"cap={mode.get('effective_position_cap', 0)}</code>\n"
            f"🔗 <b>Proposal→Entry:</b> <code>"
            f"{proposal_summary.get('telegram_trade_proposals_count', 0)}→"
            f"{proposal_summary.get('demo_entry_posts_count', 0)}</code>\n"
            f"🛡️ <b>حماية المراكز المُدارة:</b> <code>"
            f"unprotected={len(protection.get('unprotected_managed_symbols') or [])}</code>\n"
        )
        if operational.get("warnings"):
            msg += (
                "🚨 <b>تحذيرات تشغيلية:</b> <code>"
                + ", ".join(operational["warnings"])
                + "</code>\n"
            )
        msg += "\n"
        
        # معلومات الخادم
        if sys_health:
            limit_mb = sys_health.get("cgroup_limit_mb")
            current_mb = sys_health.get("cgroup_current_mb")
            headroom_mb = sys_health.get("cgroup_headroom_mb")
            msg += f"🖥️ <b>CPU العملية:</b> <code>{sys_health['process_cpu_percent']:.1f}%</code>\n"
            msg += f"🧠 <b>Process RSS:</b> <code>{sys_health['rss_mb']:.1f} MB</code>\n"
            msg += f"📈 <b>Peak RSS:</b> <code>{sys_health['peak_rss_mb']:.1f} MB</code>\n"
            if limit_mb:
                msg += (
                    f"📦 <b>ذاكرة الحاوية:</b> <code>{current_mb:.1f} / {limit_mb:.1f} MB</code>\n"
                    f"🟢 <b>Headroom:</b> <code>{headroom_mb:.1f} MB</code>\n"
                )
            else:
                msg += "📦 <b>حد ذاكرة الحاوية:</b> <code>غير متاح</code>\n"
            msg += (
                f"🧵 <b>Threads:</b> <code>{sys_health['threads']}</code> | "
                f"FDs: <code>{sys_health.get('file_descriptors')}</code>\n"
                f"🆔 <b>Run:</b> <code>{sys_health['run_id'][:12]}</code>\n"
            )
            if sys_health.get("cgroup_oom_kill_events") is not None:
                msg += (
                    "💥 <b>cgroup OOM kills (هذا التشغيل):</b> "
                    f"<code>{sys_health['cgroup_oom_kill_events']}</code>\n"
                )
            msg += f"⏱️ <b>مدة التشغيل:</b> <code>{sys_health['uptime_hours']:.1f} ساعة</code>\n\n"
        
        # معلومات الشبكة
        lat_emoji = "🟢" if conn_health['latency'] < 500 else ("🟡" if conn_health['latency'] < 1000 else "🔴")
        msg += f"🌐 <b>حالة الاتصال:</b> {conn_health['status']}\n"
        if conn_health['latency'] > 0:
            msg += f"⚡ <b>سرعة الاستجابة (Ping):</b> <code>{conn_health['latency']:.0f} ms</code> {lat_emoji}\n\n"
            
        # حالة الدورة والإحصاءات
        msg += f"📊 <b>صفقات مفتوحة:</b> <code>{active_count}</code>\n"
        msg += f"⏳ <b>فخاخ القناص:</b> <code>{virtual_count}</code>\n"
        msg += f"🔒 <b>عملات محظورة مؤقتاً:</b> <code>{locked_count}</code>\n"
        msg += (
            f"♻️ <b>Recovery:</b> <code>{'COMPLETE' if self.state_manager.get('recovery_complete', False) else 'BLOCKED'}</code>\n"
            f"🔄 <b>آخر مزامنة ناجحة:</b> <code>{self.state_manager.get('last_successful_reconciliation_at', 'غير متاح')}</code>\n"
            f"🔎 <b>آخر مسح مكتمل:</b> <code>{self.state_manager.get('last_scan_completed_at', 'غير متاح')}</code>\n"
        )
        try:
            import builtins
            mark_ws = getattr(builtins, "_global_ws_manager", None)
            candle_ws = getattr(builtins, "_global_candle_stream", None)
            if mark_ws is not None:
                stats = mark_ws.get_stats()
                msg += (
                    f"📡 <b>Mark WS:</b> <code>{'UP' if stats.get('connected') else 'DOWN'}</code> "
                    f"| symbols=<code>{stats.get('symbols_tracked', 0)}</code>\n"
                )
            if candle_ws is not None:
                stats = candle_ws.get_stats()
                msg += (
                    f"🕯️ <b>Candle WS:</b> <code>{'UP' if stats.get('connected') else 'DOWN'}</code> "
                    f"| subscriptions=<code>{stats.get('symbols', 0)}</code> "
                    f"| threads=<code>{1 if stats.get('thread_alive') else 0}</code>\n"
                )
        except Exception as exc:
            logger.debug(f"[Health] تعذر قراءة إحصاءات WebSocket: {exc}")

        try:
            from utils.hot_list_monitor import get_latest_hot_list_snapshot
            from utils.momentum_continuation_monitor import (
                get_latest_momentum_continuation_snapshot,
            )
            from utils.active_altcoin_shadow_monitor import (
                get_latest_active_altcoin_shadow_snapshot,
            )

            hot_snapshot = get_latest_hot_list_snapshot()
            momentum_snapshot = get_latest_momentum_continuation_snapshot()
            active_alt_snapshot = get_latest_active_altcoin_shadow_snapshot()
            msg += (
                f"🔥 <b>القائمة الساخنة:</b> "
                f"<code>{hot_snapshot.get('status', 'UNKNOWN')}</code> "
                f"| candidates=<code>{hot_snapshot.get('candidate_count', 0)}</code>\n"
                f"🧪 <b>Momentum Shadow:</b> "
                f"<code>{momentum_snapshot.get('status', 'UNKNOWN')}</code> "
                f"| setups=<code>{momentum_snapshot.get('active_setups', 0)}</code> "
                f"| plans=<code>{momentum_snapshot.get('active_plans', 0)}</code> "
                f"| execution=<code>OFF</code>\n"
                f"🔬 <b>SMC→Continuation paired:</b> "
                f"setups=<code>{momentum_snapshot.get('paired_active_setups', 0)}</code> "
                f"| plans=<code>{momentum_snapshot.get('paired_active_plans', 0)}</code>\n"
                f"⚡ <b>Active Altcoin Shadow:</b> "
                f"<code>{active_alt_snapshot.get('status', 'UNKNOWN')}</code> "
                f"| setups=<code>{active_alt_snapshot.get('active_setups', 0)}</code> "
                f"| plans=<code>{active_alt_snapshot.get('active_plans', 0)}</code> "
                f"| execution=<code>OFF</code>\n"
            )
        except Exception as exc:
            logger.debug(f"[Health] تعذر قراءة حالة المسارات الظلية: {exc}")
        
        virtual_orders = self.state_manager.get_virtual_orders()
        if virtual_orders:
            msg += "\n⏳ <b>تفاصيل فخاخ القناص:</b>\n"
            now = datetime.now()
            
            def _parse_dt_safe(value):
                try:
                    if isinstance(value, float) or isinstance(value, int):
                        return datetime.fromtimestamp(value)
                    return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
                except Exception:
                    return None

            for sym, vo in list(virtual_orders.items())[:5]:
                created_at = _parse_dt_safe(vo.get("created_at"))
                expires_at = _parse_dt_safe(vo.get("expires_at"))

                age_txt = "غير معروف"
                left_txt = "غير معروف"

                if created_at:
                    age_min = int((now - created_at).total_seconds() / 60)
                    age_txt = f"{age_min} دقيقة"

                if expires_at:
                    left_min = int((expires_at - now).total_seconds() / 60)
                    left_txt = f"{max(left_min, 0)} دقيقة"

                msg += (
                    f"• <code>{sym}</code> | "
                    f"عمر: <code>{age_txt}</code> | "
                    f"ينتهي بعد: <code>{left_txt}</code>\n"
                )
                
        return msg
        
    def send_health_report(self):
        """إرسال التقرير لتليجرام"""
        msg = self.generate_health_report()
        self.notifier.send_message(msg)
