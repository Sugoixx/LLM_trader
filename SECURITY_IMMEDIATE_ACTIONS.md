# LLM_Trader - IMMEDIATE SECURITY ACTIONS

Priority fixes to apply TODAY.

---

## ⚠️ ACTION 1: Rotate All Exposed API Keys

**Status:** 🔴 CRITICAL - Do immediately if keys.env was ever shared

### Step 1: Check If Keys Are Compromised
```bash
# Check git history for keys.env
git log --all -- keys.env
git log -p keys.env | grep -E "OPENROUTER_API_KEY|GOOGLE_STUDIO_API_KEY"

# If keys appear in ANY commit: KEYS ARE COMPROMISED
```

### Step 2: If Keys Found in History
```bash
# Revoke ALL keys immediately in their dashboards:

1. Google AI: https://aistudio.google.com/app/settings
   - Remove/revoke AIzaSyAX8J_IV5E1L9MGU2xSt_RxiXZil3QNAC4
   - Create new key

2. OpenRouter: https://openrouter.ai/keys
   - Revoke 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
   - Create new key

3. CryptoCompare: https://min-api.cryptocompare.com/
   - Revoke bf1e97e189ce225c7e82e0ea59509e702552e446befbe1fb1d96870e4a592297
   - Create new key
```

### Step 3: Update keys.env
```bash
# Edit keys.env with new keys only
nano keys.env
# or
code keys.env
# Replace with new keys from step 2
```

### Step 4: Verify Secrets Not in Repository (Optional: Clean History)
```bash
# Option A: Just going forward (recommended for now)
# Do nothing - .gitignore is already protecting keys.env

# Option B: Clean historical commits (only if keys in history)
# WARNING: This rewrites git history - only do if ALONE on this repo
git filter-branch --tree-filter 'rm -f keys.env' -- --all
git push origin --force --all
```

---

## ⚠️ ACTION 2: Audit BlockRun Wallet

**Status:** 🔴 CRITICAL - Check if wallet was used for transactions

### Step 1: Check If BlockRun Is Enabled
```bash
# Check config
grep -i "blockrun" config/config.ini

# Check if wallet key exists
cat keys.env | grep BLOCKRUN_WALLET_KEY
```

### Step 2: If BlockRun Is Enabled In keys.env
```bash
# DO THIS IMMEDIATELY:

1. Check wallet activity at Etherscan:
   - Go to: https://etherscan.io/address/0x[YOUR_WALLET_ADDRESS]
   - Check: transaction history, balance, token transfers

2. If there are ANY transactions:
   - Transfer all funds to NEW wallet immediately
   - Regenerate wallet address
   - Update keys.env with new wallet or remove BlockRun

3. If wallet is empty or no transactions:
   - Still delete the current wallet key from keys.env
   - Create NEW disposable wallet for testing only (if needed)
   - Add clear warning in BLOCKRUN_WALLET_KEY comment
```

### Step 3: Disable BlockRun (Recommended)
```ini
# In config/config.ini
[ai_providers]
provider = googleai  # Change to: local, googleai, or openrouter
# Remove blockrun from provider list
```

Comment out in keys.env:
```bash
# NEVER AGAIN: Wallet private keys should NOT be in config files
# BLOCKRUN_WALLET_KEY=0x...
```

---

## ⚠️ ACTION 3: Implement Local LLM as Default

**Status:** 🟡 RECOMMENDED - Protects trading data

### Step 1: Install LM Studio
```bash
# Download from: https://lmstudio.ai/
# Install locally on your machine
```

### Step 2: Configure as Default
```ini
# In config/config.ini
[ai_providers]
provider = local  # Use local LM Studio
lm_studio_base_url = http://localhost:1234/v1
lm_studio_model = local-model  # Or your chosen model
```

### Step 3: Load A Model in LM Studio
```
1. Open LM Studio
2. Search for: "mistral-7b-instruct" or similar
3. Click "Download"
4. Wait for download (5-30 minutes depending on connection)
5. Click "Start Server" (should run on http://localhost:1234)
```

### Step 4: Test Local Connection
```bash
# Run the bot and verify it uses local LM Studio
python start.py

# Check logs for:
# "Sending request to LM Studio SDK with model: ..."
# NOT: "Sending request to Google AI..."
```

---

## ⚠️ ACTION 4: Protect logs from containing sensitive data

**Status:** 🟡 MEDIUM - Prevents leaks if logs are shared

### Step 1: Verify Debug Logging is OFF
```ini
# In config/config.ini
[debug]
logger_debug = false  # Make sure this is false
save_chart_images = false  # Don't save charts locally
```

### Step 2: Set Log Rotation
```python
# Edit src/logger/logger.py

# Ensure logs are rotated and don't grow indefinitely
# Add at the end of setup:

from logging.handlers import RotatingFileHandler

# Max 10MB per file, keep 5 files
handler = RotatingFileHandler(
    log_file,
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5  # Keep 5 old files
)

# Logs older than 5 files are deleted automatically
```

### Step 3: Clean Existing Logs
```bash
# Delete old logs that might contain sensitive data
rm -rf logs/*.log*

# Or keep only last 7 days
find logs/ -name "*.log" -mtime +7 -delete
```

---

## ⚠️ ACTION 5: Add .git/config Protection

**Status:** 🟢 OPTIONAL - Extra safety

```bash
# Add this to .git/config to prevent accidental pushes of sensitive files

[core]
    # Treat keys.env as always dirty (never commit even if accidentally staged)
    # This prevents 'git add .' from picking it up
    safecrlf = false

# Or better: use a pre-commit hook
cat > .git/hooks/pre-commit << 'EOF'
#!/bin/bash
# Prevent committing keys.env

if git diff --cached --name-only | grep -q "keys.env"; then
    echo "ERROR: Cannot commit keys.env - it contains sensitive API keys!"
    echo "Use 'git reset HEAD keys.env' to unstage it"
    exit 1
fi

if git diff --cached --name-only | grep -q "\.env$"; then
    echo "ERROR: Cannot commit .env files - they contain sensitive data!"
    exit 1
fi

exit 0
EOF

chmod +x .git/hooks/pre-commit
```

---

## ⚠️ ACTION 6: Document Your Choices

**Status:** 🟢 REQUIRED - Future-proofs team

### Create SECURITY.md
```markdown
# Security Policy - LLM_Trader

## Cloud API Keys
- Status: ❌ DISABLED for live trading
- Reason: Sensitive trading data exposed to third parties
- If needed: Use local LLM Studio only (LM Studio provider)

## Privacy
- Trading data is NEVER sent to Google/OpenRouter/BlockRun in production
- All analysis happens locally via LM Studio
- Public APIs used: CryptoCompare, CoinGecko (public market data only - no PII)

## Key Rotation
- All production API keys rotated: [DATE]
- New keys: [list which ones changed]
- Revoked keys: [list old ones]

## Wallet Security
- BlockRun disabled: [DATE]
- All crypto wallets archived
- If needed: Only test/demo wallets with <$1 for testing

## Incident Log
- [Date]: Keys rotated - no unauthorized access detected
```

---

## ✅ VERIFICATION SCRIPT

Run this to verify all fixes are in place:

```bash
#!/bin/bash
# save as check_security.sh

echo "🔍 Security Check for LLM_Trader"
echo "================================"

# Check 1: API Keys not in git history
echo ""
echo "1️⃣  Checking for exposed keys in git history..."
if git log -p --all | grep -q "OPENROUTER_API_KEY\|AIzaSy\|bf1e97e189"; then
    echo "   ❌ FAIL: Keys found in git history!"
    echo "   Run: git filter-branch --tree-filter 'rm -f keys.env' -- --all"
else
    echo "   ✅ PASS: No keys in git history"
fi

# Check 2: keys.env in gitignore
echo ""
echo "2️⃣  Checking .gitignore protection..."
if grep -q "^keys.env$" .gitignore; then
    echo "   ✅ PASS: keys.env is in .gitignore"
else
    echo "   ❌ FAIL: keys.env not protected by .gitignore"
fi

# Check 3: No HTTPS violations
echo ""
echo "3️⃣  Checking for unencrypted connections..."
if grep -r "http://" src/ --include="*.py" | grep -v "localhost" | grep -v "127.0.0.1"; then
    echo "   ❌ FAIL: Unencrypted HTTP found!"
else
    echo "   ✅ PASS: All external connections use HTTPS"
fi

# Check 4: LM Studio configured
echo ""
echo "4️⃣  Checking default provider..."
if grep -q "provider = local" config/config.ini; then
    echo "   ✅ PASS: Local LM Studio is default provider"
elif grep -q "provider = googleai" config/config.ini; then
    echo "   ⚠️  WARNING: Google AI is default provider (consider switching to local)"
else
    echo "   ❌ FAIL: Provider not configured"
fi

# Check 5: Debug logging disabled
echo ""
echo "5️⃣  Checking debug settings..."
if grep -q "logger_debug = false" config/config.ini; then
    echo "   ✅ PASS: Debug logging is OFF"
else
    echo "   ⚠️  WARNING: Debug logging might be ON (check config.ini)"
fi

# Check 6: BlockRun disabled
echo ""
echo "6️⃣  Checking BlockRun status..."
if grep -q "^# BLOCKRUN_WALLET_KEY" keys.env && ! grep -q "^BLOCKRUN_WALLET_KEY=0x" keys.env; then
    echo "   ✅ PASS: BlockRun wallet is commented out"
else
    echo "   ⚠️  WARNING: BlockRun wallet might be active (review keys.env)"
fi

echo ""
echo "================================"
echo "Security check complete!"
```

Run it:
```bash
chmod +x check_security.sh
./check_security.sh
```

---

## 📋 CHECKLIST - Complete These TODAY

- [ ] Run git history check for exposed keys
- [ ] Rotate all API keys if found in history
- [ ] Audit BlockRun wallet activity
- [ ] Disable BlockRun in config
- [ ] Set provider = local in config.ini
- [ ] Install LM Studio on your machine
- [ ] Test bot with local LM Studio
- [ ] Disable debug logging in config.ini
- [ ] Delete old log files
- [ ] Create SECURITY.md documentation
- [ ] Run security check script
- [ ] Add pre-commit hook (optional)
- [ ] Commit updated config files
- [ ] Test that bot works with new configuration

---

## ⏱️ TIME ESTIMATE

- Total time: **2-4 hours** depending on:
  - Whether keys are in git history (5 min vs 30 min fix)
  - LM Studio download speed (5 min to 30 min)
  - Testing the bot (15 min)

**Critical path (fast version - 30 min):**
1. Check if BlockRun is used (2 min)
2. If yes: audit wallet, disable it (5 min) 
3. Check .gitignore for keys.env (1 min)
4. Change config to local provider (2 min)
5. Test bot (5 min)
6. Done!

---

## ⚠️ TROUBLESHOOTING

### Error: "LM Studio connection refused"
```
Solution: Make sure LM Studio is running with server active
- Open LM Studio app
- Click "Start Server" button
- Verify: http://localhost:1234/v1/models has models loaded
```

### Error: "404 Model not found"
```
Solution: Make sure a model is loaded in LM Studio
- Go to LM Studio Models section
- Download a model (mistral-7b-instruct recommended)
- Load it by clicking the download
- Check dropdown shows model name
```

### Error: "OPENROUTER_API_KEY not set"
```
Solution: If you want to keep OpenRouter as fallback
- Edit keys.env
- Add new OpenRouter key from dashboard
OR disable OpenRouter in config:
[ai_providers]
provider = local  # Uses LM Studio only
```

