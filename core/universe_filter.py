import time
import math
from core.scanner_universe import canonical_usdt_symbol, select_demo_targets
from cachetools import TTLCache
from core.logger import logger
from core.config import Config

class UniverseFilter:
    def __init__(self, client, state_manager=None):
        self.client = client
        self.state_manager = state_manager
        self.last_selection_details = {}
        # قائمة العملات الميمية المعروفة لتطبيق قواعد حماية مشددة عليها
        self.meme_coins = {
            'DOGE', 'SHIB', 'PEPE', 'FLOKI', 'BONK', 
            'WIF', 'BOME', 'MEME', 'MYRO', '1000SATS', 'ORDI',
            '1000PEPE', '1000SHIB', '1000BONK', '1000FLOKI'
        }
        # القائمة السوداء الافتراضية (مستقرة أو مشاكل سيولة)
        self.blacklist = set(getattr(Config, 'BLACKLIST', [
            'USDC/USDT', 'FDUSD/USDT', 'TUSD/USDT', 'AEUR/USDT', 'EUR/USDT'
        ]))
        
        # إعدادات الفلترة من الإعدادات أو الافتراضية
        self.min_volume_24h = getattr(Config, 'MIN_24H_VOLUME_USDT', 10_000_000) # 10 مليون دولار
        # ── طبقة ثانية: سيولة متوسطة مسموحة فقط بشرط زخم استثنائي ───────
        self.min_volume_24h_relaxed = getattr(Config, 'MIN_24H_VOLUME_RELAXED_USDT', 2_000_000)  # 2 مليون
        self.relaxed_requires_volume_spike = getattr(Config, 'RELAXED_REQUIRES_SPIKE', True)
        self.max_spread_pct = getattr(Config, 'MAX_SPREAD_PCT', 0.001)         # 0.10%

        # العملات المستهدفة للتحليل لمراقبة الفلاتر المرفوضة بدقة
        self.target_universe = {
            'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'ADA/USDT', 
            'XRP/USDT', 'DOT/USDT', 'LTC/USDT', 'LINK/USDT', 'AVAX/USDT', 
            'NEAR/USDT', 'FIL/USDT', 'FTM/USDT', 'OP/USDT', 'ARB/USDT', 
            'INJ/USDT', 'GRT/USDT', 'SUI/USDT', 'APT/USDT', 'LDO/USDT',
            'DOGE/USDT', '1000PEPE/USDT', '1000SHIB/USDT', '1000BONK/USDT', 
            'WIF/USDT', 'FLOKI/USDT', 'BOME/USDT', 'ORDI/USDT', 'PEOPLE/USDT', 
            'MEME/USDT'
        }
        self._symbol_classification_cache = TTLCache(maxsize=1000, ttl=6 * 3600)

    def classify_symbol(self, symbol):
        """
        تصنيف العملة لمعرفة ما إذا كانت ميم أو جديدة أو عملة كبرى.
        """
        if symbol in self._symbol_classification_cache:
            return self._symbol_classification_cache[symbol]

        base = symbol.split('/')[0]
        
        # 1. التحقق من كونها عملة ميمية معروفة
        if base in self.meme_coins:
            res = ('meme', "عملة ميمية معروفة")
            self._symbol_classification_cache[symbol] = res
            return res
            
        # 2. التحقق من كونها جديدة (أقل من 30 يوم من البيانات اليومية)
        try:
            # نجلب 30 شمعة يومية للتحقق من عمر التداول على باينانس
            daily_candles = self.client.exchange.fetch_ohlcv(symbol, timeframe='1d', limit=30)
            if len(daily_candles) < 30:
                res = ('new', f"عملة جديدة (عمرها {len(daily_candles)} يوم فقط)")
                self._symbol_classification_cache[symbol] = res
                return res
        except Exception as e:
            logger.warning(f"فشل التحقق من عمر العملة {symbol}: {e}")
            
        res = ('mainstream', "عملة رئيسية / مستقرة تاريخياً")
        self._symbol_classification_cache[symbol] = res
        return res

    def get_dynamic_blacklist(self):
        """
        تقوم بتحليل الصفقات التاريخية من الباك تست والصفقات الحية لآخر 90 يوماً
        وتحظر تلقائياً العملات ذات عامل الربح المنخفض أو التراجع العالي.
        """
        # القائمة السوداء الثابتة كبداية
        dynamic_blacklist = set(self.blacklist)
        
        is_testnet = getattr(Config, 'USE_TESTNET', False)
        is_dynamic_blacklist_active = getattr(Config, 'DYNAMIC_BLACKLIST_ACTIVE', True)
        if is_testnet:
            # تعطيل القائمة السوداء الديناميكية مؤقتاً في وضع Testnet
            is_dynamic_blacklist_active = False
            
        if not is_dynamic_blacklist_active:
            return dynamic_blacklist
            
        import os
        import pandas as pd
        
        data_dir = getattr(Config, 'DATA_DIR', '.')
        trades_dfs = []
        for file_name in ['backtest_results.csv', 'testnet_trades_log.csv']:
            target_path = os.path.join(data_dir, file_name)
            if not os.path.exists(target_path) and data_dir != '.':
                target_path = file_name
            if os.path.exists(target_path):
                try:
                    df = pd.read_csv(target_path)
                    if not df.empty:
                        trades_dfs.append(df)
                except Exception as e:
                    logger.error(f"فشل قراءة سجل الصفقات {target_path}: {e}")
                    
        if not trades_dfs:
            return dynamic_blacklist
            
        try:
            all_trades = pd.concat(trades_dfs, ignore_index=True)
            if all_trades.empty or 'close_time' not in all_trades.columns:
                return dynamic_blacklist
                
            all_trades['close_time'] = pd.to_datetime(all_trades['close_time'], errors='coerce')
            latest_time = all_trades['close_time'].max()
            if pd.isna(latest_time):
                return dynamic_blacklist
                
            cutoff_time = latest_time - pd.Timedelta(days=90)
            recent_trades = all_trades[all_trades['close_time'] >= cutoff_time]
            
            min_trades = max(
                20, int(getattr(Config, 'DYNAMIC_BLACKLIST_MIN_TRADES', 20))
            )
            for symbol, group in recent_trades.groupby('symbol'):
                # لا يُسمح لبضعة أحداث عشوائية بتكوين قاعدة حظر دائمة.
                if len(group) >= min_trades:
                    pnls = pd.to_numeric(group['pnl'], errors='coerce').dropna().values
                    winners = pnls[pnls > 0]
                    losers = pnls[pnls <= 0]
                    
                    sum_win = winners.sum() if len(winners) > 0 else 0
                    sum_loss = abs(losers.sum()) if len(losers) > 0 else 0
                    
                    # حساب عامل الربح (Profit Factor)
                    pf = sum_win / sum_loss if sum_loss > 0 else float('inf')
                    
                    # حساب أقصى تراجع للرمز (Max Drawdown)
                    equity = [100.0]
                    pnl_pcts = pd.to_numeric(group.get('pnl_pct', group['pnl'] / 10.0), errors='coerce').fillna(0).values
                    for p_pct in pnl_pcts:
                        equity.append(equity[-1] + p_pct)
                    
                    peak = equity[0]
                    max_dd = 0
                    for val in equity:
                        if val > peak:
                            peak = val
                        dd = (peak - val) / peak * 100
                        if dd > max_dd:
                            max_dd = dd
                            
                    # شروط الإدراج في القائمة السوداء الديناميكية:
                    # 1. PF أقل من 0.85
                    # 2. أو Max Drawdown أعلى من 15%
                    if pf < 0.85 or max_dd > 15.0:
                        dynamic_blacklist.add(symbol)
                        logger.warning(f"🚫 [Dynamic Blacklist] حظر {symbol} تلقائياً لآخر 90 يوماً: PF={pf:.2f} | Max DD={max_dd:.1f}%")
                        
        except Exception as e:
            logger.error(f"خطأ أثناء تحديث القائمة السوداء الديناميكية: {e}")
            
        return dynamic_blacklist

    def filter_and_get_tickers(self):
        """
        مسح كامل السوق وجلب جميع أزواج USDT Perpetual المؤهلة وتصنيفها.
        """
        try:
            is_testnet = getattr(Config, 'USE_TESTNET', False)
            logger.info("🔍 بدء عملية مسح وتصفية أسواق باينانس (Universe Filtering)...")
            tickers = self.client.exchange.fetch_tickers()
            markets = self.client.exchange.load_markets()
            
            # جلب القائمة السوداء الديناميكية
            active_blacklist = {canonical_usdt_symbol(s) for s in self.get_dynamic_blacklist()}
            static_blacklist = {canonical_usdt_symbol(s) for s in self.blacklist}
            
            eligible_symbols = []
            seen_bases = set()
            
            rejection_stats = {
                'symbol_format': 0,
                'inactive_or_not_swap': 0,
                'static_blacklist': 0,
                'dynamic_blacklist': 0,
                'volume': 0,
                'spread': 0
            }
            
            for symbol, ticker in tickers.items():
                # 1. فلترة أزواج USDT العقود الآجلة المستمرة فقط
                if not (symbol.endswith('/USDT') or ':USDT' in symbol):
                    rejection_stats['symbol_format'] += 1
                    continue
                if '-' in symbol: # استبعاد العقود المؤرخة (Delivery)
                    rejection_stats['symbol_format'] += 1
                    continue
                
                market = markets.get(symbol)
                market_type = market.get('type', '') if market else ''
                is_valid_type = market_type in ('swap', 'future') if is_testnet else market_type == 'swap'
                if not market or not market.get('active', False) or not is_valid_type:
                    rejection_stats['inactive_or_not_swap'] += 1
                    continue
                
                if is_testnet:
                    info = market.get('info') or {}
                    if (not canonical_usdt_symbol(symbol)
                            or market.get('quote', 'USDT') != 'USDT'
                            or market.get('settle', 'USDT') != 'USDT'
                            or market.get('inverse') is True
                            or market.get('linear') is False
                            or market.get('expiry')
                            or info.get('contractType', 'PERPETUAL') != 'PERPETUAL'):
                        rejection_stats['inactive_or_not_swap'] += 1
                        continue

                # 2. استبعاد القائمة السوداء الثابتة والديناميكية
                if canonical_usdt_symbol(symbol) in active_blacklist:
                    if canonical_usdt_symbol(symbol) in static_blacklist:
                        rejection_stats['static_blacklist'] += 1
                    else:
                        rejection_stats['dynamic_blacklist'] += 1
                    continue
                    
                # 3. التحقق من حجم التداول 24h
                try:
                    volume_24h = float(ticker.get('quoteVolume', 0) or 0)
                except (TypeError, ValueError, OverflowError):
                    volume_24h = 0.0
                if not math.isfinite(volume_24h) or volume_24h <= 0:
                    rejection_stats['volume'] += 1
                    continue
                if volume_24h < self.min_volume_24h:
                    # ── فلتر طبقي: السماح بسيولة أقل بشرط Spike إستثنائي ──
                    _allowed_low_vol = False
                    if self.relaxed_requires_volume_spike and volume_24h >= self.min_volume_24h_relaxed:
                        # CCXT ``percentage`` هو تغير *السعر* وليس تغير الحجم.
                        # استخدامه هنا كان يسمح لعملة قليلة السيولة بعد pump كبير
                        # بالمرور تحت اسم Volume Spike. لا نقبل المسار المرن ما لم
                        # يوفّر المصدر حقلاً صريحاً لتغير quote volume.
                        info = ticker.get("info") or {}
                        raw_volume_change = (
                            ticker.get("quoteVolumeChangePercent")
                            or info.get("quoteVolumeChangePercent")
                        )
                        try:
                            volume_change_24h = float(raw_volume_change)
                        except (TypeError, ValueError):
                            volume_change_24h = None
                        if volume_change_24h is not None and math.isfinite(volume_change_24h) and volume_change_24h > 200:
                            _allowed_low_vol = True
                            logger.debug(
                                f"[UniverseFilter] قبول استثنائي (تغير حجم صريح "
                                f"{volume_change_24h:.0f}%): {symbol}"
                            )
                    if not _allowed_low_vol:
                        rejection_stats['volume'] += 1
                        if self.state_manager and symbol in self.target_universe:
                            self.state_manager.increment_rejection_counter("Volume")
                        continue
                    
                # 4. التحقق من السبريد (Spread)
                ask = ticker.get('ask', 0) or 0
                bid = ticker.get('bid', 0) or 0
                if bid <= 0:
                    if not is_testnet:
                        rejection_stats['spread'] += 1
                        continue
                    else:
                        spread = 0.0
                else:
                    spread = (ask - bid) / bid
                
                if not is_testnet and spread > self.max_spread_pct:
                    rejection_stats['spread'] += 1
                    if self.state_manager and symbol in self.target_universe:
                        self.state_manager.increment_rejection_counter("Spread")
                    continue
                    
                base_key = symbol.split(':')[0]
                if base_key not in seen_bases:
                    seen_bases.add(base_key)
                    eligible_symbols.append((symbol, ticker, volume_24h))
                
            logger.info("=========================================")
            logger.info("📊 تقرير تصفية أزواج العملات (Universe Filter Report):")
            logger.info(f"   • Rejected by symbol format: {rejection_stats['symbol_format']}")
            logger.info(f"   • Rejected by inactive/not swap: {rejection_stats['inactive_or_not_swap']}")
            logger.info(f"   • Rejected by static blacklist: {rejection_stats['static_blacklist']}")
            logger.info(f"   • Rejected by dynamic blacklist: {rejection_stats['dynamic_blacklist']}")
            logger.info(f"   • Rejected by volume (< {self.min_volume_24h/1_000_000:.1f}M): {rejection_stats['volume']}")
            logger.info(f"   • Rejected by spread (> {self.max_spread_pct:.2%}): {rejection_stats['spread']}")
            logger.info(f"   • Final eligible pairs: {len(eligible_symbols)}")
            logger.info("=========================================")
            return eligible_symbols
            
        except Exception as e:
            logger.error(f"❌ خطأ أثناء تصفية الأسواق: {e}")
            return []

    def get_scanning_targets(self, limit=50, *, activity_snapshot=None):
        """Select venue-eligible pairs; Demo can reserve slots for fresh activity."""
        limit = max(0, int(limit))
        eligible = self.filter_and_get_tickers()
        if getattr(Config, 'USE_TESTNET', False):
            enabled = getattr(Config, 'DEMO_ACTIVITY_UNIVERSE_ENABLED', True)
            targets, self.last_selection_details = select_demo_targets(
                eligible,
                forced=getattr(Config, 'TESTNET_FORCE_SYMBOLS', []),
                limit=limit,
                snapshot=activity_snapshot if enabled else None,
                now_ts=time.time(),
                activity_slots=getattr(Config, 'DEMO_ACTIVITY_UNIVERSE_SLOTS', 20),
                max_age_seconds=getattr(Config, 'DEMO_ACTIVITY_MAX_AGE_SECONDS', 180),
            )
        else:
            targets = [row[0] for row in sorted(eligible, key=lambda row: row[2], reverse=True)[:limit]]
            self.last_selection_details = {
                "policy": "LIVE_LIQUIDITY", "selected_count": len(targets), "targets": targets,
            }
        logger.info("[UniverseFilter] policy=%s selected=%s activity=%s added=%s",
                    self.last_selection_details['policy'], len(targets),
                    self.last_selection_details.get('activity_status', 'DISABLED'),
                    self.last_selection_details.get('added_vs_liquidity', []))
        return targets
