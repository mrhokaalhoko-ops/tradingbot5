import ccxt
import pandas as pd
import time
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse
from core.config import Config
from core.logger import logger

class ExchangeClient:
    def __init__(self):
        # استخدام ccxt للاتصال بمنصة باينانس
        self.exchange = ccxt.binance({
            'apiKey': Config.BINANCE_API_KEY,
            'secret': Config.BINANCE_API_SECRET,
            'enableRateLimit': True, # احترام قيود الـ Rate Limits من باينانس لتجنب الحظر
            'timeout': 30000, # مهلة الاتصال 30 ثانية للتغلب على ضعف الشبكة اللحظي
            'options': {
                'defaultType': 'future', # العمل على سوق العقود الآجلة
                'adjustForTimeDifference': True,
                'recvWindow': 10000,
            }
        })
        
        # تفعيل Testnet (Demo Trading الجديد) في حالة كان مفعلاً في الإعدادات
        if Config.USE_TESTNET:
            try:
                # التحديث الجديد لـ ccxt يتطلب استخدام enable_demo_trading للعقود الآجلة
                self.exchange.enable_demo_trading(True)
                logger.info("تم تشغيل وضع Demo Trading الجديد لباينانس بنجاح.")
            except AttributeError:
                # كحل بديل إذا كان إصدار ccxt أقدم من 4.5.6
                self.exchange.set_sandbox_mode(True)
                logger.info("تم تشغيل وضع Sandbox (القديم) لباينانس.")

        self._environment_proof = None
        if Config.TELEGRAM_PROPOSAL_DEMO_MODE:
            self._environment_proof = self.prove_demo_environment()
            if not self._environment_proof.get("is_demo"):
                raise RuntimeError(
                    "TELEGRAM_PROPOSAL_EXECUTOR_REJECTED_NON_DEMO_ENDPOINT"
                )

        # الإصلاح 1: فحص وضع التداول مرة واحدة عند التهيئة لتجنب إرسال positionSide في One-Way Mode
        # (خطأ -4061 من باينانس إذا أُرسلت positionSide في وضع One-Way)
        self._is_hedge_mode = self._detect_hedge_mode()
        self._state_manager = None

    def bind_state_manager(self, state_manager):
        """Bind durable lifecycle state after both runtime objects exist."""
        self._state_manager = state_manager
        return self

    def prove_demo_environment(self):
        """Prove CCXT USD-M Futures routes point only at Binance Demo."""
        api_urls = (self.exchange.urls or {}).get("api") or {}
        futures_urls = []

        def _walk(value, path=""):
            if isinstance(value, dict):
                for key, child in value.items():
                    _walk(child, f"{path}.{key}" if path else str(key))
            elif isinstance(value, str) and "fapi" in path.lower():
                futures_urls.append(value)

        _walk(api_urls)
        hosts = sorted({
            urlparse(url).hostname or ""
            for url in futures_urls
            if isinstance(url, str)
        })
        is_demo = bool(hosts) and set(hosts) == {"demo-fapi.binance.com"}
        proof = {
            "is_demo": is_demo,
            "mainnet": False if is_demo else None,
            "allowed_host": "demo-fapi.binance.com",
            "observed_hosts": hosts,
            "futures_routes_checked": len(futures_urls),
            "order_calls_possible": bool(
                is_demo and Config.DEMO_EXECUTION_ENABLED
            ),
        }
        if not is_demo:
            logger.critical(f"[DemoGuard] endpoint proof failed: {proof}")
        else:
            logger.info(f"[DemoGuard] endpoint proof: {proof}")
        return proof

    def _detect_hedge_mode(self) -> bool:
        """يفحص إعداد الحساب مرة واحدة: هل الحساب في Hedge Mode أم One-Way Mode؟"""
        try:
            response = self.exchange.fapiPrivateGetPositionSideDual()
            is_hedge = response.get('dualSidePosition', False)
            mode_label = 'Hedge Mode' if is_hedge else 'One-Way Mode'
            logger.info(f"[ExchangeClient] وضع التداول المكتشف: {mode_label}")
            return bool(is_hedge)
        except Exception as e:
            assumed = getattr(Config, 'ASSUME_HEDGE_MODE', False)
            logger.warning(
                f"[ExchangeClient] تعذّر فحص وضع التداول ({e}) — "
                f"سيُفترض: {'Hedge Mode' if assumed else 'One-Way Mode'}. "
                f"تأكد من ضبط ASSUME_HEDGE_MODE في Config بشكل صحيح."
            )
            return assumed

    def fetch_ohlcv(self, symbol, timeframe='15m', limit=100):
        """
        جلب بيانات الشموع (Open, High, Low, Close, Volume)
        مع معالجة الاستثناءات في حالة فشل الاتصال.
        """
        try:
            logger.debug(f"جاري جلب بيانات الشموع لـ {symbol} بإطار {timeframe}")
            bars = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            
            df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            return df
        except ccxt.NetworkError as e:
            logger.error(f"خطأ في الشبكة أثناء جلب بيانات الشموع: {e}")
        except ccxt.ExchangeError as e:
            logger.error(f"خطأ من منصة باينانس: {e}")
        except Exception as e:
            logger.critical(f"خطأ غير متوقع في جلب الشموع: {e}")
        return None
        
    def get_balance(self):
        """
        جلب رصيد المحفظة.
        """
        try:
            balance = self.exchange.fetch_balance()
            return balance
        except ccxt.AuthenticationError as e:
            logger.error(f"خطأ في المصادقة (تأكد من الـ API Keys): {e}")
        except Exception as e:
            logger.error(f"فشل في جلب الرصيد: {e}")
        return None

    def get_top_volatile_symbols(self, limit=15):
        """
        جلب العملات الأكثر تحركاً وتذبذباً في السوق (Market Screener)
        الترتيب حسب نسبة التغيّر السعري (%) لاصطياد العملات المشتعلة فعلاً
        """
        try:
            logger.info("جاري مسح السوق للعثور على العملات الأكثر تحركاً...")
            tickers = self.exchange.fetch_tickers()
            
            # فلترة أزواج USDT فقط واستبعاد العقود المؤرخة (Delivery Contracts)
            usdt_pairs = {k: v for k, v in tickers.items() if '/USDT' in k and '-' not in k}
            
            # الإصلاح 4: رفع الحد الأدنى للسيولة ليتوافق مع MIN_24H_VOLUME_USDT من الإعدادات
            min_volume = getattr(Config, 'MIN_24H_VOLUME_USDT', 10_000_000)
            liquid_pairs = [v for v in usdt_pairs.values() if (v.get('quoteVolume') or 0) > min_volume]

            # الإصلاح 4: الترتيب بـ (volume × volatility) يوازن بين السيولة والتذبذب
            # التذبذب المطلق وحده يختار عملات بعد pump كبير (خطر دخول في القمة)
            def _score(x):
                vol = x.get('quoteVolume') or 0
                pct = abs(x.get('percentage') or 0)
                return vol * pct

            sorted_pairs = sorted(liquid_pairs, key=_score, reverse=True)
            
            top_symbols = [pair['symbol'] for pair in sorted_pairs[:limit]]
            
            # طباعة تفاصيل العملات المختارة
            for pair in sorted_pairs[:limit]:
                s = pair['symbol'].split('/')[0]
                pct = pair.get('percentage', 0) or 0
                vol = (pair.get('quoteVolume', 0) or 0) / 1_000_000
                logger.info(f"  📌 {s}: تغيّر {pct:+.2f}% | سيولة: {vol:.1f}M USDT")
            
            return top_symbols
        except Exception as e:
            logger.error(f"فشل في مسح السوق: {e}")
            return ['BTC/USDT', 'ETH/USDT']

    def get_current_price(self, symbol):
        """جلب السعر اللحظي للعملة"""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return ticker['last']
        except Exception as e:
            logger.error(f"فشل جلب السعر الحالي: {e}")
        return None

    def get_mark_price(self, symbol):
        """جلب Mark Price من عقد USD-M نفسه لتجنب خلطه مع Last Price."""
        try:
            market = self.exchange.market(symbol)
            market_id = market.get('id') if isinstance(market, dict) else None
            if not market_id:
                market_id = str(symbol).replace('/', '').replace(':USDT', '')
            response = self.exchange.fapiPublicGetPremiumIndex({'symbol': market_id})
            mark_price = float((response or {}).get('markPrice') or 0.0)
            if mark_price > 0:
                return mark_price
            raise ValueError("markPrice missing from premiumIndex response")
        except Exception as e:
            logger.error(f"فشل جلب Mark Price لـ {symbol}: {e}")
            return None

    def normalize_price(self, symbol, price):
        """تطبيع السعر حسب tickSize الفعلي للسوق بدلاً من تخمين المنازل العشرية."""
        try:
            return float(self.exchange.price_to_precision(symbol, float(price)))
        except Exception as e:
            logger.error(f"[{symbol}] تعذر تطبيع السعر {price}: {e}")
            return None

    def normalize_amount(self, symbol, amount):
        """تطبيع الكمية حسب stepSize الفعلي للسوق."""
        try:
            return float(self.exchange.amount_to_precision(symbol, float(amount)))
        except Exception as e:
            logger.error(f"[{symbol}] تعذر تطبيع الكمية {amount}: {e}")
            return None

    def get_price_tick_size(self, symbol):
        """Return the authoritative USD-M PRICE_FILTER tick size."""
        resolved_symbol, market = self._resolve_usdm_contract_market(symbol)
        if not resolved_symbol or not isinstance(market, dict):
            return None
        info = market.get("info") or {}
        for item in info.get("filters", []) or []:
            if str((item or {}).get("filterType") or "") == "PRICE_FILTER":
                tick = self._positive_rule((item or {}).get("tickSize"))
                if tick:
                    return tick
        precision = (market.get("precision") or {}).get("price")
        tick = self._positive_rule(precision)
        return tick

    def preview_order_amount(self, symbol, amount, ref_price):
        """Apply the same USD-M filters as execute_trade without any order call."""
        resolved_symbol, market = self._resolve_usdm_contract_market(symbol)
        if not resolved_symbol or not market:
            return None
        return self._validate_order_market_rules(
            resolved_symbol, amount, ref_price, market=market
        )

    def _set_and_confirm_leverage(self, symbol, leverage):
        """يؤكد الرافعة من استجابة Binance؛ غياب التأكيد يعني رفض الدخول."""
        try:
            requested = int(leverage)
        except (TypeError, ValueError):
            logger.critical(f"[{symbol}] رافعة غير صالحة: {leverage}")
            return False
        if not (int(Config.MIN_LEVERAGE) <= requested <= int(Config.MAX_LEVERAGE)):
            logger.critical(
                f"[{symbol}] الرافعة {requested}x خارج حدود البوت "
                f"({Config.MIN_LEVERAGE}-{Config.MAX_LEVERAGE}x)."
            )
            return False
        try:
            response = self.exchange.set_leverage(requested, symbol)
        except Exception as exc:
            logger.critical(
                f"[{symbol}] رُفض الدخول: فشل تعيين الرافعة {requested}x: {exc}"
            )
            return False
        if not isinstance(response, dict):
            logger.critical(
                f"[{symbol}] رُفض الدخول: Binance لم تُرجع تأكيداً منظماً للرافعة."
            )
            return False
        raw = response.get("info", {}) if isinstance(response.get("info"), dict) else {}
        confirmed = response.get("leverage", raw.get("leverage"))
        try:
            confirmed = int(confirmed)
        except (TypeError, ValueError):
            logger.critical(
                f"[{symbol}] رُفض الدخول: استجابة الرافعة لا تحتوي قيمة مؤكدة."
            )
            return False
        if confirmed != requested:
            logger.critical(
                f"[{symbol}] رُفض الدخول: المطلوب {requested}x لكن المؤكد {confirmed}x."
            )
            return False
        logger.info(f"تم تأكيد الرافعة المالية للزوج {symbol} على {confirmed}x")
        return True

    @staticmethod
    def _positive_rule(value):
        try:
            parsed = float(value)
            return parsed if parsed > 0 else None
        except (TypeError, ValueError):
            return None

    def _resolve_usdm_contract_market(self, symbol):
        """Resolve an analysis symbol to its explicit USD-M contract market.

        The scanner intentionally stores state as ``BASE/USDT`` while CCXT keys
        perpetual contracts as ``BASE/USDT:USDT``. Looking up the short symbol
        directly can therefore return Spot metadata, or no metadata at all for
        futures-only listings. Execution must never infer contract rules from
        a Spot market.
        """
        raw_symbol = str(symbol or "")
        base_symbol = raw_symbol.split(":", 1)[0]
        markets = getattr(self.exchange, "markets", None) or {}
        candidates = []

        for market_key, market in markets.items():
            if not isinstance(market, dict) or not market:
                continue
            unified_symbol = str(market.get("symbol") or market_key or "")
            if unified_symbol.split(":", 1)[0] != base_symbol:
                continue

            market_type = str(market.get("type") or "").lower()
            is_contract = bool(
                market.get("contract") is True
                or market.get("swap") is True
                or market.get("future") is True
                or market_type in {"swap", "future"}
            )
            if not is_contract:
                continue

            quote = str(
                market.get("quote") or base_symbol.rsplit("/", 1)[-1]
            ).upper()
            settle = str(market.get("settle") or "USDT").upper()
            if quote != "USDT" or settle != "USDT" or market.get("linear") is False:
                continue

            rank = (
                int(unified_symbol == raw_symbol),
                int(market_type == "swap" or market.get("swap") is True),
                int(market.get("active") is not False),
                int(market.get("linear") is True),
            )
            candidates.append((rank, unified_symbol, market))

        if not candidates:
            return None, None
        candidates.sort(key=lambda item: item[0], reverse=True)
        _, unified_symbol, market = candidates[0]
        return unified_symbol, market

    def _set_execution_failure(
        self, code, symbol, *, send_attempted=False, detail=None
    ):
        self.last_execution_failure = {
            "code": str(code or "UNKNOWN"),
            "symbol": str(symbol or ""),
            "send_attempted": bool(send_attempted),
            "detail": str(detail or ""),
        }

    def _set_protection_replacement_failure(
        self, *, symbol, side, amount, current_algo_id=None, old_sl=0.0,
        requested_sl=0.0, reason="UNKNOWN", error=None, retry_count=0,
    ):
        evidence = self._sanitize_exchange_error(error) if error else {
            "code": None, "message": "", "type": ""
        }
        self.last_protection_replacement_failure = {
            "symbol": str(symbol or ""),
            "side": str(side or "").upper(),
            "position_qty": float(amount or 0.0),
            "current_algo_id": str(current_algo_id or ""),
            "old_sl": float(old_sl or 0.0),
            "requested_sl": float(requested_sl or 0.0),
            "endpoint": "/fapi/v1/algoOrder",
            "binance_code": evidence.get("code"),
            "binance_message": evidence.get("message"),
            "error_type": evidence.get("type"),
            "retry_count": int(retry_count or 0),
            "reason": str(reason or "UNKNOWN"),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.error(
            "[ProtectionReplace] symbol=%s side=%s qty=%s algo_id=%s "
            "old_sl=%s requested_sl=%s endpoint=%s code=%s reason=%s message=%s",
            self.last_protection_replacement_failure["symbol"],
            self.last_protection_replacement_failure["side"],
            self.last_protection_replacement_failure["position_qty"],
            self.last_protection_replacement_failure["current_algo_id"],
            self.last_protection_replacement_failure["old_sl"],
            self.last_protection_replacement_failure["requested_sl"],
            self.last_protection_replacement_failure["endpoint"],
            self.last_protection_replacement_failure["binance_code"],
            self.last_protection_replacement_failure["reason"],
            self.last_protection_replacement_failure["binance_message"],
        )

    @staticmethod
    def _sanitize_exchange_error(exc):
        """Keep Binance code/message for forensics without request signatures."""
        text = str(exc or "")
        for marker in ("signature=", "X-MBX-APIKEY", "apiKey="):
            if marker in text:
                text = text.split(marker, 1)[0] + marker + "REDACTED"
        code = None
        for candidate in (
            getattr(exc, "code", None),
            (getattr(exc, "response", None) or {}).get("code")
            if isinstance(getattr(exc, "response", None), dict) else None,
        ):
            try:
                code = int(candidate)
                break
            except (TypeError, ValueError):
                pass
        if code is None:
            import re
            match = re.search(r"(?:code[\"']?\s*[:=]\s*|\b)(-\d{3,6})\b", text)
            if match:
                code = int(match.group(1))
        return {"code": code, "message": text[:500], "type": type(exc).__name__}

    @classmethod
    def _is_definitive_exchange_reject(cls, exc):
        evidence = cls._sanitize_exchange_error(exc)
        code = evidence.get("code")
        if isinstance(code, int) and code < 0 and code not in {-1001, -1007, -1008}:
            return True, evidence
        name = str(evidence.get("type") or "").lower()
        message = str(evidence.get("message") or "").lower()
        transport = any(token in name or token in message for token in (
            "timeout", "network", "connection", "temporarily unavailable",
            "gateway timeout", "service unavailable",
        ))
        explicit = any(token in name for token in (
            "invalidorder", "insufficientfunds", "authenticationerror",
            "permissiondenied", "badrequest",
        ))
        return bool(explicit and not transport), evidence

    @staticmethod
    def _raw_market_filter(market, filter_type):
        for item in ((market or {}).get("info") or {}).get("filters", []) or []:
            if str((item or {}).get("filterType") or "").upper() == filter_type:
                return item or {}
        return {}

    def _validate_order_market_rules(self, symbol, amount, ref_price, market=None):
        """تطبيع وفحص قواعد Binance؛ أي metadata ناقصة تفشل مغلقة."""
        resolved_symbol = symbol
        if market is None:
            resolved_symbol, market = self._resolve_usdm_contract_market(symbol)
        else:
            resolved_symbol = str(market.get("symbol") or symbol)
        if not isinstance(market, dict) or not market:
            self._set_execution_failure(
                "MARKET_METADATA_UNAVAILABLE", symbol, send_attempted=False
            )
            logger.critical(f"[{symbol}] رُفض الدخول: market metadata غير متاحة.")
            return None
        if market.get("active") is False:
            self._set_execution_failure(
                "MARKET_INACTIVE", symbol, send_attempted=False
            )
            logger.critical(f"[{symbol}] رُفض الدخول: السوق غير نشط على Binance.")
            return None

        normalized_amount = self.normalize_amount(resolved_symbol, amount)
        if normalized_amount is None or normalized_amount <= 0:
            self._set_execution_failure(
                "MARKET_AMOUNT_INVALID", symbol, send_attempted=False,
                detail=f"requested={amount}",
            )
            logger.critical(f"[{symbol}] رُفض الدخول: الكمية بعد التطبيع غير صالحة.")
            return None

        limits = market.get("limits")
        if not isinstance(limits, dict):
            self._set_execution_failure(
                "MARKET_LIMITS_UNAVAILABLE", symbol, send_attempted=False
            )
            logger.critical(f"[{symbol}] رُفض الدخول: limits metadata غير متاحة.")
            return None
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}
        lot = self._raw_market_filter(market, "LOT_SIZE")
        market_lot = self._raw_market_filter(market, "MARKET_LOT_SIZE")
        min_candidates = [
            self._positive_rule(amount_limits.get("min")),
            self._positive_rule(lot.get("minQty")),
            self._positive_rule(market_lot.get("minQty")),
        ]
        min_candidates = [value for value in min_candidates if value is not None]
        min_qty = max(min_candidates) if min_candidates else None
        min_notional = self._positive_rule(cost_limits.get("min"))

        # Binance USD-M تنشر LOT_SIZE وMIN_NOTIONAL. غيابهما غالباً يعني أن
        # load_markets/cache لم يكتمل، وليس تصريحاً بتجاوز الفحص.
        if min_qty is None or min_notional is None:
            self._set_execution_failure(
                "MARKET_MIN_RULES_UNAVAILABLE", symbol, send_attempted=False,
                detail=f"minQty={min_qty};minNotional={min_notional}",
            )
            logger.critical(
                f"[{symbol}] رُفض الدخول: minQty/minNotional غير موثوقين في metadata."
            )
            return None
        if normalized_amount < min_qty:
            self._set_execution_failure(
                "MARKET_MIN_QTY_NOT_MET", symbol, send_attempted=False,
                detail=f"requested={normalized_amount};minQty={min_qty}",
            )
            logger.warning(
                f"[{symbol}] الكمية {normalized_amount:.8f} أقل من الحد الأدنى {min_qty}."
            )
            return None
        max_candidates = [
            self._positive_rule(amount_limits.get("max")),
            self._positive_rule(lot.get("maxQty")),
            self._positive_rule(market_lot.get("maxQty")),
        ]
        max_candidates = [value for value in max_candidates if value is not None]
        max_qty = min(max_candidates) if max_candidates else None
        if max_qty is not None and normalized_amount > max_qty:
            logger.warning(
                f"[{symbol}] الكمية {normalized_amount:.8f} أعلى من الحد الأقصى {max_qty}."
            )
            self._set_execution_failure(
                "MARKET_MAX_QTY_EXCEEDED",
                symbol,
                send_attempted=False,
                detail=(
                    f"requested={normalized_amount};maxQty={max_qty};"
                    f"lot={lot.get('maxQty')};marketLot={market_lot.get('maxQty')}"
                ),
            )
            return None
        ref_price = self._positive_rule(ref_price)
        if ref_price is None:
            self._set_execution_failure(
                "MARKET_REFERENCE_PRICE_UNAVAILABLE", symbol, send_attempted=False
            )
            logger.critical(f"[{symbol}] رُفض الدخول: لا يوجد سعر حديث لفحص notional.")
            return None
        notional = normalized_amount * ref_price
        if notional < min_notional:
            self._set_execution_failure(
                "MARKET_MIN_NOTIONAL_NOT_MET", symbol, send_attempted=False,
                detail=f"notional={notional};minNotional={min_notional}",
            )
            logger.warning(
                f"[{symbol}] قيمة الصفقة {notional:.2f} أقل من الحد الأدنى "
                f"{min_notional} USDT."
            )
            return None
        max_notional = self._positive_rule(cost_limits.get("max"))
        if max_notional is not None and notional > max_notional:
            self._set_execution_failure(
                "MARKET_MAX_NOTIONAL_EXCEEDED", symbol, send_attempted=False,
                detail=f"notional={notional};maxNotional={max_notional}",
            )
            logger.warning(
                f"[{symbol}] قيمة الصفقة {notional:.2f} أعلى من الحد الأقصى "
                f"{max_notional} USDT."
            )
            return None
        return normalized_amount
            
    @staticmethod
    def _normalize_algo_order(raw):
        """تحويل استجابة Binance Algo إلى الشكل الموحد المستخدم داخلياً."""
        if not isinstance(raw, dict) or not raw:
            return None
        info = dict(raw)
        info["_algo_order"] = True
        return {
            "id": str(raw.get("algoId") or raw.get("orderId") or ""),
            "clientOrderId": str(
                raw.get("clientAlgoId") or raw.get("clientOrderId") or ""
            ),
            "status": str(raw.get("algoStatus") or raw.get("status") or ""),
            "type": str(raw.get("orderType") or raw.get("type") or "").lower(),
            "side": str(raw.get("side") or "").lower(),
            "stopPrice": float(
                raw.get("triggerPrice") or raw.get("stopPrice") or 0
            ),
            "actualOrderId": str(raw.get("actualOrderId") or ""),
            "info": info,
        }

    def _find_algo_order_by_client_id(self, symbol, client_order_id, quiet=False):
        if not client_order_id:
            return None
        try:
            market = self.exchange.market(symbol)
            raw = self.exchange.fapiPrivateGetAlgoOrder({
                "symbol": market["id"],
                "clientAlgoId": client_order_id,
            })
            return self._normalize_algo_order(raw)
        except Exception as exc:
            if not quiet:
                logger.warning(
                    f"[AlgoOrderRecovery] تعذر استرجاع {client_order_id} لـ {symbol}: {exc}"
                )
            return None

    def resolve_algo_order_by_client_id(self, symbol, client_order_id):
        """Resolve one conditional Algo order without collapsing uncertainty.

        A timeout after POST may mean Binance accepted the order.  Only a
        successful signed Algo query can prove it exists, and only Binance's
        explicit not-found response can prove it does not.  Transport/parser
        failures remain UNKNOWN and must never trigger a blind POST retry.
        """
        if not client_order_id:
            return {
                "status": "UNKNOWN", "order": None,
                "reason": "MISSING_CLIENT_ALGO_ID",
            }
        try:
            market = self.exchange.market(symbol)
            raw = self.exchange.fapiPrivateGetAlgoOrder({
                "symbol": market["id"],
                "clientAlgoId": str(client_order_id),
            })
            order = self._normalize_algo_order(raw)
            if not isinstance(order, dict) or not order:
                return {
                    "status": "UNKNOWN", "order": None,
                    "reason": "UNPARSEABLE_ALGO_ORDER_RESPONSE",
                }
            return {
                "status": "FOUND", "order": order,
                "reason": "ALGO_ORDER_QUERY_CONFIRMED",
            }
        except Exception as exc:
            code = self._exchange_error_code(exc)
            if code == -2013:
                return {
                    "status": "NOT_FOUND", "order": None,
                    "reason": "BINANCE_ALGO_ORDER_DOES_NOT_EXIST", "code": code,
                }
            logger.warning(
                f"[AlgoOrderRecovery] حالة الأمر {client_order_id} لـ {symbol} "
                f"غير معروفة: {exc}"
            )
            return {
                "status": "UNKNOWN", "order": None,
                "reason": type(exc).__name__, "code": code,
            }

    def _find_order_by_client_id(self, symbol, client_order_id):
        """استرجاع الأمر بعد timeout، مع أولوية واجهة Algo لأوامر SL/TP."""
        if not client_order_id:
            return None

        algo_order = self._find_algo_order_by_client_id(
            symbol, client_order_id, quiet=True
        )
        if algo_order:
            return algo_order
        resolution = self.resolve_order_by_client_id(symbol, client_order_id)
        return resolution.get("order") if resolution.get("status") == "FOUND" else None

    @staticmethod
    def _exchange_error_code(exc):
        """Extract a Binance numeric code without treating text as proof."""
        for candidate in (
            getattr(exc, "code", None),
            getattr(exc, "error_code", None),
        ):
            try:
                if candidate is not None:
                    return int(candidate)
            except (TypeError, ValueError):
                pass
        text = str(exc or "")
        for token in ("-2013", "-2011"):
            if token in text:
                return int(token)
        return None

    def resolve_order_by_client_id(self, symbol, client_order_id):
        """Resolve a standard futures order without collapsing UNKNOWN to absent.

        Binance code -2013 is definitive for a query that reached the venue.
        Network/timeout/5xx/parser failures remain UNKNOWN and callers must keep
        their durable intent instead of retrying or deleting it blindly.
        """
        if not client_order_id:
            return {
                "status": "UNKNOWN",
                "order": None,
                "reason": "MISSING_CLIENT_ORDER_ID",
            }
        try:
            market = self.exchange.market(symbol)
            raw = self.exchange.fapiPrivateGetOrder({
                "symbol": market["id"],
                "origClientOrderId": str(client_order_id),
            })
            if not isinstance(raw, dict):
                return {
                    "status": "UNKNOWN", "order": None,
                    "reason": "NON_OBJECT_ORDER_RESPONSE",
                }
            parsed = self.exchange.parse_order(raw, market)
            if not isinstance(parsed, dict):
                return {
                    "status": "UNKNOWN", "order": None,
                    "reason": "UNPARSEABLE_ORDER_RESPONSE",
                }
            return {
                "status": "FOUND", "order": parsed,
                "reason": "ORDER_QUERY_CONFIRMED",
            }
        except Exception as exc:
            code = self._exchange_error_code(exc)
            if code == -2013:
                return {
                    "status": "NOT_FOUND", "order": None,
                    "reason": "BINANCE_ORDER_DOES_NOT_EXIST", "code": code,
                }
            logger.warning(
                f"[OrderRecovery] حالة الأمر {client_order_id} لـ {symbol} "
                f"غير معروفة: {exc}"
            )
            return {
                "status": "UNKNOWN", "order": None,
                "reason": type(exc).__name__, "code": code,
            }

    def _fetch_position_protection_orders(self, symbol, position_side=None):
        """جلب أوامر الحماية مع تمييز الفشل عن نتيجة فارغة.

        لا تكون النتيجة كاملة إلا بعد نجاح مصدر Algo ومصدر الأوامر العادية.
        قد تكون الحماية موزعة بين المصدرين أثناء migration/recovery، لذلك لا
        يجوز اعتبار نجاح أحدهما دليلاً على عدم وجود أوامر في الآخر.
        """
        requested_side = str(position_side or "").upper()
        orders = []
        seen = set()
        algo_fetch_succeeded = False
        regular_fetch_succeeded = False
        legacy_stop_fetch_succeeded = False
        source_errors = {}

        try:
            market = self.exchange.market(symbol)
            raw_orders = self.exchange.fapiPrivateGetOpenAlgoOrders({
                "symbol": market["id"],
            })
            if isinstance(raw_orders, list):
                algo_fetch_succeeded = True
                for raw in raw_orders:
                    order = self._normalize_algo_order(raw)
                    if order:
                        orders.append(order)
            else:
                source_errors["algo"] = "NON_LIST_RESPONSE"
        except Exception as exc:
            logger.warning(f"[{symbol}] تعذر جلب أوامر Algo المفتوحة: {exc}")
            source_errors["algo"] = f"{type(exc).__name__}:{exc}"

        # الاستعلام العام إلزامي حتى عند وجود أوامر Algo، لأنه قد يحتوي أوامر
        # عادية/قديمة لن تظهر في Algo API. استعلام stop القديم best-effort فقط
        # للتوافق مع حسابات ونسخ CCXT الأقدم.
        try:
            fetched = self.exchange.fetch_open_orders(symbol, params={})
            if isinstance(fetched, list):
                regular_fetch_succeeded = True
                orders.extend(fetched)
            else:
                source_errors["regular"] = "NON_LIST_RESPONSE"
        except Exception as exc:
            logger.warning(f"[{symbol}] تعذر جلب الأوامر العادية المفتوحة: {exc}")
            source_errors["regular"] = f"{type(exc).__name__}:{exc}"

        try:
            fetched = self.exchange.fetch_open_orders(symbol, params={"stop": True})
            if isinstance(fetched, list):
                legacy_stop_fetch_succeeded = True
                orders.extend(fetched)
            else:
                source_errors["legacy_stop"] = "NON_LIST_RESPONSE"
        except Exception as exc:
            # Binance الحديثة قد ترفض هذا المسار بعد نقل conditional orders
            # إلى Algo API؛ لذلك لا يؤثر فشله على اكتمال المصدرين authoritative.
            source_errors["legacy_stop"] = f"{type(exc).__name__}:{exc}"
            logger.debug(f"[{symbol}] فشل مسار legacy stop: {exc}")

        complete = algo_fetch_succeeded and regular_fetch_succeeded

        filtered = []
        for order in orders:
            if not self._is_protection_order(order):
                continue
            if self._is_hedge_mode:
                order_side = self._order_position_side(order)
                if not requested_side or order_side != requested_side:
                    continue
            info = order.get("info", {}) or {}
            identity = str(
                info.get("algoId")
                or order.get("id")
                or info.get("clientAlgoId")
                or order.get("clientOrderId")
                or ""
            )
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            filtered.append(order)

        return {
            "status": "OK" if complete else "UNKNOWN",
            "complete": complete,
            "source_status": {
                "algo": "OK" if algo_fetch_succeeded else "UNKNOWN",
                "regular": "OK" if regular_fetch_succeeded else "UNKNOWN",
                "legacy_stop": "OK" if legacy_stop_fetch_succeeded else "OPTIONAL_UNKNOWN",
            },
            "source_errors": source_errors,
            "orders": filtered,
            "sl_orders": [o for o in filtered if self._is_stop_market_order(o)],
            "tp_orders": [
                o for o in filtered
                if "TAKE_PROFIT" in self._order_type(o)
            ],
        }

    def get_position_protection_orders(self, symbol, position_side=None):
        return self._fetch_position_protection_orders(symbol, position_side)

    @staticmethod
    def _order_type(order):
        info = order.get("info", {}) or {}
        return str(
            info.get("orderType")
            or info.get("type")
            or order.get("type")
            or ""
        ).upper().replace("-", "_")

    @staticmethod
    def _order_trigger_price(order):
        info = order.get("info", {}) or {}
        try:
            return float(
                info.get("triggerPrice")
                or info.get("stopPrice")
                or order.get("stopPrice")
                or 0
            )
        except Exception:
            return 0.0

    def _cancel_protection_order(self, symbol, order):
        info = order.get("info", {}) or {}
        algo_id = info.get("algoId") if info.get("_algo_order") else None
        if algo_id:
            market = self.exchange.market(symbol)
            self.exchange.fapiPrivateDeleteAlgoOrder({
                "symbol": market["id"],
                "algoId": algo_id,
            })
            return True
        self.exchange.cancel_order(order["id"], symbol, params={"stop": True})
        return True

    def execute_trade(self, symbol, side, amount, price=None, current_price=None,
                      leverage=None, market_type=None, client_order_id=None):
        """تنفيذ صفقة حقيقية على المنصة وتحديد الرافعة المالية صراحةً"""
        self.last_execution_failure = None
        send_attempted = False
        # Defense in depth: observation-only Telegram mode may analyze and
        # publish proposals, but no caller may reach leverage mutation or an
        # Entry POST while Demo execution is disabled.
        if (
            Config.TELEGRAM_PROPOSAL_DEMO_MODE
            and not Config.DEMO_EXECUTION_ENABLED
        ):
            self._set_execution_failure(
                "DEMO_EXECUTION_DISABLED",
                symbol,
                send_attempted=False,
            )
            logger.info(
                f"[{symbol}] Entry blocked by observation-only Demo guard."
            )
            return None
        try:
            exchange_symbol, contract_market = self._resolve_usdm_contract_market(symbol)
            if not exchange_symbol or not contract_market:
                self._set_execution_failure(
                    "USDM_CONTRACT_METADATA_UNAVAILABLE",
                    symbol,
                    send_attempted=False,
                )
                logger.critical(
                    f"[{symbol}] رُفض الدخول: metadata عقد USD-M الدائم غير متاحة."
                )
                return None

            # ── تعيين الرافعة قبل التنفيذ ─────────────────────────────────
            trade_leverage = leverage if leverage is not None else Config.LEVERAGE
            if not self._set_and_confirm_leverage(exchange_symbol, trade_leverage):
                self._set_execution_failure(
                    "LEVERAGE_NOT_CONFIRMED", symbol, send_attempted=False
                )
                return None

            # ── إصلاح: التحقق من minQty / stepSize / minNotional قبل الإرسال ─
            # باينانس ترفض الأوامر صمتاً إذا لم تتطابق مع قيود السوق
            try:
                amount = self._validate_order_market_rules(
                    exchange_symbol,
                    amount,
                    price or current_price,
                    market=contract_market,
                )
            except Exception as exc:
                self._set_execution_failure(
                    "MARKET_RULE_VALIDATION_ERROR",
                    symbol,
                    send_attempted=False,
                    detail=type(exc).__name__,
                )
                logger.critical(
                    f"[{symbol}] رُفض الدخول: فشل فحص قيود السوق: {exc}"
                )
                return None
            if amount is None:
                if not self.last_execution_failure:
                    self._set_execution_failure(
                        "MARKET_RULE_VALIDATION_FAILED", symbol, send_attempted=False
                    )
                return None

            order_type = 'market' if not price else 'limit'
            logger.info(f"إرسال أمر {order_type.upper()} {side.upper()} | كمية={amount:.6f} | {symbol}")

            # positionSide في Hedge Mode فقط (تجنب -4061 في One-Way)
            if self._is_hedge_mode:
                position_side = 'LONG' if side.lower() == 'buy' else 'SHORT'
                params = {'positionSide': position_side}
            else:
                params = {}

            if client_order_id:
                safe_client_id = ''.join(
                    ch for ch in str(client_order_id)
                    if ch.isalnum() or ch in ('_', '-')
                )[:36]
                params['newClientOrderId'] = safe_client_id
            else:
                safe_client_id = None

            send_attempted = True
            order = self.exchange.create_order(
                exchange_symbol, order_type, side, amount, price, params
            )
            acknowledged_filled = float(order.get("filled") or 0.0)
            logger.info(
                f"✅ استجابة Binance للأمر: {order.get('id')} | "
                f"status={order.get('status')} | executed={acknowledged_filled:.6f}"
            )

            # Slippage Monitoring
            if order_type == 'market' and current_price:
                actual_price = order.get('average') or order.get('price')
                if actual_price and actual_price > 0:
                    slippage = abs(actual_price - current_price) / current_price
                    if slippage > 0.01:
                        logger.critical(
                            f"[Slippage] ⚠️ انزلاق {slippage:.2%} في {symbol}! "
                            f"المتوقع={current_price} الفعلي={actual_price}"
                        )
            return order
        except Exception as e:
            logger.error(f"فشل تنفيذ الصفقة لـ {symbol}: {e}")
            definitive_reject, rejection = self._is_definitive_exchange_reject(e)
            self._set_execution_failure(
                (
                    "ENTRY_REJECTED_BY_EXCHANGE"
                    if send_attempted and definitive_reject
                    else "ORDER_SUBMISSION_UNRESOLVED"
                    if send_attempted else "LOCAL_PRE_SUBMIT_ERROR"
                ),
                symbol,
                send_attempted=send_attempted,
                detail=(
                    f"code={rejection.get('code')};message={rejection.get('message')}"
                ),
            )
            if definitive_reject:
                return None
            # Timeout قد يعني أن Matching Engine نفذ الأمر رغم غياب الرد.
            # لا نعيد None قبل محاولة الاسترجاع بمعرف idempotency.
            recovered = self._find_order_by_client_id(
                locals().get("exchange_symbol") or symbol,
                locals().get('safe_client_id'),
            )
            if recovered:
                self.last_execution_failure = None
                logger.critical(
                    f"[OrderRecovery] ✅ استُعيد أمر {symbol} بعد خطأ الإرسال؛ "
                    f"order={recovered.get('id')} status={recovered.get('status')}"
                )
                return recovered
            return None

    def get_position_status(self, symbol, position_side=None):
        """
        يعيد حالة مركز صريحة بدلاً من خلط فشل الشبكة مع عدم وجود مركز.

        status:
        - OPEN: يوجد مركز فعلي.
        - CLOSED: نجح الاستعلام ولا يوجد مركز.
        - UNKNOWN: تعذر الاستعلام، ولا يجوز اعتباره مركزاً مغلقاً.
        """
        try:
            positions = self.exchange.fetch_positions([symbol])
            requested_side = str(position_side or "").upper()
            matches = []

            for position in positions or []:
                info = position.get('info', {}) or {}
                raw_amt = float(info.get('positionAmt', 0) or 0)
                if raw_amt == 0:
                    continue

                raw_side = str(
                    info.get('positionSide')
                    or position.get('side')
                    or ('LONG' if raw_amt > 0 else 'SHORT')
                ).upper()
                if requested_side and requested_side not in ("BOTH", raw_side):
                    continue

                matches.append({
                    "amount": abs(raw_amt),
                    "signed_amount": raw_amt,
                    "position_side": raw_side,
                    "entry_price": float(
                        position.get('entryPrice')
                        or info.get('entryPrice', 0)
                        or 0
                    ),
                    "raw": position,
                })

            if matches:
                return {
                    "status": "OPEN",
                    "amount": sum(item["amount"] for item in matches),
                    "positions": matches,
                }

            return {"status": "CLOSED", "amount": 0.0, "positions": []}
        except Exception as e:
            logger.error(f"فشل التحقق من حالة المركز لـ {symbol}: {e}")
            return {
                "status": "UNKNOWN",
                "amount": None,
                "positions": [],
                "error": str(e),
            }

    def has_open_position(self, symbol, position_side=None):
        """True للمفتوح، False للمغلق، وNone عندما تكون حالة Binance غير معروفة."""
        status = self.get_position_status(symbol, position_side=position_side)
        if status["status"] == "UNKNOWN":
            return None
        return status["status"] == "OPEN"

    @staticmethod
    def _order_actual_id(order):
        if not order:
            return None
        info = order.get("info", {}) or {}
        value = order.get("actualOrderId") or info.get("actualOrderId")
        return str(value) if value not in (None, "", "0", 0) else None

    def _create_close_position_protection(
        self, symbol, side, order_type, trigger_price, client_order_id
    ):
        close_side = "SELL" if side.lower() == "buy" else "BUY"
        position_side = "LONG" if side.lower() == "buy" else "SHORT"
        market = self.exchange.market(symbol)
        params = {
            "algoType": "CONDITIONAL",
            "symbol": market["id"],
            "side": close_side,
            "type": order_type,
            "triggerPrice": trigger_price,
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
            "clientAlgoId": client_order_id,
        }
        if self._is_hedge_mode:
            params["positionSide"] = position_side
        raw = self.exchange.fapiPrivatePostAlgoOrder(params)
        normalized = self._normalize_algo_order(raw)
        if not normalized or not normalized.get("id"):
            raise RuntimeError("BINANCE_ALGO_PROTECTION_ACK_INVALID")
        return normalized

    def _place_or_recover_protection(
        self, symbol, side, order_type, trigger_price, client_order_id
    ):
        try:
            order = self._create_close_position_protection(
                symbol, side, order_type, trigger_price, client_order_id
            )
            return True, order
        except Exception as exc:
            logger.error(f"فشل وضع {order_type} لـ {symbol}: {exc}")
            order = self._find_order_by_client_id(symbol, client_order_id)
            terminal = ("canceled", "cancelled", "rejected", "expired")
            if order and str(order.get("status", "")).lower() not in terminal:
                logger.warning(
                    f"[{symbol}] استُعيد {order_type} الموجود بعد خطأ الطلب: {order.get('id')}"
                )
                return True, order
            return False, None

    def _protection_result(self, sl_order, tp_order, sl_client_id, tp_client_id):
        return {
            "sl_ok": bool(sl_order),
            "tp_ok": bool(tp_order),
            "sl_order_id": sl_order.get("id") if sl_order else None,
            "tp_order_id": tp_order.get("id") if tp_order else None,
            "sl_actual_order_id": self._order_actual_id(sl_order),
            "tp_actual_order_id": self._order_actual_id(tp_order),
            "sl_client_id": sl_client_id if sl_order else None,
            "tp_client_id": tp_client_id if tp_order else None,
            "working_type": "MARK_PRICE",
            "sl_price": self._order_trigger_price(sl_order) if sl_order else None,
            "tp_price": self._order_trigger_price(tp_order) if tp_order else None,
        }

    def place_sl_tp(self, symbol, side, amount, sl_price, tp_price, client_order_prefix=None):
        """وضع SL وTP كاملين مع استرداد أوامر Algo بالمعرّف الصحيح."""
        sl_price = self.normalize_price(symbol, sl_price)
        tp_price = self.normalize_price(symbol, tp_price)
        if sl_price is None or tp_price is None:
            return {"sl_ok": False, "tp_ok": False, "reason": "invalid_price_precision"}

        safe_prefix = "".join(
            ch for ch in str(client_order_prefix or f"bot_{int(time.time() * 1000)}")
            if ch.isalnum() or ch in ("_", "-")
        )[:24]
        sl_client_id = f"{safe_prefix}_sl"[:36]
        tp_client_id = f"{safe_prefix}_tp"[:36]

        sl_ok, sl_order = self._place_or_recover_protection(
            symbol, side, "STOP_MARKET", sl_price, sl_client_id
        )
        if sl_ok:
            logger.info(f"✅ تم وضع Stop Loss عند {sl_price} | رقم الأمر: {sl_order.get('id')}")

        tp_ok, tp_order = self._place_or_recover_protection(
            symbol, side, "TAKE_PROFIT_MARKET", tp_price, tp_client_id
        )
        if tp_ok:
            logger.info(f"✅ تم وضع Take Profit عند {tp_price} | رقم الأمر: {tp_order.get('id')}")

        return self._protection_result(
            sl_order if sl_ok else None,
            tp_order if tp_ok else None,
            sl_client_id,
            tp_client_id,
        )

    def ensure_position_protection(
        self, symbol, side, amount, sl_price, tp_price, client_order_prefix=None
    ):
        """يتبنى الحماية الموجودة ويُنشئ الجزء المفقود فقط.

        إذا تعذر الاستعلام نعيد UNKNOWN ولا نرسل أمراً مكرراً ولا نغلق المركز
        بناء على حالة لا نعرفها.
        """
        position_side = "LONG" if side.lower() == "buy" else "SHORT"
        snapshot = self.get_position_protection_orders(
            symbol, position_side if self._is_hedge_mode else None
        )
        if snapshot["status"] == "UNKNOWN":
            return {"sl_ok": False, "tp_ok": False, "status": "UNKNOWN"}

        safe_prefix = "".join(
            ch for ch in str(client_order_prefix or f"bot_{int(time.time() * 1000)}")
            if ch.isalnum() or ch in ("_", "-")
        )[:24]
        sl_client_id = f"{safe_prefix}_sl"[:36]
        tp_client_id = f"{safe_prefix}_tp"[:36]
        sl_order = snapshot["sl_orders"][0] if snapshot["sl_orders"] else None
        tp_order = snapshot["tp_orders"][0] if snapshot["tp_orders"] else None

        if not sl_order:
            sl_price = self.normalize_price(symbol, sl_price)
            if sl_price is not None:
                ok, sl_order = self._place_or_recover_protection(
                    symbol, side, "STOP_MARKET", sl_price, sl_client_id
                )
                if not ok:
                    sl_order = None
        if not tp_order:
            tp_price = self.normalize_price(symbol, tp_price)
            if tp_price is not None:
                ok, tp_order = self._place_or_recover_protection(
                    symbol, side, "TAKE_PROFIT_MARKET", tp_price, tp_client_id
                )
                if not ok:
                    tp_order = None

        result = self._protection_result(
            sl_order, tp_order,
            str(sl_order.get("clientOrderId") or sl_client_id) if sl_order else sl_client_id,
            str(tp_order.get("clientOrderId") or tp_client_id) if tp_order else tp_client_id,
        )
        result["status"] = "OK"
        return result

    def place_full_sl(self, symbol, side, sl_price):
        """
        وضع Stop Loss كامل يغلق أي كمية متبقية من الصفقة.
        يستخدم closePosition=True + MARK_PRICE لتجنب الـ wick الكاذبة.
        """
        sl_price = self.normalize_price(symbol, sl_price)
        if sl_price is None:
            return None
        close_side    = "sell" if side.lower() == "buy" else "buy"
        position_side = "LONG" if side.lower() == "buy" else "SHORT"

        try:
            params = {
                "stopPrice":     sl_price,
                "closePosition": True,
                "workingType":   "MARK_PRICE",  # يمنع تفعيل SL على wick مؤقت
                "timeInForce":   "GTE_GTC",
            }
            if self._is_hedge_mode:
                params["positionSide"] = position_side

            order = self.exchange.create_order(
                symbol, "STOP_MARKET", close_side, None, None, params
            )

            logger.info(f"✅ تم وضع SL كامل عند {sl_price} (MARK_PRICE) | order={order.get('id')}")
            return order

        except Exception as e:
            logger.error(f"فشل وضع SL كامل لـ {symbol}: {e}")
            return None

    def cancel_all_orders(self, symbol):
        """إلغاء كافة الأوامر المفتوحة للعملة (مثل الهدف والستوب)"""
        standard_ok = True
        conditional_ok = True
        try:
            self.exchange.cancel_all_orders(symbol)
        except Exception as e:
            standard_ok = False
            logger.warning(f"فشل إلغاء الأوامر العادية للزوج {symbol}: {e}")

        # يجب محاولة أوامر Algo الشرطية صراحة حتى لو فشل مسار الأوامر العادية.
        try:
            snapshot = self._fetch_position_protection_orders(symbol)
            if snapshot["status"] == "UNKNOWN":
                raise RuntimeError("تعذر التحقق من أوامر Algo المفتوحة")
            for order in snapshot["orders"]:
                self._cancel_protection_order(symbol, order)
        except Exception as e:
            conditional_ok = False
            logger.warning(f"⚠️ [cancel_all_orders] فشل إلغاء أوامر الستوب الشرطية لـ {symbol}: {e}")

        if standard_ok and conditional_ok:
            logger.info(f"تم إلغاء كافة الأوامر المعلقة للزوج {symbol}.")
        return standard_ok and conditional_ok

    def close_position(self, symbol, side, amount):
        """Close with a durable idempotent saga and keep protection until zero."""
        close_side = 'sell' if side.lower() == 'buy' else 'buy'
        position_side = 'LONG' if side.lower() == 'buy' else 'SHORT'
        verify_side = position_side if self._is_hedge_mode else None
        state_manager = getattr(self, "_state_manager", None)
        intent = {}

        def _archive(terminal_status, proofs=None):
            if state_manager is None:
                return False
            return state_manager.archive_close_intent(
                symbol, position_side, terminal_status, proofs=proofs
            )

        def _transition(expected, new_status, updates=None):
            if state_manager is None:
                return True
            return state_manager.transition_close_intent(
                symbol, position_side, expected, new_status, updates=updates
            )

        def _cleanup_after_zero():
            if self._is_hedge_mode:
                cleanup_ok = self.cancel_position_protection_orders(symbol, position_side)
            else:
                cleanup_ok = self.cancel_all_orders(symbol)
            if not cleanup_ok:
                logger.critical(
                    f"[{symbol}] أُغلق المركز لكن تعذر تنظيف أوامر الحماية؛ "
                    "راجع الأوامر المفتوحة على Binance."
                )
            return cleanup_ok

        try:
            # Resolve a previous generation before sending anything again.
            if state_manager is not None:
                intent = state_manager.get_close_intent(symbol, position_side)
                if intent:
                    live = self.get_position_status(symbol, position_side=verify_side)
                    if live.get("status") == "CLOSED":
                        _archive(
                            "ZERO_CONFIRMED",
                            {"position_status": "CLOSED", "resolution": "PRE_SEND"},
                        )
                        _cleanup_after_zero()
                        return True
                    if live.get("status") == "UNKNOWN":
                        _transition(None, "UNKNOWN", {"retry_after": time.time() + 15})
                        logger.error(
                            f"[{symbol}] نية إغلاق سابقة وحالة المركز UNKNOWN؛ "
                            "لن يُرسل أمر مكرر."
                        )
                        return False

                    resolution = self.resolve_order_by_client_id(
                        symbol, intent.get("client_order_id")
                    )
                    resolution_status = str(
                        resolution.get("status") or "UNKNOWN"
                    ).upper()
                    if resolution_status == "UNKNOWN":
                        _transition(
                            None, "UNKNOWN",
                            {"retry_after": time.time() + 15,
                             "resolution_reason": resolution.get("reason")},
                        )
                        return False
                    if resolution_status == "FOUND":
                        order = resolution.get("order") or {}
                        info = order.get("info") if isinstance(order.get("info"), dict) else {}
                        venue_status = str(
                            info.get("status") or order.get("status") or "UNKNOWN"
                        ).upper()
                        try:
                            executed = float(
                                order.get("filled")
                                or info.get("executedQty")
                                or 0
                            )
                        except (TypeError, ValueError):
                            executed = 0.0
                        if venue_status in {
                            "CANCELED", "CANCELLED", "REJECTED", "EXPIRED",
                            "EXPIRED_IN_MATCH",
                        } and executed <= 0:
                            _archive(
                                "FAILED_DEFINITIVE",
                                {"venue_status": venue_status, "executed_qty": 0.0},
                            )
                            intent = {}
                        elif venue_status in {"FILLED", "CLOSED"} and executed > 0:
                            remaining = float(live.get("amount") or 0.0)
                            _archive(
                                "PARTIAL_CLOSE_CONFIRMED",
                                {"venue_status": venue_status,
                                 "executed_qty": executed,
                                 "remaining_qty": remaining},
                            )
                            amount = remaining
                            intent = {}
                        else:
                            _transition(
                                None,
                                "PARTIAL" if executed > 0 else "SUBMITTED",
                                {"executed_qty": executed,
                                 "venue_status": venue_status,
                                 "remaining_qty": float(live.get("amount") or 0.0),
                                 "retry_after": time.time() + 10},
                            )
                            return False
                    elif resolution_status == "NOT_FOUND":
                        age = time.time() - float(intent.get("created_at") or time.time())
                        if age < 120:
                            _transition(
                                None, "UNKNOWN",
                                {"retry_after": time.time() + 15,
                                 "resolution_reason": "RECENT_NOT_FOUND"},
                            )
                            return False
                        _archive(
                            "NOT_FOUND_POSITION_STILL_OPEN",
                            {"age_seconds": age, "position_status": "OPEN"},
                        )
                        intent = {}

                if not intent:
                    meta = state_manager.get_entry_metadata(symbol) or {}
                    intent = state_manager.prepare_close_intent(
                        symbol=symbol,
                        position_side=position_side,
                        side=close_side,
                        amount=amount,
                        position_id=meta.get("trade_id"),
                        reason=meta.get("pending_close_reason"),
                    )
                    if not intent:
                        logger.critical(
                            f"[{symbol}] رُفض الإغلاق: تعذر حفظ close intent قبل Binance."
                        )
                        return False

            if not amount or float(amount) <= 0:
                logger.critical(f"[{symbol}] رُفض الإغلاق بكمية غير صالحة: {amount}")
                return False

            # نبقي SL/TP قائمين أثناء إرسال الإغلاق؛ لا نترك فجوة حماية إذا فشل.
            params = {'positionSide': position_side} if self._is_hedge_mode else {'reduceOnly': True}
            if intent.get("client_order_id"):
                params["newClientOrderId"] = intent["client_order_id"]
            logger.warning(f"🚨 جاري إغلاق الصفقة للزوج {symbol} بسعر السوق فوراً...")
            try:
                order = self.exchange.create_order(
                    symbol, 'market', close_side, amount, None, params
                )
            except Exception as exc:
                _transition(
                    ("PREPARED", "SUBMITTED", "UNKNOWN", "PARTIAL"),
                    "UNKNOWN",
                    {"last_error": str(exc), "retry_after": time.time() + 15},
                )
                # A timeout may follow acceptance. Query once, but never resend
                # blindly in this call; the next call resumes the same intent.
                resolution = self.resolve_order_by_client_id(
                    symbol, intent.get("client_order_id")
                ) if intent.get("client_order_id") else {"status": "UNKNOWN"}
                if resolution.get("status") == "FOUND":
                    _transition(
                        None, "SUBMITTED",
                        {"exchange_order_id": (resolution.get("order") or {}).get("id"),
                         "resolution_source": "QUERY_AFTER_SEND_ERROR"},
                    )
                logger.critical(
                    f"[{symbol}] إرسال الإغلاق عاد بحالة غير معروفة؛ "
                    "حُفظت النية ولن يُكرر الأمر بلا حسم: {exc}"
                )
                return False

            _transition(
                ("PREPARED", "UNKNOWN", "PARTIAL"),
                "SUBMITTED",
                {"exchange_order_id": order.get("id") if isinstance(order, dict) else None,
                 "venue_status": order.get("status") if isinstance(order, dict) else None},
            )

            # لا نعلن النجاح قبل أن تؤكد Binance أن المركز أصبح صفراً.
            for _ in range(4):
                status = self.get_position_status(symbol, position_side=verify_side)
                if status['status'] == 'CLOSED':
                    _archive(
                        "ZERO_CONFIRMED",
                        {"position_status": "CLOSED",
                         "exchange_order_id": order.get("id") if isinstance(order, dict) else None},
                    )
                    _cleanup_after_zero()
                    logger.info(f"تم إغلاق الصفقة {symbol} وتأكيد صفر المركز بنجاح.")
                    return True
                if status['status'] == 'UNKNOWN':
                    _transition(
                        None, "UNKNOWN",
                        {"retry_after": time.time() + 15,
                         "resolution_reason": "POSITION_QUERY_UNKNOWN"},
                    )
                    logger.error(
                        f"تعذر تأكيد إغلاق {symbol} بعد إرسال أمر السوق؛ ستبقى الحالة قيد المراقبة."
                    )
                    return False
                time.sleep(0.5)

            _transition(
                None, "PARTIAL",
                {"remaining_qty": float(status.get("amount") or 0.0),
                 "retry_after": time.time() + 10},
            )
            logger.critical(
                f"أُرسل أمر إغلاق {symbol} لكن المركز ما زال مفتوحاً جزئياً/كلياً. "
                "لن تُلغى الحماية ولن تُنظف الحالة."
            )
            return False
        except Exception as e:
            _transition(
                None, "UNKNOWN",
                {"last_error": str(e), "retry_after": time.time() + 15},
            )
            logger.critical(f"فشل ذريع في إغلاق الصفقة {symbol}! التدخل اليدوي مطلوب فوراً: {e}")
            return False

    def get_position_amount(self, symbol, position_side=None):
        """
        جلب كمية المركز. يعيد None عند فشل الاستعلام حتى لا يُفسر انقطاع API
        على أنه إغلاق حقيقي.
        """
        status = self.get_position_status(symbol, position_side=position_side)
        if status["status"] == "UNKNOWN":
            return None
        return float(status.get("amount", 0.0) or 0.0)

    @staticmethod
    def _order_position_side(order):
        info = order.get('info', {}) or {}
        return str(
            info.get('positionSide')
            or order.get('positionSide')
            or order.get('position_side')
            or ''
        ).upper()

    @staticmethod
    def _is_protection_order(order):
        order_type = ExchangeClient._order_type(order)
        return 'STOP' in order_type or 'TAKE_PROFIT' in order_type

    def cancel_position_protection_orders(self, symbol, position_side):
        """إلغاء أوامر حماية ساق واحدة فقط في Hedge Mode."""
        requested_side = str(position_side or '').upper()
        if not self._is_hedge_mode:
            return self.cancel_all_orders(symbol)
        if requested_side not in ('LONG', 'SHORT'):
            logger.critical(
                f"[{symbol}] رفض إلغاء الحماية: positionSide غير صالح ({requested_side or 'EMPTY'})"
            )
            return False

        try:
            snapshot = self._fetch_position_protection_orders(symbol, requested_side)
            if snapshot["status"] == "UNKNOWN":
                logger.error(f"[{symbol}] لم ينجح أي استعلام لأوامر حماية {requested_side}.")
                return False

            success = True
            for order in snapshot["orders"]:
                order_side = self._order_position_side(order)
                if not order_side:
                    logger.critical(
                        f"[{symbol}] أمر حماية {order.get('id')} بلا positionSide؛ "
                        "لن يُلغى في Hedge Mode حفاظاً على الساق المقابلة."
                    )
                    success = False
                    continue
                if order_side != requested_side:
                    continue
                try:
                    self._cancel_protection_order(symbol, order)
                    logger.debug(f"[{symbol}] أُلغي أمر حماية {requested_side}: {order['id']}")
                except Exception as exc:
                    success = False
                    logger.error(f"[{symbol}] فشل إلغاء حماية {requested_side} {order.get('id')}: {exc}")
            return success
        except Exception as exc:
            logger.error(f"[{symbol}] فشل إلغاء حماية ساق {requested_side}: {exc}")
            return False

    def cancel_sl_orders(self, symbol, position_side=None):
        """إلغاء جميع أوامر الستوب المفتوحة للعملة (تُستخدم لتحديث الستوب المتحرك)"""
        try:
            requested_side = str(position_side or '').upper()
            if self._is_hedge_mode and requested_side not in ('LONG', 'SHORT'):
                logger.critical(
                    f"[{symbol}] رفض إلغاء SL في Hedge Mode دون positionSide صريح."
                )
                return False
            snapshot = self._fetch_position_protection_orders(
                symbol, requested_side if self._is_hedge_mode else None
            )
            if snapshot["status"] == "UNKNOWN":
                return False
            for order in snapshot["sl_orders"]:
                self._cancel_protection_order(symbol, order)
                logger.debug(f"تم إلغاء أمر الستوب القديم {order.get('id')}")
            return True
        except Exception as e:
            logger.error(f"فشل إلغاء أوامر الستوب لـ {symbol}: {e}")
            return False

    @staticmethod
    def _is_stop_market_order(order):
        order_type = ExchangeClient._order_type(order)
        return order_type == 'STOP_MARKET'

    def _submit_or_resolve_protection(
        self, symbol, side, order_type, trigger_price, client_order_id
    ):
        """Submit once and preserve UNKNOWN instead of collapsing it into failure."""
        try:
            order = self._create_close_position_protection(
                symbol, side, order_type, trigger_price, client_order_id
            )
            return {"status": "ACKNOWLEDGED", "order": order}
        except Exception as exc:
            resolution = self.resolve_algo_order_by_client_id(
                symbol, client_order_id
            )
            if resolution.get("status") == "FOUND":
                order = resolution.get("order") or {}
                venue_status = str(order.get("status") or "").upper()
                if venue_status not in {
                    "CANCELED", "CANCELLED", "REJECTED", "EXPIRED",
                    "EXPIRED_IN_MATCH",
                }:
                    return {"status": "ACKNOWLEDGED", "order": order}
                return {
                    "status": "FAILED_DEFINITIVE", "order": order,
                    "reason": venue_status,
                }
            if resolution.get("status") == "NOT_FOUND":
                return {
                    "status": "FAILED_DEFINITIVE", "order": None,
                    "reason": "ORDER_NOT_FOUND", "error": str(exc),
                }
            return {
                "status": "UNKNOWN", "order": None,
                "reason": resolution.get("reason"), "error": str(exc),
            }

    @staticmethod
    def _stop_is_at_least_as_protective(side, observed, target):
        observed = float(observed or 0.0)
        target = float(target or 0.0)
        if min(observed, target) <= 0:
            return False
        epsilon = max(abs(target) * 1e-10, 1e-12)
        return (
            observed >= target - epsilon
            if str(side).lower() == "buy"
            else observed <= target + epsilon
        )

    @staticmethod
    def _stop_prices_equal(first, second):
        first = float(first or 0.0)
        second = float(second or 0.0)
        if min(first, second) <= 0:
            return False
        return abs(first - second) <= max(abs(second) * 1e-10, 1e-12)

    def recover_sl_replacement(self, symbol, saga=None):
        """Resume one persisted SL replacement without ever blind-resubmitting."""
        state_manager = getattr(self, "_state_manager", None)
        if state_manager is None:
            return False
        if not isinstance(saga, dict) or not saga:
            for position_side in ("LONG", "SHORT"):
                candidate = state_manager.get_protection_replacement(
                    symbol, position_side
                )
                if candidate:
                    saga = candidate
                    break
        if not isinstance(saga, dict) or not saga:
            return False

        symbol = saga.get("symbol") or symbol
        side = str(saga.get("side") or "").lower()
        position_side = str(saga.get("position_side") or "").upper()
        amount = float(saga.get("amount") or 0.0)
        old_stop = float(saga.get("old_stop") or 0.0)
        new_stop = float(saga.get("new_stop") or 0.0)
        if side not in ("buy", "sell") or position_side not in ("LONG", "SHORT"):
            logger.critical(f"[{symbol}] SL replacement state غير صالح؛ أُوقفت الاستعادة.")
            return False

        def _transition(phase, updates=None):
            return state_manager.transition_protection_replacement(
                symbol, position_side, None, phase, updates=updates
            )

        def _archive(phase, proofs=None):
            return state_manager.archive_protection_replacement(
                symbol, position_side, phase, proofs=proofs
            )

        def _verify_snapshot():
            return self._fetch_position_protection_orders(
                symbol, position_side if self._is_hedge_mode else None
            )

        def _confirm_new_stop(order, observed_price):
            """Persist only a stop proven by an exchange read response."""
            if not self._stop_is_at_least_as_protective(side, observed_price, new_stop):
                _transition(
                    "BLOCKED_STOP_MISMATCH",
                    {
                        "observed_stop": observed_price,
                        "requested_stop": new_stop,
                        "retry_after": time.time() + 30,
                    },
                )
                return False
            meta = state_manager.get_entry_metadata(symbol) or {}
            if meta:
                meta.setdefault("initial_sl", float(meta.get("sl") or observed_price))
                meta["current_sl"] = observed_price
                meta["current_sl_confirmed_at"] = datetime.now(timezone.utc).isoformat()
                meta["sl_order_id"] = order.get("id")
                if not state_manager.save_entry_metadata(symbol, meta):
                    _transition(
                        "NEW_VERIFIED_STATE_PENDING",
                        {"verified_stop": observed_price, "retry_after": time.time() + 5},
                    )
                    return False
            if not _archive(
                "NEW_VERIFIED",
                {"verified_stop": observed_price, "order_id": order.get("id")},
            ):
                return False
            return observed_price

        def _emergency_close(reason):
            if str(saga.get("phase") or "") == "EMERGENCY_CLOSE_REQUESTED":
                return False
            if not _transition("EMERGENCY_CLOSE_REQUESTED", {"reason": reason}):
                return False
            logger.critical(
                f"[{symbol}] تعذر إثبات/استعادة SL؛ إرسال إغلاق طوارئ durable "
                f"({reason})."
            )
            self.close_position(symbol, side, amount)
            return False

        retry_after = float(saga.get("retry_after") or 0.0)
        if retry_after > time.time():
            return False
        if str(saga.get("phase") or "") in {
            "EMERGENCY_CLOSE_REQUESTED", "UNPROTECTED_CRITICAL",
            "BLOCKED_MULTIPLE_STOPS", "BLOCKED_STOP_MISMATCH",
        }:
            return False

        live = self.get_position_status(
            symbol,
            position_side=position_side if self._is_hedge_mode else None,
        )
        if live.get("status") == "CLOSED":
            _archive("POSITION_CLOSED", {"position_status": "CLOSED"})
            return True
        if live.get("status") == "UNKNOWN":
            _transition("WAIT_POSITION", {"retry_after": time.time() + 15})
            return False
        if amount <= 0:
            amount = float(live.get("amount") or 0.0)

        snapshot = _verify_snapshot()
        if snapshot.get("status") == "UNKNOWN":
            self._set_protection_replacement_failure(
                symbol=symbol, side=side, amount=amount,
                old_sl=old_stop, requested_sl=new_stop,
                reason="PROTECTION_QUERY_UNKNOWN",
                retry_count=int(saga.get("retry_count") or 0),
            )
            _transition("WAIT_SNAPSHOT", {"retry_after": time.time() + 15})
            return False
        sl_orders = snapshot.get("sl_orders") or []
        phase = str(saga.get("phase") or "")
        if len(sl_orders) > 1:
            _transition(
                "BLOCKED_MULTIPLE_STOPS",
                {
                    "observed_order_ids": [str(order.get("id")) for order in sl_orders],
                    "retry_after": time.time() + 30,
                },
            )
            logger.critical(
                f"[{symbol}] وُجد أكثر من SL للجهة {position_side}; "
                "لن يُلغى أي أمر آلياً قبل حسم التعارض."
            )
            return False

        # A fresh open-orders snapshot is already authoritative proof.  Use it
        # before client-id recovery so an eventually-consistent query endpoint
        # cannot hide a stop that the open Algo order feed has confirmed.
        if len(sl_orders) == 1:
            observed_snapshot_price = self._order_trigger_price(sl_orders[0])
            if self._stop_is_at_least_as_protective(
                side, observed_snapshot_price, new_stop
            ):
                return _confirm_new_stop(sl_orders[0], observed_snapshot_price)

        # A prior POST may have been accepted even when the local response was
        # lost.  On ACK/UNKNOWN recovery we query the deterministic client id;
        # we never issue the protection POST again blindly.
        pending_new_phases = {
            "NEW_ACKNOWLEDGED", "NEW_STATUS_UNKNOWN", "PROTECTION_UNCERTAIN",
            "WAIT_NEW_STATUS", "NEW_VERIFIED_STATE_PENDING",
        }
        pending_new_resolution = None
        if phase in pending_new_phases:
            pending_new_resolution = self.resolve_algo_order_by_client_id(
                symbol, saga.get("new_client_order_id")
            )
            resolution_status = str(pending_new_resolution.get("status") or "UNKNOWN")
            if resolution_status == "FOUND":
                resolved_order = pending_new_resolution.get("order") or {}
                venue_status = str(resolved_order.get("status") or "").upper()
                if venue_status in {
                    "CANCELED", "CANCELLED", "REJECTED", "EXPIRED",
                    "EXPIRED_IN_MATCH",
                }:
                    pending_new_resolution = {"status": "NOT_FOUND"}
                else:
                    resolved_price = self._order_trigger_price(resolved_order) or new_stop
                    if sl_orders:
                        listed_ids = {str(order.get("id")) for order in sl_orders}
                        resolved_id = str(resolved_order.get("id"))
                        listed_prices = {
                            self._order_trigger_price(order) for order in sl_orders
                        }
                        if resolved_id not in listed_ids and resolved_price not in listed_prices:
                            _transition(
                                "BLOCKED_MULTIPLE_STOPS",
                                {
                                    "observed_order_ids": sorted(listed_ids | {resolved_id}),
                                    "retry_after": time.time() + 30,
                                },
                            )
                            return False
                    return _confirm_new_stop(resolved_order, resolved_price)
            if str(pending_new_resolution.get("status") or "UNKNOWN") == "UNKNOWN":
                _transition(
                    "PROTECTION_UNCERTAIN",
                    {
                        "last_error": pending_new_resolution.get("reason"),
                        "retry_after": time.time() + 15,
                    },
                )
                return False
            # The exchange authoritatively says the attempted new stop does
            # not exist.  If the old stop is still present, retain it and end
            # the upgrade as a safe NOOP rather than cancelling it again.
            if sl_orders:
                observed_price = self._order_trigger_price(sl_orders[0])
                _archive(
                    "UPGRADE_NOT_APPLIED_OLD_PROTECTED",
                    {"verified_stop": observed_price, "order_id": sl_orders[0].get("id")},
                )
                return observed_price

        pending_restore_phases = {
            "RESTORE_ACKNOWLEDGED", "RESTORE_STATUS_UNKNOWN",
            "WAIT_RESTORE_STATUS",
        }
        if phase in pending_restore_phases and not sl_orders:
            resolution = self.resolve_algo_order_by_client_id(
                symbol, saga.get("restore_client_order_id")
            )
            resolution_status = str(resolution.get("status") or "UNKNOWN")
            if resolution_status == "FOUND":
                restored_order = resolution.get("order") or {}
                restored_price = self._order_trigger_price(restored_order) or old_stop
                if self._stop_prices_equal(restored_price, old_stop):
                    _archive(
                        "RESTORED_OLD",
                        {"verified_stop": restored_price, "order_id": restored_order.get("id")},
                    )
                    return restored_price
                _transition(
                    "BLOCKED_STOP_MISMATCH",
                    {"observed_stop": restored_price, "requested_stop": old_stop},
                )
                return False
            if resolution_status == "UNKNOWN":
                _transition(
                    "RESTORE_STATUS_UNKNOWN",
                    {"last_error": resolution.get("reason"), "retry_after": time.time() + 15},
                )
                return False
            return _emergency_close("RESTORE_NOT_FOUND")

        if len(sl_orders) == 1:
            observed_price = self._order_trigger_price(sl_orders[0])
            if (
                str(saga.get("phase") or "").startswith("RESTORE_")
                and self._stop_prices_equal(observed_price, old_stop)
            ):
                _archive(
                    "RESTORED_OLD",
                    {
                        "verified_stop": observed_price,
                        "order_id": sl_orders[0].get("id"),
                    },
                )
                return observed_price
            # Persist the dangerous transition before the first cancel call.
            if not _transition(
                "CANCEL_IN_PROGRESS",
                {
                    "observed_old_order_id": sl_orders[0].get("id"),
                    "observed_old_stop": observed_price,
                },
            ):
                return False
            try:
                self._cancel_protection_order(symbol, sl_orders[0])
            except Exception as exc:
                self._set_protection_replacement_failure(
                    symbol=symbol, side=side, amount=amount,
                    current_algo_id=sl_orders[0].get("id"),
                    old_sl=observed_price, requested_sl=new_stop,
                    reason="OLD_SL_CANCEL_FAILED", error=exc,
                    retry_count=int(saga.get("retry_count") or 0),
                )
                _transition(
                    "CANCEL_STATUS_UNKNOWN",
                    {"last_error": str(exc), "retry_after": time.time() + 10},
                )
                return False
            if not _transition("OLD_CANCEL_REQUESTED"):
                return False
            snapshot = _verify_snapshot()
            if snapshot.get("status") == "UNKNOWN":
                _transition("WAIT_OLD_ABSENCE", {"retry_after": time.time() + 10})
                return False
            if snapshot.get("sl_orders"):
                _transition("WAIT_OLD_ABSENCE", {"retry_after": time.time() + 10})
                return False

        if not _transition("OLD_ABSENT"):
            return False
        submission = (
            {"status": "FAILED_DEFINITIVE", "reason": "PRIOR_POST_NOT_FOUND"}
            if pending_new_resolution is not None
            else self._submit_or_resolve_protection(
                symbol,
                side,
                "STOP_MARKET",
                new_stop,
                saga.get("new_client_order_id"),
            )
        )
        if submission["status"] == "UNKNOWN":
            self._set_protection_replacement_failure(
                symbol=symbol, side=side, amount=amount,
                old_sl=old_stop, requested_sl=new_stop,
                reason="NEW_SL_STATUS_UNKNOWN",
                error=RuntimeError(submission.get("error") or submission.get("reason") or "unknown"),
                retry_count=int(saga.get("retry_count") or 0),
            )
            _transition(
                "NEW_STATUS_UNKNOWN",
                {"last_error": submission.get("error"), "retry_after": time.time() + 15},
            )
            return False
        if submission["status"] == "ACKNOWLEDGED":
            _transition(
                "NEW_ACKNOWLEDGED",
                {
                    "new_order_id": (submission.get("order") or {}).get("id"),
                    "retry_after": time.time() + 2,
                },
            )
            # A fresh snapshot on the next monitor tick is the terminal proof.
            return False

        if old_stop > 0:
            self._set_protection_replacement_failure(
                symbol=symbol, side=side, amount=amount,
                old_sl=old_stop, requested_sl=new_stop,
                reason="NEW_SL_DEFINITIVE_FAILURE",
                error=RuntimeError(submission.get("error") or submission.get("reason") or "failed"),
                retry_count=int(saga.get("retry_count") or 0),
            )
            restore = self._submit_or_resolve_protection(
                symbol,
                side,
                "STOP_MARKET",
                old_stop,
                saga.get("restore_client_order_id"),
            )
            if restore["status"] == "ACKNOWLEDGED":
                _transition(
                    "RESTORE_ACKNOWLEDGED",
                    {
                        "restore_order_id": (restore.get("order") or {}).get("id"),
                        "retry_after": time.time() + 2,
                    },
                )
                return False
            if restore["status"] == "UNKNOWN":
                _transition(
                    "RESTORE_STATUS_UNKNOWN",
                    {"last_error": restore.get("error"), "retry_after": time.time() + 15},
                )
                return False

        return _emergency_close("NEW_AND_RESTORE_FAILED")

    def recover_pending_protection_replacements(self):
        """Resume all persisted replacements; returns a fail-closed summary."""
        state_manager = getattr(self, "_state_manager", None)
        if state_manager is None:
            return {"status": "OK", "pending": [], "resolved": []}
        pending = []
        resolved = []
        for key, saga in state_manager.get_protection_replacements().items():
            lock = self._get_durable_sl_replace_lock(
                saga.get("symbol"), saga.get("side")
            )
            with lock:
                result = self.recover_sl_replacement(saga.get("symbol"), saga=saga)
            if result:
                resolved.append(key)
            else:
                pending.append(key)
        return {
            "status": "OK" if not pending else "BLOCKED",
            "pending": pending,
            "resolved": resolved,
        }

    def _get_durable_sl_replace_lock(self, symbol, side):
        if not hasattr(self, "_durable_sl_replace_locks"):
            self._durable_sl_replace_locks = {}
            self._durable_sl_replace_guard = threading.Lock()
        with self._durable_sl_replace_guard:
            return self._durable_sl_replace_locks.setdefault(
                f"{symbol}|{str(side).lower()}", threading.RLock()
            )

    def _start_durable_sl_replacement(
        self, symbol, side, amount, normalized_price, state_manager
    ):
        position_side = "LONG" if side.lower() == "buy" else "SHORT"
        existing = state_manager.get_protection_replacement(
            symbol, position_side
        )
        if existing:
            return self.recover_sl_replacement(symbol, saga=existing)
        snapshot = self._fetch_position_protection_orders(
            symbol, position_side if self._is_hedge_mode else None
        )
        if snapshot.get("status") == "UNKNOWN":
            return False
        old_orders = snapshot.get("sl_orders") or []
        old_prices = [self._order_trigger_price(order) for order in old_orders]
        old_prices = [price for price in old_prices if price > 0]
        if old_orders and len(old_prices) != len(old_orders):
            logger.critical(
                f"[{symbol}] لا يمكن حفظ saga: SL قديم بلا triggerPrice موثوق."
            )
            return False
        protected_price = (
            max(old_prices) if side.lower() == "buy" else min(old_prices)
        ) if old_prices else 0.0
        if protected_price and not self._stop_is_at_least_as_protective(
            side, normalized_price, protected_price
        ):
            return protected_price
        old_refs = [{
            "id": order.get("id"),
            "client_order_id": order.get("clientOrderId")
            or (order.get("info", {}) or {}).get("clientOrderId"),
            "trigger_price": self._order_trigger_price(order),
        } for order in old_orders]
        saga = state_manager.prepare_protection_replacement(
            symbol=symbol,
            position_side=position_side,
            side=side,
            amount=amount,
            old_orders=old_refs,
            old_stop=protected_price,
            new_stop=normalized_price,
        )
        if not saga:
            logger.critical(
                f"[{symbol}] رُفض تحديث SL: تعذر حفظ replacement saga."
            )
            return False
        return self.recover_sl_replacement(symbol, saga=saga)

    def replace_sl_order_state(self, symbol, side, amount, sl_price):
        """Return an explicit state for one protection replacement request.

        ``replace_sl_order`` historically returned ``False`` both for a
        definitive failure and for a durable saga that had merely reached
        ``NEW_ACKNOWLEDGED`` and was waiting for a fresh exchange snapshot.
        Callers therefore counted safe asynchronous progress as failure and
        could freeze their local trailing state while Binance later confirmed
        the new stop.  This adapter preserves the legacy method for external
        callers while giving runtime managers a non-ambiguous contract.
        """
        normalized = self.normalize_price(symbol, sl_price)
        if normalized is None:
            return {
                "state": "FAILED", "confirmed_stop": None,
                "requested_stop": sl_price, "reason": "INVALID_PRICE_PRECISION",
            }
        raw = self.replace_sl_order(symbol, side, amount, normalized)
        if raw:
            confirmed = float(raw)
            state = "CONFIRMED" if self._stop_prices_equal(confirmed, normalized) else "NOOP_PROTECTED"
            return {
                "state": state, "confirmed_stop": confirmed,
                "requested_stop": normalized, "reason": None,
            }

        state_manager = getattr(self, "_state_manager", None)
        position_side = "LONG" if str(side).lower() == "buy" else "SHORT"
        saga = (
            state_manager.get_protection_replacement(symbol, position_side)
            if state_manager is not None else None
        )
        if isinstance(saga, dict) and saga:
            phase = str(saga.get("phase") or "UNKNOWN")
            failed_phases = {
                "BLOCKED_MULTIPLE_STOPS", "EMERGENCY_CLOSE_REQUESTED",
                "UNPROTECTED_CRITICAL", "BLOCKED_STOP_MISMATCH",
            }
            return {
                "state": "FAILED" if phase in failed_phases else "PENDING_VERIFICATION",
                "confirmed_stop": None,
                "requested_stop": normalized,
                "reason": phase,
                "retry_after": saga.get("retry_after"),
                "saga_id": saga.get("replacement_id") or saga.get("id"),
            }

        failure = getattr(self, "last_protection_replacement_failure", None) or {}
        return {
            "state": "FAILED", "confirmed_stop": None,
            "requested_stop": normalized,
            "reason": failure.get("reason") or "NO_DURABLE_SAGA_AND_NOT_CONFIRMED",
        }

    def replace_sl_order(self, symbol, side, amount, sl_price):
        """
        يستبدل SL تسلسلياً وفق قيد Binance لأوامر closePosition.

        Binance يرفض إنشاء وقف closePosition ثانٍ للجهة نفسها؛ لذلك نتحقق من
        الوقف القديم، نلغيه، ثم ننشئ الجديد فوراً. إذا فشل الجديد نعيد الوقف
        السابق مباشرة. قفل لكل رمز وbackoff يمنعان Trailing وStepTrail من
        إرسال آلاف الطلبات المتعارضة.
        """
        self.last_protection_replacement_failure = None
        normalized_price = self.normalize_price(symbol, sl_price)
        if normalized_price is None:
            return False

        state_manager = getattr(self, "_state_manager", None)
        if state_manager is not None:
            durable_lock = self._get_durable_sl_replace_lock(symbol, side)
            with durable_lock:
                return self._start_durable_sl_replacement(
                    symbol, side, amount, normalized_price, state_manager
                )

        if not hasattr(self, "_sl_replace_locks"):
            self._sl_replace_locks = {}
            self._sl_replace_state = {}
            self._sl_replace_guard = threading.Lock()
        with self._sl_replace_guard:
            lock = self._sl_replace_locks.setdefault(symbol, threading.Lock())

        with lock:
            runtime = self._sl_replace_state.setdefault(symbol, {})
            if time.time() < float(runtime.get("retry_after", 0) or 0):
                logger.debug(f"[{symbol}] تأجيل تحديث SL بسبب backoff سابق.")
                return False

            position_side = "LONG" if side.lower() == "buy" else "SHORT"
            snapshot = self._fetch_position_protection_orders(
                symbol, position_side if self._is_hedge_mode else None
            )
            if snapshot["status"] == "UNKNOWN":
                logger.error(
                    f"[{symbol}] حالة SL القديم غير معروفة؛ لن نغيّر الحماية."
                )
                runtime["retry_after"] = time.time() + float(
                    getattr(Config, "SL_REPLACE_BACKOFF_SECONDS", 30)
                )
                return False

            old_stop_orders = snapshot["sl_orders"]
            old_prices = [
                self._order_trigger_price(order) for order in old_stop_orders
            ]
            old_prices = [price for price in old_prices if price > 0]

            if old_stop_orders and len(old_prices) != len(old_stop_orders):
                logger.critical(
                    f"[{symbol}] يوجد SL قديم بلا triggerPrice موثوق؛ رُفض الاستبدال."
                )
                return False

            if old_prices:
                protected_price = (
                    max(old_prices) if side.lower() == "buy" else min(old_prices)
                )
                improves = (
                    normalized_price > protected_price
                    if side.lower() == "buy"
                    else normalized_price < protected_price
                )
                if not improves:
                    logger.debug(
                        f"[{symbol}] تجاهل SL={normalized_price}; الوقف الحالي "
                        f"{protected_price} أكثر حماية."
                    )
                    return protected_price
            else:
                protected_price = None

            cancelled = []
            try:
                for order in old_stop_orders:
                    self._cancel_protection_order(symbol, order)
                    cancelled.append(order)
            except Exception as exc:
                logger.error(f"[{symbol}] فشل إلغاء SL القديم؛ أُلغي الاستبدال: {exc}")
                remaining = self._fetch_position_protection_orders(
                    symbol, position_side if self._is_hedge_mode else None
                )
                protection_uncertain = bool(cancelled) and (
                    remaining["status"] != "OK" or not remaining["sl_orders"]
                )
                if protection_uncertain:
                    restored = bool(
                        protected_price
                        and self.place_sl_only(symbol, side, amount, protected_price)
                    )
                    if not restored:
                        logger.critical(
                            f"[{symbol}] تعذر ضمان/استعادة SL القديم بعد فشل الإلغاء؛ "
                            "سيُرسل إغلاق طوارئ."
                        )
                        emergency_closed = self.close_position(
                            symbol, side.lower(), float(amount or 0.0)
                        )
                        if not emergency_closed:
                            logger.critical(
                                f"[{symbol}] فشل إغلاق الطوارئ بعد فشل إلغاء SL؛ "
                                "تدخل يدوي مطلوب فوراً."
                            )
                runtime["retry_after"] = time.time() + float(
                    getattr(Config, "SL_REPLACE_BACKOFF_SECONDS", 30)
                )
                return False

            if self.place_sl_only(symbol, side, amount, normalized_price):
                runtime.clear()
                logger.info(f"[{symbol}] تم استبدال SL عند {normalized_price}")
                return normalized_price

            restored = bool(
                protected_price
                and self.place_sl_only(symbol, side, amount, protected_price)
            )
            runtime["retry_after"] = time.time() + float(
                getattr(Config, "SL_REPLACE_BACKOFF_SECONDS", 30)
            )
            if restored:
                logger.critical(
                    f"[{symbol}] فشل SL الجديد {normalized_price}؛ أُعيد القديم عند {protected_price}."
                )
            else:
                logger.critical(
                    f"[{symbol}] فشل SL الجديد وتعذرت استعادة القديم؛ "
                    "سيُرسل إغلاق طوارئ لتجنب مركز بلا حماية."
                )
                emergency_closed = self.close_position(
                    symbol, side.lower(), float(amount or 0.0)
                )
                if not emergency_closed:
                    logger.critical(
                        f"[{symbol}] فشل إغلاق الطوارئ أيضاً؛ تدخل يدوي مطلوب فوراً."
                    )
            return False

    def place_sl_only(self, symbol, side, amount, sl_price, client_order_id=None):
        """وضع SL واحد بمعرّف idempotent قابل للاسترداد بعد timeout."""
        sl_price = self.normalize_price(symbol, sl_price)
        if sl_price is None:
            return False
        if not client_order_id:
            price_tag = str(sl_price).replace(".", "")[-8:]
            client_order_id = f"slr_{int(time.time() * 1000)}_{price_tag}"[:36]
        ok, sl_order = self._place_or_recover_protection(
            symbol,
            side,
            "STOP_MARKET",
            sl_price,
            client_order_id,
        )
        if ok:
            logger.debug(f"✅ تم وضع SL عند {sl_price} (MARK_PRICE) | {symbol}")
            return True
        return False

    def _fetch_funding_income(self, symbol, since_ms=None):
        """يجمع Funding Fee خلال عمر الصفقة؛ الفشل يبقى UNKNOWN ولا يتحول لصفر موثوق."""
        try:
            market = self.exchange.market(symbol)
            cursor = int(since_ms) if since_ms else None
            max_pages = int(getattr(Config, "ACCOUNT_HISTORY_MAX_PAGES", 20))
            rows = []
            seen = set()
            complete = False
            for _ in range(max_pages):
                params = {
                    "symbol": market["id"],
                    "incomeType": "FUNDING_FEE",
                    "limit": 1000,
                }
                if cursor is not None:
                    params["startTime"] = cursor
                page = self.exchange.fapiPrivateGetIncome(params)
                if not isinstance(page, list):
                    raise ValueError("unexpected funding income response")
                for row in page:
                    key = str(row.get("tranId") or "") or (
                        str(row.get("time") or ""),
                        str(row.get("income") or ""),
                        str(row.get("asset") or ""),
                    )
                    if key not in seen:
                        seen.add(key)
                        rows.append(row)
                if len(page) < 1000:
                    complete = True
                    break
                latest_ms = max(int(row.get("time") or 0) for row in page)
                if latest_ms <= 0 or (cursor is not None and latest_ms < cursor):
                    raise RuntimeError("funding pagination made no progress")
                cursor = latest_ms + 1
            if not complete:
                raise RuntimeError("funding pagination budget exhausted")
            total = 0.0
            for row in rows:
                asset = str(row.get("asset") or "USDT").upper()
                if asset not in ("USDT", "USDC", "FDUSD", "BUSD"):
                    continue
                total += float(row.get("income") or 0.0)
            return {"status": "OK", "funding": total}
        except Exception as exc:
            logger.warning(f"[{symbol}] تعذر جلب Funding لصافي PnL: {exc}")
            return {"status": "UNKNOWN", "funding": 0.0}

    def _fetch_my_trades_complete(self, symbol, since_ms=None):
        """يجلب user trades بصفحات متتابعة أو يفشل بدلاً من الادعاء بالاكتمال."""
        cursor = int(since_ms) if since_ms else None
        max_pages = int(getattr(Config, "ACCOUNT_HISTORY_MAX_PAGES", 20))
        trades = []
        seen = set()
        complete = False
        for _ in range(max_pages):
            page = self.exchange.fetch_my_trades(
                symbol,
                since=cursor,
                limit=1000,
            )
            if not isinstance(page, list):
                raise ValueError("unexpected user trades response")
            for trade in page:
                info = trade.get("info", {}) or {}
                key = str(trade.get("id") or info.get("id") or "") or (
                    str(trade.get("order") or info.get("orderId") or ""),
                    str(trade.get("timestamp") or info.get("time") or ""),
                    str(trade.get("amount") or info.get("qty") or ""),
                )
                if key not in seen:
                    seen.add(key)
                    trades.append(trade)
            if len(page) < 1000:
                complete = True
                break
            latest_ms = max(
                int(
                    trade.get("timestamp")
                    or (trade.get("info", {}) or {}).get("time")
                    or 0
                )
                for trade in page
            )
            if latest_ms <= 0 or (cursor is not None and latest_ms < cursor):
                raise RuntimeError("user trades pagination made no progress")
            cursor = latest_ms + 1
        if not complete:
            raise RuntimeError("user trades pagination budget exhausted")
        return sorted(
            trades,
            key=lambda trade: int(
                trade.get("timestamp")
                or (trade.get("info", {}) or {}).get("time")
                or 0
            ),
        )

    def get_recent_sl_tp_fills(self, symbol, since_ms=None, entry_meta=None):
        """
        يجمع fills الإغلاق حسب order id ويربطها بأوامر الحماية المخزنة.
        لا يصنف الإغلاق اليدوي كـ TP/SL بناءً على إشارة الربح فقط.
        """
        try:
            meta = entry_meta or {}
            if since_ms is None and meta.get('entry_time'):
                try:
                    since_ms = int(pd.Timestamp(meta['entry_time']).timestamp() * 1000)
                except Exception:
                    since_ms = None

            trades = self._fetch_my_trades_complete(symbol, since_ms=since_ms)
            entry_side = str(meta.get('side', '')).upper()
            close_side = 'sell' if entry_side in ('BUY', 'LONG') else (
                'buy' if entry_side in ('SELL', 'SHORT') else ''
            )
            expected_position_side = str(meta.get('position_side', '')).upper()
            groups = {}

            entry_qty = 0.0
            entry_commission = 0.0
            entry_commission_complete = True
            for trade in trades or []:
                info = trade.get('info', {}) or {}
                trade_side = str(info.get('side') or trade.get('side') or '').lower()
                trade_position_side = str(info.get('positionSide', '')).upper()
                if (
                    expected_position_side
                    and trade_position_side
                    and trade_position_side not in ('BOTH', expected_position_side)
                ):
                    continue
                if not close_side or trade_side == close_side:
                    continue
                qty = float(info.get('qty') or trade.get('amount') or 0.0)
                if qty <= 0:
                    continue
                fee = abs(float(
                    info.get('commission')
                    or trade.get('fee', {}).get('cost', 0)
                    or 0
                ))
                fee_asset = str(
                    info.get('commissionAsset')
                    or trade.get('fee', {}).get('currency', '')
                ).upper()
                entry_qty += qty
                if fee_asset in ('', 'USDT', 'USDC', 'FDUSD', 'BUSD'):
                    entry_commission += fee
                elif fee > 0:
                    entry_commission_complete = False

            for trade in trades or []:
                info = trade.get('info', {}) or {}
                qty = float(
                    info.get('qty')
                    or trade.get('amount')
                    or 0
                )
                if qty <= 0:
                    continue

                trade_side = str(info.get('side') or trade.get('side') or '').lower()
                trade_position_side = str(info.get('positionSide', '')).upper()
                if close_side and trade_side != close_side:
                    continue
                if (
                    expected_position_side
                    and trade_position_side
                    and trade_position_side not in ('BOTH', expected_position_side)
                ):
                    continue

                realized_pnl = float(info.get('realizedPnl', 0) or 0)
                explicit_close = str(info.get('closePosition', '')).lower() == 'true'
                # عند غياب metadata نبقي التوافق القديم لكن دون اعتبار fee/funding صفقة.
                if not close_side and not explicit_close and realized_pnl == 0:
                    continue

                order_id = str(
                    trade.get('order')
                    or info.get('orderId')
                    or info.get('orderID')
                    or f"trade:{trade.get('id', '')}"
                )
                timestamp = int(
                    trade.get('timestamp')
                    or info.get('time')
                    or info.get('updateTime')
                    or 0
                )
                price = float(trade.get('price') or info.get('price') or 0)
                commission = abs(float(
                    info.get('commission')
                    or trade.get('fee', {}).get('cost', 0)
                    or 0
                ))
                commission_asset = str(
                    info.get('commissionAsset')
                    or trade.get('fee', {}).get('currency', '')
                ).upper()
                commission_usdt = commission if commission_asset in ('', 'USDT', 'USDC', 'FDUSD', 'BUSD') else 0.0

                group = groups.setdefault(order_id, {
                    'symbol': symbol,
                    'order_id': order_id,
                    'client_order_id': str(
                        info.get('clientOrderId')
                        or info.get('clientOrderID')
                        or ''
                    ),
                    'raw_type': str(info.get('orderType') or info.get('type') or '').upper(),
                    'qty': 0.0,
                    'notional': 0.0,
                    'realized_pnl_gross': 0.0,
                    'commission': 0.0,
                    'timestamp': timestamp,
                    'price': price,
                    'position_side': trade_position_side,
                })
                group['qty'] += qty
                group['notional'] += qty * price
                group['realized_pnl_gross'] += realized_pnl
                group['commission'] += commission_usdt
                if timestamp >= group['timestamp']:
                    group['timestamp'] = timestamp
                    group['price'] = price
                if not group['raw_type']:
                    group['raw_type'] = str(info.get('orderType') or info.get('type') or '').upper()

            sl_ids = {
                str(value) for value in (
                    meta.get('sl_order_id'),
                    meta.get('sl_client_id'),
                    meta.get('sl_actual_order_id'),
                ) if value
            }
            tp_ids = {
                str(value) for value in (
                    meta.get('tp_order_id'),
                    meta.get('tp_client_id'),
                    meta.get('tp_actual_order_id'),
                    meta.get('tp1_order_id'),
                    meta.get('tp1_client_id'),
                ) if value
            }

            # أمر Algo له algoId بينما userTrades يعرض actualOrderId. نربطهما
            # صراحة حتى لا يتحول SL/TP الحقيقي إلى MANUAL_CLOSE.
            for client_id, target_ids in (
                (meta.get('sl_client_id'), sl_ids),
                (meta.get('tp_client_id'), tp_ids),
            ):
                if not client_id:
                    continue
                algo_order = self._find_algo_order_by_client_id(
                    symbol, client_id, quiet=True
                )
                if not algo_order:
                    continue
                for value in (
                    algo_order.get('id'),
                    algo_order.get('clientOrderId'),
                    algo_order.get('actualOrderId'),
                ):
                    if value not in (None, "", "0", 0):
                        target_ids.add(str(value))

            funding_result = self._fetch_funding_income(symbol, since_ms=since_ms)
            total_closed_qty = sum(group.get('qty', 0.0) for group in groups.values())
            alerts = []
            for group in groups.values():
                identifiers = {group['order_id'], group['client_order_id']}
                raw_type = group['raw_type']
                if identifiers & sl_ids or ('STOP' in raw_type and 'TAKE_PROFIT' not in raw_type):
                    close_type = 'STOP_LOSS'
                    classification_source = 'protection_order_match'
                elif identifiers & tp_ids or 'TAKE_PROFIT' in raw_type:
                    close_type = 'TAKE_PROFIT'
                    classification_source = 'protection_order_match'
                elif (
                    meta.get('pending_close_reason')
                    and float(meta.get('pending_close_requested_at') or 0) > 0
                    and group.get('timestamp', 0) > 0
                    and -5 <= (
                        group['timestamp'] / 1000.0
                        - float(meta.get('pending_close_requested_at'))
                    ) <= 300
                ):
                    close_type = str(meta['pending_close_reason']).upper()
                    classification_source = 'persisted_bot_close_intent'
                else:
                    close_type = 'MANUAL_CLOSE'
                    classification_source = 'unmatched_exchange_fill'

                avg_price = (
                    group['notional'] / group['qty']
                    if group['qty'] > 0 else group['price']
                )
                allocation = (
                    group['qty'] / total_closed_qty if total_closed_qty > 0 else 0.0
                )
                entry_fee_allocated = min(
                    entry_commission,
                    entry_commission * (
                        group['qty'] / entry_qty if entry_qty > 0 else 0.0
                    ),
                )
                funding_allocated = float(funding_result['funding']) * allocation
                total_fees = group['commission'] + entry_fee_allocated
                net_pnl = group['realized_pnl_gross'] - total_fees + funding_allocated
                timestamp = group['timestamp']
                alerts.append({
                    **group,
                    'type': close_type,
                    'classification_source': classification_source,
                    'price': avg_price,
                    'pnl': net_pnl,
                    'entry_commission': entry_fee_allocated,
                    'exit_commission': group['commission'],
                    'commission': total_fees,
                    'funding': funding_allocated,
                    'funding_status': funding_result['status'],
                    'pnl_complete': bool(
                        entry_commission_complete
                        and funding_result['status'] == 'OK'
                    ),
                    'exit_time': (
                        pd.to_datetime(timestamp, unit='ms').strftime('%Y-%m-%d %H:%M:%S')
                        if timestamp > 0 else None
                    ),
                })

            return sorted(alerts, key=lambda item: item.get('timestamp', 0))
        except Exception as e:
            logger.error(f"خطأ في جلب التداولات المغلقة لـ {symbol} (قد لا تكون مدعومة): {e}")
            return []
