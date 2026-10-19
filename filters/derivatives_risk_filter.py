import csv
import os
from datetime import datetime, timezone

import requests

from core.config import Config
from core.logger import logger


class DerivativesRiskFilter:
    """فلتر مشتقات يعتمد حصراً على بيانات Binance Futures Mainnet العامة."""

    BASE_URL = "https://fapi.binance.com"

    def __init__(self, state_manager=None, http_session=None):
        self.state_manager = state_manager
        self.http = http_session or requests
        self._last_oi_change_status = "NO_BASELINE"

    @staticmethod
    def _clean_symbol(symbol: str) -> str:
        base = str(symbol or "").upper().split(":")[0].replace("/", "")
        return base if base.endswith("USDT") else f"{base}USDT"

    def _get_json(self, path: str, params: dict, metric: str):
        try:
            response = self.http.get(f"{self.BASE_URL}{path}", params=params, timeout=7)
            if response.status_code != 200:
                logger.warning(
                    f"[DerivFilter] {metric} غير متاح: Binance Mainnet HTTP "
                    f"{response.status_code} للرمز {params.get('symbol', '?')}"
                )
                return None
            return response.json()
        except Exception as exc:
            logger.warning(f"[DerivFilter] {metric} غير متاح من Binance Mainnet: {exc}")
            return None

    def _fetch_funding_rate(self, symbol: str):
        data = self._get_json(
            "/fapi/v1/premiumIndex", {"symbol": self._clean_symbol(symbol)}, "Funding Rate"
        )
        if not isinstance(data, dict):
            return None
        try:
            return float(data["lastFundingRate"])
        except (KeyError, TypeError, ValueError):
            logger.warning(f"[DerivFilter] Funding Rate مفقود/تالف للرمز {symbol}")
            return None

    def _fetch_open_interest(self, symbol: str):
        data = self._get_json(
            "/fapi/v1/openInterest", {"symbol": self._clean_symbol(symbol)}, "Open Interest"
        )
        if not isinstance(data, dict):
            return None
        try:
            value = float(data["openInterest"])
            if value <= 0:
                raise ValueError("non-positive OI")
            return value
        except (KeyError, TypeError, ValueError):
            logger.warning(
                f"[DerivFilter] OI مفقود/صفري للرمز {symbol} — لن يُستخدم صفر زائف في القرار"
            )
            return None

    def _fetch_long_short_ratio(self, symbol: str):
        data = self._get_json(
            "/futures/data/globalLongShortAccountRatio",
            {"symbol": self._clean_symbol(symbol), "period": "15m", "limit": 1},
            "Long/Short Ratio",
        )
        if not isinstance(data, list) or not data:
            logger.warning(f"[DerivFilter] Long/Short Ratio مفقود للرمز {symbol}")
            return None
        try:
            ratio = float(data[0]["longShortRatio"])
            return ratio if ratio > 0 else None
        except (KeyError, TypeError, ValueError):
            logger.warning(f"[DerivFilter] Long/Short Ratio تالف للرمز {symbol}")
            return None

    def _calculate_oi_change(self, symbol: str, oi_now):
        if oi_now is None or self.state_manager is None:
            self._last_oi_change_status = "UNAVAILABLE"
            return None

        clean_symbol = self._clean_symbol(symbol)
        state_key = f"oi_mainnet_samples_{clean_symbol}"
        interval_ms = 15 * 60 * 1000
        slot_ms = (
            int(datetime.now(timezone.utc).timestamp() * 1000) // interval_ms
        ) * interval_ms
        lookback = max(1, int(getattr(Config, "OI_CHANGE_LOOKBACK", 4)))
        raw_samples = self.state_manager.get(state_key, [])
        samples = []
        for sample in raw_samples if isinstance(raw_samples, list) else []:
            try:
                sample_slot = int(sample.get("slot_ms"))
                sample_oi = float(sample.get("open_interest"))
                if sample_oi > 0:
                    samples.append({"slot_ms": sample_slot, "open_interest": sample_oi})
            except (AttributeError, TypeError, ValueError):
                continue

        # A forced scan in the same 15m slot replaces the observation instead
        # of pretending it is another lookback period.
        samples = [sample for sample in samples if sample["slot_ms"] != slot_ms]
        samples.append({"slot_ms": slot_ms, "open_interest": float(oi_now)})
        samples = sorted(samples, key=lambda sample: sample["slot_ms"])[-32:]
        self.state_manager.set(state_key, samples)

        target_slot = slot_ms - lookback * interval_ms
        baseline = next(
            (sample for sample in reversed(samples) if sample["slot_ms"] == target_slot),
            None,
        )
        if baseline and baseline["open_interest"] > 0:
            self._last_oi_change_status = f"OK_{lookback}_SLOTS"
            return (float(oi_now) - baseline["open_interest"]) / baseline["open_interest"]
        self._last_oi_change_status = f"NO_{lookback}_SLOT_BASELINE"
        return None

    def evaluate_risk(self, symbol: str, side: str, price: float, client=None) -> dict:
        """
        يجلب المقاييس الثلاثة بصورة مستقلة من Mainnet. فشل مقياس لا يتحول إلى
        صفر/1.0 زائف ولا يمنع التداول؛ تُطبق القاعدة الخاصة به فقط عند توفره.
        معامل client باقٍ للتوافق مع المستدعين ولا يُستخدم كمصدر بيانات.
        """
        funding_rate = self._fetch_funding_rate(symbol)
        open_interest = self._fetch_open_interest(symbol)
        oi_change_pct = self._calculate_oi_change(symbol, open_interest)
        long_short_ratio = self._fetch_long_short_ratio(symbol)

        statuses = {
            "funding": "OK" if funding_rate is not None else "UNAVAILABLE",
            "open_interest": "OK" if open_interest is not None else "UNAVAILABLE",
            "oi_change": self._last_oi_change_status,
            "long_short_ratio": "OK" if long_short_ratio is not None else "UNAVAILABLE",
        }

        if open_interest is not None:
            change_text = f"{oi_change_pct:+.2%}" if oi_change_pct is not None else "N/A (baseline)"
            logger.debug(
                f"[DerivFilter] OI Mainnet لـ {symbol}: {open_interest:.8f} | تغير: {change_text}"
            )

        decision = "ALLOW"
        reasons = []
        direction = side.upper()
        if direction == "LONG":
            direction = "BUY"
        elif direction == "SHORT":
            direction = "SELL"

        max_funding = getattr(Config, "MAX_FUNDING_RATE_ABS", 0.001)
        if funding_rate is not None and abs(funding_rate) > max_funding:
            if direction == "SELL" and funding_rate > max_funding:
                reasons.append(f"Funding موجب مرتفع ({funding_rate:.4%}) يدعم SHORT")
            elif direction == "BUY" and funding_rate > max_funding:
                decision = "BLOCK_SUGGESTED"
                reasons.append(f"Funding موجب مرتفع ({funding_rate:.4%}) — خطر Long Squeeze")
            elif direction == "SELL" and funding_rate < -max_funding:
                decision = "BLOCK_SUGGESTED"
                reasons.append(f"Funding سالب مرتفع ({funding_rate:.4%}) — خطر Short Squeeze")

        if direction == "BUY" and oi_change_pct is not None and oi_change_pct < -0.03:
            if decision != "BLOCK_SUGGESTED":
                decision = "WARNING"
            reasons.append(f"هبوط حاد في OI ({oi_change_pct:+.2%})")

        liquidation_bias = "UNKNOWN"
        max_ls = getattr(Config, "MAX_LONG_SHORT_RATIO", 2.5)
        if long_short_ratio is not None:
            liquidation_bias = "NEUTRAL"
            if long_short_ratio > max_ls:
                liquidation_bias = "LONG_LIQ_RISK"
                if direction == "BUY" and decision != "BLOCK_SUGGESTED":
                    decision = "WARNING"
                    reasons.append(f"ازدحام متطرف في الشراء (L/S={long_short_ratio:.2f})")
            elif long_short_ratio < (1.0 / max_ls):
                liquidation_bias = "SHORT_LIQ_RISK"
                if direction == "SELL" and decision != "BLOCK_SUGGESTED":
                    decision = "WARNING"
                    reasons.append(f"ازدحام متطرف في البيع (L/S={long_short_ratio:.2f})")

        available_count = sum(value == "OK" for value in statuses.values() if value != "NO_BASELINE")
        is_mock = available_count == 0
        if not reasons:
            unavailable = [name for name, status in statuses.items() if status != "OK"]
            reasons.append(
                "مؤشرات المشتقات المتاحة ضمن الحدود الآمنة"
                + (f" | غير متاح: {', '.join(unavailable)}" if unavailable else "")
            )

        return {
            "decision": decision,
            "funding_rate": funding_rate,
            "open_interest": open_interest,
            "oi_change_pct": oi_change_pct,
            "long_short_ratio": long_short_ratio,
            "liquidation_bias": liquidation_bias,
            "reason": " | ".join(reasons),
            "is_mock": is_mock,
            "data_status": statuses,
            "source": "binance_futures_mainnet",
        }

    def log_audit_result(self, symbol: str, side: str, price: float, result: dict):
        """تسجيل تدقيق المشتقات مع حالة توافر كل مقياس."""
        file_path = os.path.join(Config.DATA_DIR, "derivatives_audit_log.csv")
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
        file_exists = os.path.exists(file_path)
        headers = [
            "timestamp", "symbol", "side", "price", "decision", "funding_rate",
            "open_interest", "oi_change_pct", "long_short_ratio", "liquidation_bias",
            "reason", "is_mock", "data_status", "source",
        ]
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": side.upper(),
            "price": price,
            "decision": result["decision"],
            "funding_rate": "" if result.get("funding_rate") is None else f"{result['funding_rate']:.8f}",
            "open_interest": "" if result.get("open_interest") is None else f"{result['open_interest']:.8f}",
            "oi_change_pct": "" if result.get("oi_change_pct") is None else f"{result['oi_change_pct']:.8f}",
            "long_short_ratio": "" if result.get("long_short_ratio") is None else f"{result['long_short_ratio']:.4f}",
            "liquidation_bias": result.get("liquidation_bias", "UNKNOWN"),
            "reason": result.get("reason", ""),
            "is_mock": str(result.get("is_mock", False)),
            "data_status": str(result.get("data_status", {})),
            "source": result.get("source", "binance_futures_mainnet"),
        }
        try:
            with open(file_path, "a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row)
            logger.info(f"📊 [تدقيق مشتقات Binance Mainnet] تم تسجيل {symbol} في {file_path}")
        except Exception as exc:
            logger.error(f"فشل تسجيل تدقيق المشتقات في CSV: {exc}")
