# How to Verify Bundle Deployment is Working

## ✅ Quick Status Check

Run this Python code to check both apps:

```python
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()

# Check dev app
dev = w.apps.get(name="lakebase-app-yash-dev")
print(f"Dev App: {dev.app_status.state.value}")
print(f"URL: {dev.url}")

# Check prod app
prod = w.apps.get(name="lakebase-app-yash")
print(f"Prod App: {prod.app_status.state.value}")
print(f"URL: {prod.url}")
```

**Expected Output:**
```
Dev App: RUNNING
URL: https://lakebase-app-yash-dev-7474658713176204.aws.databricksapps.com
Prod App: RUNNING
URL: https://lakebase-app-yash-7474658713176204.aws.databricksapps.com
```

---

## 🌐 Browser Test (Recommended)

### 1. Open the Dev App
**URL**: https://lakebase-app-yash-dev-7474658713176204.aws.databricksapps.com

1. Click the URL above (or copy-paste into browser)
2. Sign in with your Databricks credentials if prompted
3. You should see the Research Copilot chat interface
4. Try a test query: **"What is machine learning?"**

**✅ Success indicators:**
- Page loads without errors
- Chat interface appears
- Query returns a response (may take 10-20s on first query due to embedding model download)
- No authentication errors

### 2. Open the Prod App
**URL**: https://lakebase-app-yash-7474658713176204.aws.databricksapps.com

Repeat the same test as dev.

---

## 🔍 Deep Verification

### Check 1: Bundle Configuration
```bash
cd /Workspace/Users/inimitablelol@gmail.com/AI-Research-Learning-Copilot-OpenAlex
databricks bundle validate --target dev
```

**Expected**: `Validation OK!`

### Check 2: Apps List
```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()

apps = w.apps.list()
for app in apps:
    if 'lakebase-app-yash' in app.name:
        print(f"{app.name}: {app.compute_status.state.value} / {app.app_status.state.value}")
```

**Expected**: Both apps show `ACTIVE / RUNNING`

### Check 3: Recent Deployments
```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()

dev_deployments = w.apps.list_deployments(app_name="lakebase-app-yash-dev")
for dep in list(dev_deployments)[:3]:
    print(f"{dep.deployment_id}: {dep.status.state.value} - {dep.status.message}")
```

**Expected**: Latest deployment shows `SUCCEEDED - App started successfully`

### Check 4: Volume Permissions
```sql
SHOW GRANTS ON VOLUME workspace.research_copilot.raw;
```

**Expected**: Service principals should have `READ_VOLUME` permission:
- `e0c84985-f86f-4fac-a55d-e8f6459363f2` (prod - already granted)
- `6faa8167-0a98-40c9-8e78-ae3173cbd9eb` (dev - needs manual grant)

### Check 5: App Logs
```python
from databricks.sdk import WorkspaceClient
import time

w = WorkspaceClient()

# Get recent logs from dev app
logs = w.apps.get_app_deployment_status(
    app_name="lakebase-app-yash-dev",
    deployment_id=w.apps.get(name="lakebase-app-yash-dev").active_deployment.deployment_id
)

print("Recent events:")
for event in logs.deployment_events[:5]:
    print(f"  {event.timestamp}: {event.message}")
```

**Look for**: No errors, successful startup messages

---

## 🧪 Functional Tests

### Test 1: Basic Query (No Search)
```
User: "What is machine learning?"
Expected: General response from LLM (no search needed)
Duration: ~2-5 seconds
```

### Test 2: Search Query (First Time)
```
User: "Find papers about transformer architectures"
Expected: 
- First query downloads embedding model (~10-20s)
- Searches OpenAlex corpus
- Returns relevant papers with citations
Duration: 10-30 seconds (first query), 3-8 seconds (subsequent)
```

### Test 3: SQL Query
```
User: "Show me recent papers on neural networks"
Expected:
- Executes PostgreSQL query via tools
- Returns structured results
- Shows paper titles, authors, citations
```

---

## ❌ Common Issues

### Issue 1: App shows "UNAVAILABLE"
**Cause**: Compute not started or deployment failed

**Fix**:
```bash
databricks apps start lakebase-app-yash-dev
databricks apps deploy lakebase-app-yash-dev \
  --source-code-path /Workspace/Users/inimitablelol@gmail.com/AI-Research-Learning-Copilot-OpenAlex/app
```

### Issue 2: "404 Not Found" in browser
**Cause**: App is still starting up

**Fix**: Wait 30-60 seconds, then hard refresh (Ctrl+Shift+R)

### Issue 3: "FileNotFoundError: /Volumes/.../models/..."
**Cause**: Missing volume permissions for service principal

**Fix**: Run the GRANT command from Check 4 above

### Issue 4: Search takes forever on first query
**Expected**: First search downloads ~90MB embedding model from HuggingFace

**Not an issue**: Subsequent searches will be fast (3-8 seconds)

### Issue 5: "Authentication failed" errors in logs
**Cause**: Service principal lacks Foundation Model endpoint access

**Fix**: Foundation Model endpoints grant access by default - check the endpoint name in app.py is correct

---

## 📊 Success Criteria

✅ **All systems go when:**

1. ✅ `databricks bundle validate` passes
2. ✅ Both apps show `RUNNING` status
3. ✅ Both app URLs load in browser (after sign-in)
4. ✅ Test query returns a response
5. ✅ Search query executes and returns papers
6. ✅ No errors in app logs
7. ✅ Volume permissions granted to both service principals

---

## 🔄 Quick Reset (If Things Break)

```bash
cd /Workspace/Users/inimitablelol@gmail.com/AI-Research-Learning-Copilot-OpenAlex

# Re-deploy from scratch
databricks apps stop lakebase-app-yash-dev
databricks bundle deploy --target dev
databricks apps start lakebase-app-yash-dev
databricks apps deploy lakebase-app-yash-dev \
  --source-code-path /Workspace/Users/inimitablelol@gmail.com/AI-Research-Learning-Copilot-OpenAlex/app

# Check status
databricks apps get lakebase-app-yash-dev | grep state
```

---

## 📝 Monitoring Commands

```bash
# Watch app status
watch -n 5 'databricks apps get lakebase-app-yash-dev | grep -E "(state|message)"'

# Tail logs (if available)
databricks apps logs lakebase-app-yash-dev --follow

# Check recent deployments
databricks apps list-deployments lakebase-app-yash-dev
```