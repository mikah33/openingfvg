// OR FVG v11 — NinjaScript Strategy for NinjaTrader 8
// Ported from Pine Script: Opening Range + FVG Breakout/Retest Strategy
// Designed for MES (Micro E-mini S&P 500) on 1-minute chart

#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.SuperDom;
using NinjaTrader.Gui.Tools;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.Core.FloatingPoint;
using NinjaTrader.NinjaScript.Indicators;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class ORFVGv11 : Strategy
    {
        #region Variables
        // State — mirrors Pine Script vars exactly
        private double orHigh, orLow;
        private int    phase;          // -1=idle, 0=building OR, 1=scanning, 2=bias set, 4=in trade
        private int    bias;           // 0=none, 1=bull, -1=bear
        private double fvgTop, fvgBot;
        private int    breakoutBar, orCloseBar;
        private double retestLevel, retestSL;

        // FVG tracking — most recent bull/bear FVG coordinates
        private double lastBullTop, lastBullBot;
        private int    lastBullBar;
        private double lastBearTop, lastBearBot;
        private int    lastBearBar;

        // Persistence
        private int  tradingDay;
        private bool wasInOR;

        // Equity tracking (works in both backtest and live)
        private double currentEquity;

        // Pending order info for TP adjustment after fill
        private double pendingSL;
        private int    pendingBias;

        // Indicator
        private ATR atrIndicator;
        #endregion

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description     = "OR FVG v11 — Opening Range + FVG Breakout/Retest Strategy";
                Name            = "ORFVGv11";
                Calculate       = Calculate.OnBarClose;
                EntriesPerDirection                 = 1;
                EntryHandling                       = EntryHandling.UniqueEntries;
                IsExitOnSessionCloseStrategy         = true;
                ExitOnSessionCloseSeconds            = 60;
                IsFillLimitOnTouch                   = false;
                MaximumBarsLookBack                  = MaximumBarsLookBack.TwoHundredFiftySix;
                OrderFillResolution                  = OrderFillResolution.Standard;
                StartBehavior                        = StartBehavior.WaitUntilFlat;
                IsInstantiatedOnEachOptimizationIteration = true;

                // ── Strategy Inputs ──
                ORStartTime          = 93000;   // 9:30:00 — adjust if chart uses different timezone
                OREndTime            = 93500;   // 9:35:00
                SetupExpiryMinutes   = 120;
                RRRatio              = 1.75;
                StartingCapital      = 500;
                RiskDay1Pct          = 33.0;
                RiskDay2Pct          = 25.0;
                RiskDay3Pct          = 15.0;
                FVGMaxAgeBars        = 15;
                FVGToleranceATRMult  = 0.1;
                ATRPeriod            = 14;
            }
            else if (State == State.DataLoaded)
            {
                atrIndicator = ATR(ATRPeriod);
                currentEquity = StartingCapital;
                pendingSL = double.NaN;
                pendingBias = 0;
                ResetAllState();
            }
        }

        private void ResetAllState()
        {
            orHigh = orLow = double.NaN;
            phase = -1;
            bias = 0;
            fvgTop = fvgBot = double.NaN;
            breakoutBar = orCloseBar = 0;
            retestLevel = retestSL = double.NaN;
            lastBullTop = lastBullBot = double.NaN;
            lastBullBar = 0;
            lastBearTop = lastBearBot = double.NaN;
            lastBearBar = 0;
            tradingDay = 0;
            wasInOR = false;
        }

        /// <summary>
        /// Resets intraday state at the start of each new OR session.
        /// Called when newOR fires. Mirrors Pine Script lines 54-71.
        /// </summary>
        private void ResetDay()
        {
            bias = 0;
            fvgTop = fvgBot = double.NaN;
            breakoutBar = orCloseBar = 0;
            retestLevel = retestSL = double.NaN;
            lastBullTop = lastBullBot = double.NaN;
            lastBullBar = 0;
            lastBearTop = lastBearBot = double.NaN;
            lastBearBar = 0;
        }

        protected override void OnBarUpdate()
        {
            // Need enough bars for ATR + FVG lookback
            if (CurrentBar < Math.Max(ATRPeriod, 3))
                return;

            // DEBUG: Print first bar's time so we can verify timezone
            if (CurrentBar == Math.Max(ATRPeriod, 3))
                Print("FIRST BAR TIME: " + Time[0].ToString() + " | ToTime=" + ToTime(Time[0]));

            // ════════════════════════════════════════════════════════════
            //  OR Session Detection
            //  Pine: inOR = not na(time(timeframe.period, "0930-0935"))
            //  NinjaTrader: compare bar close time to OR window
            //  On 1-min chart, bar closing at 9:31 covers 9:30-9:31, etc.
            //  inOR is true for bars with close time IN (ORStart, OREnd]
            // ════════════════════════════════════════════════════════════
            int barTime   = ToTime(Time[0]);
            bool isWeekday = Time[0].DayOfWeek >= DayOfWeek.Monday
                          && Time[0].DayOfWeek <= DayOfWeek.Friday;
            bool inOR   = isWeekday && barTime > ORStartTime && barTime <= OREndTime;
            bool newOR  = inOR && !wasInOR;
            bool orDone = !inOR && wasInOR;
            wasInOR = inOR;

            // ════════════════════════════════════════════════════════════
            //  Phase 0: Build Opening Range  (Pine lines 54-85)
            // ════════════════════════════════════════════════════════════
            if (newOR)
            {
                tradingDay++;
                ResetDay();
                orHigh = High[0];
                orLow  = Low[0];
                phase  = 0;
                Print("Day " + tradingDay + " — New OR started");
                return;
            }

            if (phase == 0 && inOR)
            {
                orHigh = Math.Max(orHigh, High[0]);
                orLow  = Math.Min(orLow, Low[0]);
                return;
            }

            if (phase == 0 && orDone)
            {
                phase      = 1;
                orCloseBar = CurrentBar;
                Print("OR complete: High=" + orHigh.ToString("F2")
                    + " Low=" + orLow.ToString("F2"));
            }

            // ════════════════════════════════════════════════════════════
            //  FVG Detection  (Pine lines 88-105)
            //  Runs every bar once phase >= 1.
            //  Bull FVG: current low > 2-bar-ago high - tolerance (gap up)
            //  Bear FVG: current high < 2-bar-ago low + tolerance (gap down)
            //  Both checks are independent (if, not else-if).
            // ════════════════════════════════════════════════════════════
            double atrVal = atrIndicator[0];
            double fvgTol = atrVal * FVGToleranceATRMult;

            if (phase >= 1)
            {
                if (Low[0] > High[2] - fvgTol)
                {
                    lastBullTop = Low[0];
                    lastBullBot = High[2];
                    lastBullBar = CurrentBar - 2;
                }
                if (High[0] < Low[2] + fvgTol)
                {
                    lastBearTop = Low[2];
                    lastBearBot = High[0];
                    lastBearBar = CurrentBar - 2;
                }
            }

            // ════════════════════════════════════════════════════════════
            //  STEP 1: Day Expiry  (Pine lines 114-121)
            //  If no trade found within setupMins of OR close, kill the day.
            // ════════════════════════════════════════════════════════════
            bool dayExpired = phase >= 1 && phase < 4
                           && orCloseBar > 0
                           && (CurrentBar - orCloseBar) > SetupExpiryMinutes;
            if (dayExpired)
            {
                Print("Day expired — no trade within " + SetupExpiryMinutes + " minutes");
                bias = 0;
                phase = -1;
                retestLevel = retestSL = double.NaN;
                CloseAllPositions("Day Expired");
                return;
            }

            // ════════════════════════════════════════════════════════════
            //  STEP 1b: Invalidation  (Pine lines 123-141)
            //  Phase 2 only. If price blows through the FVG boundary
            //  (or retest SL after a retest started), reset to scanning.
            // ════════════════════════════════════════════════════════════
            if (phase == 2)
            {
                if (bias == 1)
                {
                    double invLevel = double.IsNaN(retestLevel) ? fvgBot : retestSL;
                    if (!double.IsNaN(invLevel) && Close[0] < invLevel)
                    {
                        Print("Invalidated LONG: close=" + Close[0].ToString("F2")
                            + " < inv=" + invLevel.ToString("F2"));
                        bias = 0;
                        phase = 1;
                        retestLevel = retestSL = double.NaN;
                        CloseAllPositions("Invalidated");
                    }
                }
                if (bias == -1)
                {
                    double invLevel = double.IsNaN(retestLevel) ? fvgTop : retestSL;
                    if (!double.IsNaN(invLevel) && Close[0] > invLevel)
                    {
                        Print("Invalidated SHORT: close=" + Close[0].ToString("F2")
                            + " > inv=" + invLevel.ToString("F2"));
                        bias = 0;
                        phase = 1;
                        retestLevel = retestSL = double.NaN;
                        CloseAllPositions("Invalidated");
                    }
                }
            }

            // ════════════════════════════════════════════════════════════
            //  STEP 2: Breakout + FVG Scanner  (Pine lines 148-182)
            //  Runs in phase 1 (scanning) AND phase 2 (for bias flips).
            // ════════════════════════════════════════════════════════════
            bool hasBearFVG = !double.IsNaN(lastBearTop)
                           && (CurrentBar - lastBearBar) <= FVGMaxAgeBars;
            bool hasBullFVG = !double.IsNaN(lastBullTop)
                           && (CurrentBar - lastBullBar) <= FVGMaxAgeBars;

            if (phase == 1 || phase == 2)
            {
                // Bearish breakout: close below OR low + recent bear FVG
                if (Close[0] < orLow && hasBearFVG && bias != -1)
                {
                    if (bias != 0)
                    {
                        Print("Bias flip to BEAR — closing existing position");
                        CloseAllPositions("Bias Flip");
                    }
                    bias        = -1;
                    fvgTop      = lastBearTop;
                    fvgBot      = lastBearBot;
                    phase       = 2;
                    breakoutBar = CurrentBar;
                    retestLevel = retestSL = double.NaN;
                    Print("BEARISH breakout: close=" + Close[0].ToString("F2")
                        + " < OR low=" + orLow.ToString("F2")
                        + " FVG=[" + fvgTop.ToString("F2") + ", " + fvgBot.ToString("F2") + "]");
                }
                // Bullish breakout: close above OR high + recent bull FVG
                else if (Close[0] > orHigh && hasBullFVG && bias != 1)
                {
                    if (bias != 0)
                    {
                        Print("Bias flip to BULL — closing existing position");
                        CloseAllPositions("Bias Flip");
                    }
                    bias        = 1;
                    fvgTop      = lastBullTop;
                    fvgBot      = lastBullBot;
                    phase       = 2;
                    breakoutBar = CurrentBar;
                    retestLevel = retestSL = double.NaN;
                    Print("BULLISH breakout: close=" + Close[0].ToString("F2")
                        + " > OR high=" + orHigh.ToString("F2")
                        + " FVG=[" + fvgTop.ToString("F2") + ", " + fvgBot.ToString("F2") + "]");
                }
            }

            // ════════════════════════════════════════════════════════════
            //  STEP 3: Retest Detection  (Pine lines 189-197)
            //  When a candle wicks INTO the FVG zone, track its extreme.
            //  Bull: low touches fvgTop → track high (entry) + low (SL)
            //  Bear: high touches fvgBot → track low (entry) + high (SL)
            // ════════════════════════════════════════════════════════════
            if (phase == 2 && CurrentBar > breakoutBar)
            {
                if (bias == 1 && Low[0] <= fvgTop)
                {
                    retestLevel = High[0];
                    retestSL    = Low[0];
                }
                if (bias == -1 && High[0] >= fvgBot)
                {
                    retestLevel = Low[0];
                    retestSL    = High[0];
                }
            }

            // ════════════════════════════════════════════════════════════
            //  STEP 4: Entry Signal  (Pine lines 204-246)
            //  Retest candle's extreme gets "engulfed" by a close beyond it.
            //  Places bracket order: market entry + SL stop + TP limit.
            // ════════════════════════════════════════════════════════════
            bool longSignal  = phase == 2 && CurrentBar > breakoutBar
                            && bias == 1 && !double.IsNaN(retestLevel)
                            && Close[0] > retestLevel;

            bool shortSignal = phase == 2 && CurrentBar > breakoutBar
                            && bias == -1 && !double.IsNaN(retestLevel)
                            && Close[0] < retestLevel;

            if (longSignal)
            {
                double entryPx         = Close[0];
                double slPx            = retestSL;
                double riskPts         = entryPx - slPx;
                double tpPx            = entryPx + riskPts * RRRatio;  // temporary, adjusted on fill
                double riskPerContract = riskPts * Instrument.MasterInstrument.PointValue;

                if (riskPerContract > 0)
                {
                    double equity  = GetEquity();
                    double riskPct = tradingDay <= 1 ? RiskDay1Pct / 100.0 : tradingDay == 2 ? RiskDay2Pct / 100.0 : RiskDay3Pct / 100.0;
                    int qty        = (int)Math.Floor(equity * riskPct / riskPerContract);

                    if (qty >= 1)
                    {
                        phase = 4;
                        pendingSL = slPx;
                        pendingBias = 1;
                        SetStopLoss("Long", CalculationMode.Price, slPx, false);
                        SetProfitTarget("Long", CalculationMode.Price, tpPx);
                        EnterLong(qty, "Long");
                        Print(">>> LONG SIGNAL: qty=" + qty
                            + " signalPx=" + entryPx.ToString("F2")
                            + " SL=" + slPx.ToString("F2")
                            + " tempTP=" + tpPx.ToString("F2")
                            + " equity=$" + equity.ToString("F2"));
                    }
                }
            }
            else if (shortSignal)
            {
                double entryPx         = Close[0];
                double slPx            = retestSL;
                double riskPts         = slPx - entryPx;
                double tpPx            = entryPx - riskPts * RRRatio;  // temporary, adjusted on fill
                double riskPerContract = riskPts * Instrument.MasterInstrument.PointValue;

                if (riskPerContract > 0)
                {
                    double equity  = GetEquity();
                    double riskPct = tradingDay <= 1 ? RiskDay1Pct / 100.0 : tradingDay == 2 ? RiskDay2Pct / 100.0 : RiskDay3Pct / 100.0;
                    int qty        = (int)Math.Floor(equity * riskPct / riskPerContract);

                    if (qty >= 1)
                    {
                        phase = 4;
                        pendingSL = slPx;
                        pendingBias = -1;
                        SetStopLoss("Short", CalculationMode.Price, slPx, false);
                        SetProfitTarget("Short", CalculationMode.Price, tpPx);
                        EnterShort(qty, "Short");
                        Print(">>> SHORT SIGNAL: qty=" + qty
                            + " signalPx=" + entryPx.ToString("F2")
                            + " SL=" + slPx.ToString("F2")
                            + " tempTP=" + tpPx.ToString("F2")
                            + " equity=$" + equity.ToString("F2"));
                    }
                }
            }
        }

        /// <summary>
        /// Get current equity. Uses realized P&L for backtest,
        /// live account value for real-time. Mirrors Pine's strategy.equity.
        /// </summary>
        private double GetEquity()
        {
            if (State == State.Realtime)
                return Account.Get(AccountItem.CashValue, Currency.UsDollar);

            // Backtest: never below starting capital (user refills after losses)
            return Math.Max(currentEquity, StartingCapital);
        }

        /// <summary>
        /// Called on every order fill. Recalculates TP based on ACTUAL fill price
        /// so the R:R ratio is maintained regardless of slippage.
        /// </summary>
        protected override void OnExecutionUpdate(Execution execution, string executionId,
            double price, int quantity, MarketPosition marketPosition,
            string orderId, DateTime time)
        {
            if (execution.Order.OrderState != OrderState.Filled)
                return;

            // Entry fill — recalculate TP from actual fill price
            if (execution.Order.Name == "Long" && pendingBias == 1 && !double.IsNaN(pendingSL))
            {
                double actualRisk = price - pendingSL;
                if (actualRisk > 0)
                {
                    double newTP = price + actualRisk * RRRatio;
                    SetProfitTarget("Long", CalculationMode.Price, newTP);
                    Print(">>> LONG FILLED: fill=" + price.ToString("F2")
                        + " SL=" + pendingSL.ToString("F2")
                        + " TP=" + newTP.ToString("F2")
                        + " R:R=" + RRRatio.ToString("F2"));
                }
                pendingSL = double.NaN;
                pendingBias = 0;
            }
            else if (execution.Order.Name == "Short" && pendingBias == -1 && !double.IsNaN(pendingSL))
            {
                double actualRisk = pendingSL - price;
                if (actualRisk > 0)
                {
                    double newTP = price - actualRisk * RRRatio;
                    SetProfitTarget("Short", CalculationMode.Price, newTP);
                    Print(">>> SHORT FILLED: fill=" + price.ToString("F2")
                        + " SL=" + pendingSL.ToString("F2")
                        + " TP=" + newTP.ToString("F2")
                        + " R:R=" + RRRatio.ToString("F2"));
                }
                pendingSL = double.NaN;
                pendingBias = 0;
            }
        }

        /// <summary>
        /// Called when a position changes. Updates equity after trade closes.
        /// </summary>
        protected override void OnPositionUpdate(Position position, double averagePrice,
            int quantity, MarketPosition marketPosition)
        {
            if (marketPosition == MarketPosition.Flat && SystemPerformance.AllTrades.Count > 0)
            {
                Trade lastTrade = SystemPerformance.AllTrades[SystemPerformance.AllTrades.Count - 1];
                currentEquity += lastTrade.ProfitCurrency;
                Print("Trade closed: P&L=$" + lastTrade.ProfitCurrency.ToString("F2")
                    + " | Equity=$" + currentEquity.ToString("F2"));
            }
        }

        /// <summary>
        /// Close any open position. In NinjaTrader managed mode, this also
        /// cancels the associated StopLoss and ProfitTarget orders.
        /// Mirrors Pine Script's strategy.close_all().
        /// </summary>
        private void CloseAllPositions(string reason)
        {
            if (Position.MarketPosition == MarketPosition.Long)
                ExitLong(reason, "Long");
            else if (Position.MarketPosition == MarketPosition.Short)
                ExitShort(reason, "Short");
        }

        #region Properties

        // ── Session ──

        [NinjaScriptProperty]
        [Display(Name = "OR Start Time (HHMMSS)",
            Description = "Opening Range start. Default 93000 = 9:30 AM. Adjust to match your chart timezone (e.g. 83000 for Central Time).",
            GroupName = "1. Session", Order = 1)]
        public int ORStartTime { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "OR End Time (HHMMSS)",
            Description = "Opening Range end. Default 93500 = 9:35 AM. Adjust to match your chart timezone.",
            GroupName = "1. Session", Order = 2)]
        public int OREndTime { get; set; }

        // ── Strategy ──

        [NinjaScriptProperty]
        [Range(1, 240)]
        [Display(Name = "Setup Expiry (minutes)",
            Description = "Minutes after OR close before the day expires with no trade.",
            GroupName = "2. Strategy", Order = 1)]
        public int SetupExpiryMinutes { get; set; }

        [NinjaScriptProperty]
        [Range(0.1, 10.0)]
        [Display(Name = "Risk:Reward Ratio",
            Description = "TP distance as a multiple of SL distance.",
            GroupName = "2. Strategy", Order = 2)]
        public double RRRatio { get; set; }

        // ── Risk ──

        [NinjaScriptProperty]
        [Range(100, 1000000)]
        [Display(Name = "Starting Capital ($)",
            Description = "Your starting account balance. Used for position sizing (backtest + live first trade).",
            GroupName = "3. Risk", Order = 0)]
        public double StartingCapital { get; set; }

        [NinjaScriptProperty]
        [Range(1.0, 100.0)]
        [Display(Name = "Day 1 Risk %",
            Description = "Percentage of equity to risk on the first trading day.",
            GroupName = "3. Risk", Order = 1)]
        public double RiskDay1Pct { get; set; }

        [NinjaScriptProperty]
        [Range(1.0, 100.0)]
        [Display(Name = "Day 2 Risk %",
            Description = "Percentage of equity to risk on the second trading day.",
            GroupName = "3. Risk", Order = 2)]
        public double RiskDay2Pct { get; set; }

        [NinjaScriptProperty]
        [Range(1.0, 100.0)]
        [Display(Name = "Day 3+ Risk %",
            Description = "Percentage of equity to risk on day 3 and beyond (compounds).",
            GroupName = "3. Risk", Order = 3)]
        public double RiskDay3Pct { get; set; }

        // ── FVG ──

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "FVG Max Age (bars)",
            Description = "Maximum bars since FVG formed to still be considered valid.",
            GroupName = "4. FVG", Order = 1)]
        public int FVGMaxAgeBars { get; set; }

        [NinjaScriptProperty]
        [Range(0.0, 1.0)]
        [Display(Name = "FVG ATR Tolerance Multiplier",
            Description = "ATR fraction for FVG gap tolerance (0.1 = 10% of ATR).",
            GroupName = "4. FVG", Order = 2)]
        public double FVGToleranceATRMult { get; set; }

        [NinjaScriptProperty]
        [Range(1, 50)]
        [Display(Name = "ATR Period",
            Description = "Lookback period for ATR indicator used in FVG tolerance.",
            GroupName = "4. FVG", Order = 3)]
        public int ATRPeriod { get; set; }

        #endregion
    }
}
