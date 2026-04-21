# Fast Trading Mode — Roadmap & Implementation Log

> Document de référence pour le Fast Trading Mode et les améliorations
> de profitabilité (Tiers S / A / B / C).

---

## 1. Vue d'ensemble

**Fast Trading Mode** = trading direct sur consensus de stratégies classiques
(Bollinger, RSI, MA Crossover) sans attendre le cycle LLM complet.
Le LLM tourne toujours sur sa cadence normale et sert de **couche de correction**
(force-close si un signal opposé HIGH-confidence apparaît).

Toggle : onglet **Algo Strategies** du dashboard → bouton ⚡ FAST.

---

## 2. Architecture livrée

### Backend

| Fichier | Rôle |
|---|---|
| `src/trading/algo_strategies/fast_trader.py` | `AlgoFastTrader.decide()` — consensus regime-aware (ADX + mapping stratégie ↔ régime) |
| `src/trading/algo_strategies/safety_guard.py` | `FastTradingSafetyGuard.check()` — hard-gates runtime |
| `src/trading/trading_strategy.py` | `process_algo_decision()` — bypass LLM extraction, route directe `_open_new_position` / `_handle_existing_position` |
| `src/app.py` | `_execute_fast_trade_core()` — orchestration + `_broadcast_fast_guard_state()` |
| `src/dashboard/dashboard_state.py` | `fast_guard_state` + `update_fast_guard()` |
| `src/dashboard/routers/monitor.py` | `GET /api/monitor/fast_guard` |
| `src/dashboard/routers/settings.py` | `GET/POST /api/settings/fast-trading` |
| `src/config/loader.py` | Properties `FAST_*` |
| `config/config.ini` | Section `[fast_trading]` |

### Frontend

| Fichier | Rôle |
|---|---|
| `src/dashboard/static/modules/algo_panel.js` | Toggle + panneau safety-guard |
| `src/dashboard/static/modules/websocket.js` | Cases `fast_trading` + `fast_guard` |
| `src/dashboard/static/main.js` | Import `algo_panel.js?v=6.2` |
| `src/dashboard/static/index.html` | `#panel-fast-guard` |
| `src/dashboard/static/css/panels.css` | Styles `.fast-guard`, `.guard-row`, badges |

---

## 3. Tier S — Livré ✅

Protections indispensables pour ne pas **perdre** en mode rapide.

| # | Feature | Mécanisme | Défaut |
|---|---|---|---|
| S1 | **Regime filter** | Bollinger/RSI ⇒ RANGING ; MA ⇒ TRENDING ; block HIGH vol + ADX<20 chop ; block UNKNOWN + ADX=0 | — |
| S2 | **Min interval** | Anti-flip-flop entre trades | 900s (15 min) |
| S3 | **Daily loss limit** | Pause jusqu'à minuit UTC si PnL% journalier franchi | -3.0 % |
| S4 | **Loss-streak cooldown** | Pause après N pertes consécutives | 3 pertes → 7200s (2h) |
| S5 | **Trailing stop ATR** | Déjà actif dans execution engine | ATR×2.0 |
| S6 | **Break-even on TP1** | SL repasse à entry dès première partielle | activé |
| S7 | **Dashboard exposure** | Panneau temps réel (WS + REST) | — |

**Snapshot guard** (via `/api/monitor/fast_guard`) :
```json
{
  "last_trade_utc": "...",
  "daily_pnl_pct": 0.0,
  "consecutive_losses": 0,
  "cooldown_until_utc": null,
  "blocked_reason": null,
  "config": { "min_interval_seconds": 900, ... }
}
```

---

## 4. Tier A — À faire (gros impact, effort moyen) 🎯

Objectif : **améliorer la qualité des entrées** et **réduire les faux signaux**.

### A1 — Filtre multi-timeframe (MTF)
- Avant un BUY sur 15m, vérifier que la tendance 1h et 4h est haussière (ADX + EMA slope).
- Refus du trade si MTF contradictoire → réduit fortement les whipsaws.
- Fichier : nouveau `src/trading/algo_strategies/mtf_filter.py`.

### A2 — Volume / liquidité filter
- Refuser si volume < X× moyenne 20 périodes (marché mort).
- Refuser si spread > seuil (protection exécution).

### A3 — Session filter (horaires)
- Pas de fast trades pendant les heures creuses (02:00–06:00 UTC pour crypto, hors sessions London/NY pour forex).
- Lisible via `config.ini` → `[fast_trading] allowed_sessions = LONDON,NEW_YORK,ASIA`.

### A4 — Adaptive position sizing (Kelly light)
- Réduire la taille à 0.5× risk après 1 perte, 0.25× après 2, reset après 1 gain.
- Évite les drawdowns exponentiels pendant une mauvaise série.

### A5 — Confirmation bougie
- Attendre la **clôture** de la bougie qui produit le signal (pas d'entrée en intra-bougie).
- Plus fiable, un peu moins réactif.

### A6 — Correlation gate
- Bloquer un nouveau trade si **corrélé > 0.7** à une position ouverte (ex : BTC + ETH long).
- Évite la concentration involontaire de risque.

### A7 — News/event blackout
- Utiliser `rag/news_provider` pour bloquer les trades 15 min avant/après un event majeur (FOMC, CPI, etc.).

---

## 5. Tier B — À faire (impact moyen, effort moyen) 🛠

Objectif : **optimiser la gestion de position**.

### B1 — Scale-in pyramiding
- Ajouter à une position gagnante (max 2 ajouts) sur pullback 1×ATR.
- Ne jamais moyenner à la baisse.

### B2 — Dynamic SL/TP par ATR régime
- HIGH vol : SL=2.5×ATR, TP=4×ATR
- NORMAL : SL=1.5×ATR, TP=3×ATR
- LOW : SL=1×ATR, TP=2×ATR
- Table dans `risk_manager`.

### B3 — Partial TPs progressifs
- Remplacer les 2 partials par 3 : `0.33:0.33, 0.66:0.50, 1.0:1.0` (déjà commenté dans `config.ini`).
- Active par défaut.

### B4 — Anti-revenge cooldown
- Après un SL touché, interdire toute nouvelle entrée sur le même symbole pendant 30 min.

### B5 — Time-based exit
- Fermer toute position fast qui n'atteint pas TP1 après N bougies (ex : 12×15m = 3h).
- Évite les trades qui "pourrissent".

### B6 — Signal decay
- Un signal HIGH devient MEDIUM après 2 bougies non exécutées, LOW après 4, puis ignoré.

### B7 — Stratégie scoring & auto-disable
- Tracker win-rate par stratégie (Bollinger / RSI / MA) sur fenêtre glissante 30 trades.
- Désactiver auto une stratégie dont le WR tombe < 35 %.

---

## 6. Tier C — À faire (polish & observabilité) ✨

Objectif : **visibilité et confort**.

### C1 — Dashboard : graphique equity fast-mode
- Courbe PnL% cumulé des trades fast uniquement.
- Sparkline 24h/7d.

### C2 — Dashboard : heatmap signaux
- Grille symbol × stratégie, couleurs BUY/SELL/HOLD + confidence.

### C3 — Export CSV trades fast
- Bouton dans le panneau pour exporter l'historique fast-mode.

### C4 — Notifications (Telegram/Discord)
- Push sur : entry, TP hit, SL hit, guard blocked, cooldown trip.

### C5 — Backtest mode fast
- Mode replay : rejouer les N derniers jours en fast-only pour mesurer alpha vs LLM-only.

### C6 — Metrics Prometheus
- Expose `fast_trades_total`, `fast_blocked_total{reason=...}`, `fast_daily_pnl_pct`.

### C7 — A/B testing LLM correction
- Flag : activer/désactiver la couche LLM de correction.
- Comparer perf avec vs sans.

### C8 — Auto-tuning des seuils
- Grid search mensuel sur `min_interval_seconds`, `MIN_AGREE_RATIO`, `MIN_ADX_FOR_TREND`
  à partir de l'historique de trades.

---

## 7. Configuration de référence

```ini
[execution_engine]
enabled = true
trailing_enabled = true
trailing_atr_multiplier = 2.0
trailing_breakeven_on_tp1 = true
partial_enabled = false
partial_targets = 0.5:0.5, 1.0:1.0

[fast_trading]
min_interval_seconds = 900
daily_loss_pct_limit = -3.0
consecutive_loss_threshold = 3
consecutive_loss_cooldown_seconds = 7200
```

---

## 8. Priorités suggérées

Si tu veux maximiser l'EV avec un effort minimal :

1. **A5** (confirmation bougie) — quelques lignes, gros gain qualité.
2. **A4** (adaptive sizing) — protège les drawdowns.
3. **A1** (MTF filter) — élimine 30–40 % des faux signaux.
4. **B5** (time-based exit) — tue les trades zombies.
5. **B2** (dynamic SL/TP par régime) — optimise R:R.
6. **C4** (notifications) — visibilité opérationnelle.
7. Le reste au fil de l'eau.

---

*Dernière mise à jour : Tier S complet, livré et câblé bout-en-bout.*
