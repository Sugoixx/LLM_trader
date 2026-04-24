# Race Condition Fix — Concurrent Position Protection

## Problem
The bot was opening 2 opposite positions simultaneously due to missing synchronization between:
1. **AI Analysis Loop** (`process_analysis()`) — checks `if not self.current_position:` then opens
2. **Manual Dashboard Trade** (`manual_open_position()`) — same check, then opens

Without a lock, both threads could pass the guard and create positions concurrently.

**User Report**: "il a ouvert 2 position opposé???" (opened 2 opposite positions)

## Solution: asyncio.Lock

Added `asyncio.Lock` to serialize all position modifications:

```python
class TradingStrategy:
    def __init__(self, ...):
        self._position_lock = asyncio.Lock()  # Line 71
```

### Protected Methods

All methods that modify `self.current_position` are now wrapped with `async with self._position_lock:`:

1. **`process_analysis()`** (Line 335-337)
   - Wraps call to `_process_analysis_inner()` under lock
   - Blocks manual trades during AI analysis

2. **`check_position()`** (Line 101-129)
   - Protects SL/TP hit detection & close
   - Blocks manual closes & other operations during SL/TP check

3. **`manual_open_position()`** (Line 735-800)
   - Guard check + entire body under lock
   - Prevents concurrent open_order() calls

4. **`manual_close_position()`** (Line 803-822)
   - Wraps close operation under lock
   - Prevents concurrent close_order() calls

### Internal Protected Methods

- **`close_position()`** — Always called from within locked contexts above
- **`_process_analysis_inner()`** — Inner impl, called only from `process_analysis()` under lock

## Lock Scope & Semantics

```
AI Analysis Loop              Manual Dashboard              SL/TP Detection
     ↓                              ↓                             ↓
process_analysis()          manual_open_position()      check_position()
     ↓                              ↓                             ↓
async with _position_lock    async with _position_lock  async with _position_lock
     ↓                              ↓                             ↓
  (only one at a time)         (only one at a time)          (only one at a time)
```

**Guarantees:**
- Only ONE concurrent position operation ever
- No race window for dual opposite positions
- SL/TP hits never race with manual closes
- Manual opens never race with AI analysis

## Testing

All 150 tests pass:
```bash
.venv\Scripts\python.exe -m pytest tests/ -x -q
150 passed in 35.58s
```

**Test Coverage:**
- Brain integration tests (position lifecycle)
- Dashboard manual trade tests
- Trading strategy tests
- No new test failures

## Deployment

No config changes required. Lock is internal to `TradingStrategy`.

**Verification:**
1. Start bot with manual dashboard enabled
2. Rapid-click BUY + SELL buttons → only one position opens
3. Auto-analysis + manual trade clicks simultaneously → no dual positions
4. SL/TP hit while manual close pending → gracefully handled

## Files Changed

1. **`src/trading/trading_strategy.py`**
   - Line 71: Added `self._position_lock = asyncio.Lock()`
   - Lines 335-337: Wrapped `process_analysis()` → calls `_process_analysis_inner()` under lock
   - Lines 101-129: Wrapped `check_position()` with lock
   - Lines 735-800: Wrapped `manual_open_position()` with lock (including guard + entire body)
   - Lines 803-822: Wrapped `manual_close_position()` with lock

## Known Limitations

- **Lock contention**: If dashboard + SL/TP + AI analysis all fire simultaneously, one waits for others
  - Acceptable: SL/TP is ~1-2ms, manual trade ~100-200ms, analysis ~5-10s
  - Worst case: dashboard waits for AI analysis, then SL/TP, ~15s total — user can cancel
  
- **No RWLock**: Could allow concurrent reads (e.g., dashboard snapshot while AI analysis runs)
  - Not implemented: Complexity vs. benefit — lock is rarely held >10s

## Follow-up Improvements (Optional)

- Add lock timeout with warning: `async with asyncio.timeout(30):`
- Add lock duration metrics to dashboard
- Consider RWLock for read-only position snapshots (dashboard monitoring)
- Add audit log of lock contentions
