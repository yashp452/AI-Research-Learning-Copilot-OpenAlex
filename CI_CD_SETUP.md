# CI/CD Setup Guide - Production Ready

## 🎯 The Problem You Identified

**Current Setup (NOT production-ready):**
```
Developer edits workspace → Manual deploy → Apps update
```
- ❌ Tied to user's workspace path
- ❌ Manual deployment required
- ❌ No automated testing
- ❌ No deployment history
- ❌ Single point of failure (your account)

**New CI/CD Setup (Production-ready):**
```
Push to GitHub main → Auto-deploy to Dev → Test → Manual approve → Deploy to Prod
```
- ✅ GitHub is source of truth
- ✅ Automatic dev deployments
- ✅ Manual prod approvals
- ✅ Full audit trail
- ✅ Team can deploy (not just you)

---

## 📋 One-Time Setup (Do This Once)

### Step 1: Create Databricks Access Token

1. Go to Databricks workspace settings
2. Navigate to: **User Settings → Developer → Access tokens**
3. Click **Generate new token**
4. Name it: `GitHub_Actions_Deploy`
5. Lifetime: 90 days (or longer)
6. Copy the token (you won't see it again!)

**Important:** This token lets GitHub deploy apps. Keep it secure!

### Step 2: Add Secrets to GitHub Repository

**Important:** Find your workspace URL by looking at your browser address bar when logged into Databricks.
It will be something like: `https://dbc-XXXXXXXX-XXXX.cloud.databricks.com`

1. Go to your GitHub repository: `https://github.com/yashp452/AI-Research-Learning-Copilot-OpenAlex`
2. Navigate to: **Settings** (repository settings, not your personal settings) **→ Secrets and variables → Actions**
3. Click **New repository secret**

Add these two secrets:

| Secret Name | Value | Notes |
| --- | --- | --- |
| `DATABRICKS_HOST` | `https://dbc-8b028016-8196.cloud.databricks.com` | Your workspace URL (check browser address bar) |
| `DATABRICKS_TOKEN` | The token you copied from Step 1 | Keep this secure! |

**Why these are needed:**
- `DATABRICKS_HOST` tells the CLI which workspace to deploy to
- `DATABRICKS_TOKEN` authenticates the deployment

**Current Setup:** Both dev and prod apps deploy to the SAME workspace but in separate containers.

**Screenshot guide:**
```
GitHub Repo → Settings (tab at top) → Secrets and variables → Actions → New repository secret
```

**⚠️ Common Mistake:** Make sure you're in the **repository settings**, not your personal GitHub settings!

### Step 3: Set Up GitHub Environments (Optional but Recommended)

This adds an extra approval step for prod deployments.

1. Go to: **Settings → Environments**
2. Create environment: `dev`
   - No protection rules needed
3. Create environment: `production`
   - Check: **Required reviewers**
   - Add yourself (or team members who can approve prod deploys)

---

## 🚀 New Workflow

### Development Cycle

```bash
# 1. Create feature branch
git checkout -b feature/improved-search

# 2. Make changes
vim app/tools.py

# 3. Test locally (optional)
python app/app.py

# 4. Commit and push
git add .
git commit -m "Improve search results"
git push origin feature/improved-search

# 5. Create Pull Request on GitHub
# Review with team → Merge to main

# 6. GitHub Actions AUTOMATICALLY deploys to dev!
#    (No manual step needed)
```

**What happens automatically:**
1. You merge PR to main
2. GitHub Actions triggers
3. Code deployed to `lakebase-app-yash-dev`
4. You get notification (success/failure)

### Production Release

When dev is tested and ready:

1. Go to GitHub: **Actions → Deploy to Production**
2. Click **Run workflow**
3. Type `deploy` in the confirmation box
4. Click **Run workflow**
5. (If you set up environments) Approve the deployment
6. GitHub Actions deploys to `lakebase-app-yash`

---

## 📊 Workflow Details

### Auto-Deploy to Dev (`.github/workflows/deploy-dev.yml`)

**Triggers:**
- Every push to `main` branch
- Manual trigger via GitHub Actions UI

**Steps:**
1. Checkout code from GitHub
2. Install Databricks CLI (official GitHub Action)
3. Resolve workspace path (uses shared location: `/Workspace/Shared/apps/ci-cd/`)
4. **Sync app source to shared workspace** (uploads ./app to shared location)
5. Deploy to `lakebase-app-yash-dev` from shared workspace path
6. Report status with deployment summary

**Deployment time:** ~2-3 minutes

**Key insights:** 
- The deploy command requires source code to be IN THE WORKSPACE, not on the GitHub runner
- Uses shared workspace location (`/Workspace/Shared/apps/ci-cd/`) instead of user-specific paths
- Any team member can deploy without path conflicts

### Manual Deploy to Prod (`.github/workflows/deploy-prod.yml`)

**Triggers:**
- Manual trigger only (via GitHub Actions UI)
- Requires typing "deploy" to confirm

**Steps:**
1. Validate confirmation
2. (Optional) Wait for approval if environment protection enabled
3. Checkout code from GitHub
4. Install Databricks CLI
5. Deploy to `lakebase-app-yash`
6. Verify deployment
7. Report status

**Deployment time:** ~2-3 minutes (+ approval wait time)

---

## 🔒 Security Benefits

### Before (User Workspace Path)
- ❌ Deployment tied to your Databricks account
- ❌ If you leave the team, deployments break
- ❌ No separation between dev work and prod source
- ❌ Anyone with workspace access can modify source

### After (GitHub CI/CD)
- ✅ GitHub is source of truth
- ✅ Team members can deploy with proper GitHub access
- ✅ All changes tracked in Git history
- ✅ Deployment token can be rotated independently
- ✅ Pull request review before any prod changes
- ✅ Audit trail of who deployed what and when

---

## 🧪 Testing the Setup

### Test 1: Dev Auto-Deploy

```bash
# Make a trivial change
echo "# Test change" >> app/README.md

# Commit and push to main
git add app/README.md
git commit -m "Test CI/CD: dev auto-deploy"
git push origin main

# Check GitHub Actions
# Go to: GitHub → Actions → Watch the "Deploy to Dev" workflow
```

Expected result: Dev app updates automatically in 2-3 minutes

### Test 2: Prod Manual Deploy

1. Go to GitHub: **Actions → Deploy to Production**
2. Click **Run workflow**
3. Type `deploy`
4. Click **Run workflow**
5. Watch the workflow execute

Expected result: Prod app updates after manual confirmation

---

## 📈 Monitoring Deployments

### Via GitHub Actions
- **Real-time logs:** GitHub → Actions → Click on workflow run
- **Deployment history:** All runs listed with status
- **Failure notifications:** GitHub emails you on failures

### Via Databricks
- **Check app status:**
  ```bash
  databricks apps get lakebase-app-yash-dev
  databricks apps get lakebase-app-yash
  ```
- **View app logs:**
  ```bash
  databricks apps logs lakebase-app-yash-dev
  databricks apps logs lakebase-app-yash
  ```

---

## 🆘 Troubleshooting

### "Error: Invalid credentials"
- **Cause:** GitHub secrets not set or token expired
- **Fix:** 
  1. Generate new Databricks token
  2. Update `DATABRICKS_TOKEN` secret in GitHub
  3. Re-run workflow

### "Error: App not found"
- **Cause:** App name mismatch
- **Fix:** Check app names match exactly:
  - Dev: `lakebase-app-yash-dev`
  - Prod: `lakebase-app-yash`

### "Error: Permission denied"
- **Cause:** Databricks token doesn't have app deployment permissions
- **Fix:**
  1. Create token from an account with app permissions
  2. Or grant permissions to the service principal
  3. Update `DATABRICKS_TOKEN` secret

### Dev workflow not triggering
- **Cause:** Workflow file not in main branch
- **Fix:** Make sure `.github/workflows/deploy-dev.yml` exists in main

### Want to disable auto-deploy temporarily?
- **Option 1:** Comment out the `push:` trigger in `deploy-dev.yml`
- **Option 2:** Disable the workflow in GitHub Actions UI

---

## 🔄 Migration from Old Workflow

### What Changes

**Before:**
```bash
# From your workspace
cd /Workspace/Users/inimitablelol@gmail.com/AI-Research-Learning-Copilot-OpenAlex
databricks apps deploy lakebase-app-yash-dev --source-code-path ./app
```

**After:**
```bash
# From anywhere - just push to GitHub!
git push origin main
# Dev automatically deploys
# Use GitHub UI for prod deploys
```

### Clean Up (Optional)

You can now work from anywhere, not just Databricks workspace:

```bash
# Clone to your local machine
git clone https://github.com/yashp452/AI-Research-Learning-Copilot-OpenAlex.git
cd AI-Research-Learning-Copilot-OpenAlex

# Make changes locally
vim app/tools.py

# Push to GitHub
git add .
git commit -m "Update from local machine"
git push origin main
# Dev automatically deploys!
```

The workspace Git folder is no longer required for deployments!

---

## 🎯 Next Steps

1. **Set up secrets** (5 minutes)
   - DATABRICKS_HOST
   - DATABRICKS_TOKEN

2. **Test dev auto-deploy** (make a small change to main)

3. **Test prod manual deploy** (use GitHub Actions UI)

4. **Set up environment protection** (optional, for approval workflow)

5. **Update team on new process** (no more manual deploys!)

---

## 🏢 Advanced: Multi-Workspace Setup (Optional)

**Current Setup:** Both dev and prod in the SAME workspace (https://dbc-8b028016-8196.cloud.databricks.com)

**Enterprise Setup:** Dev and prod in SEPARATE workspaces

If you ever need to deploy dev and prod to different workspaces:

### Using GitHub Environments

1. **Set up GitHub Environments:**
   - Go to: Repository Settings → Environments
   - Create environment: `dev`
   - Create environment: `production`

2. **Add environment-specific secrets:**

   For `dev` environment:
   ```
   DATABRICKS_HOST = https://dev-workspace.cloud.databricks.com
   DATABRICKS_TOKEN = <dev-workspace-token>
   ```

   For `production` environment:
   ```
   DATABRICKS_HOST = https://prod-workspace.cloud.databricks.com
   DATABRICKS_TOKEN = <prod-workspace-token>
   ```

3. **Update workflow files to use environments:**

   In `.github/workflows/deploy-dev.yml`:
   ```yaml
   jobs:
     deploy-dev:
       environment: dev  # ← Add this line
       runs-on: ubuntu-latest
   ```

   In `.github/workflows/deploy-prod.yml`:
   ```yaml
   jobs:
     deploy-prod:
       environment: production  # ← Add this line
       runs-on: ubuntu-latest
   ```

   GitHub will automatically use the environment-specific secrets!

### Benefits of Multi-Workspace:
- ✅ Complete isolation between dev and prod
- ✅ Different access controls per workspace
- ✅ Separate costs and resource management
- ✅ Can have different workspace configurations

### Downsides:
- ❌ More complex to manage
- ❌ Need separate tokens and permissions
- ❌ Higher cost (two workspaces)
- ❌ Data not shared between dev and prod

**Recommendation:** Start with single workspace (current setup). Move to multi-workspace only when you need complete isolation.

---

## 📚 Additional Resources

- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [Databricks Apps CLI Reference](https://docs.databricks.com/en/dev-tools/cli/apps-cli.html)
- [Databricks Token Management](https://docs.databricks.com/en/dev-tools/auth/pat.html)
- [GitHub Environments](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment)

---

## 🔄 Pipeline Orchestration (NEW!)

### Overview

The **deploy-pipeline.yml** workflow orchestrates your 4-stage Research Copilot Pipeline:

1. **ingest_openalex** - Fetch papers from OpenAlex API → bronze table
2. **build_silver** - Clean data, reconstruct abstracts → silver tables
3. **embed_gold** - Generate MiniLM embeddings → gold table
4. **load_lakebase** - Upsert into Lakebase PostgreSQL

### Trigger Options

**1. Automatic (Push to main)**
```bash
# Any change to notebooks or databricks.yml triggers deployment
git push origin main
```

**2. Scheduled (Weekly)**
- Runs every Sunday at 6:00 AM IST
- Keeps your research corpus fresh
- Enable by setting `pause_status: UNPAUSED` in databricks.yml

**3. Manual (with parameters)**
1. Go to: **Actions → Deploy and Run Research Pipeline**
2. Click **Run workflow**
3. Override parameters:
   - `limit_rows`: 0 = unlimited, 100 = testing
   - `max_pages`: 100 = ~20K papers
   - `concept_id`: C119857082 = Machine Learning
   - `from_date`: 2023-01-01

### Parameters Explained

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `limit_rows` | 0 | Papers to embed (0 = unlimited) |
| `max_pages` | 100 | OpenAlex pages to fetch (~200 papers/page) |
| `concept_id` | C119857082 | Filter by OpenAlex concept (ML) |
| `from_date` | 2023-01-01 | Only papers published after this date |

### Testing First Run

Use small parameters to verify everything works:

```yaml
limit_rows: 100      # Embed only 100 papers
max_pages: 1         # Fetch only 1 page (~200 papers)
concept_id: C119857082
from_date: 2024-01-01  # Recent papers only
```

This completes in ~10-15 minutes and uses minimal compute.

### Production Run

For weekly scheduled runs:

```yaml
limit_rows: 0        # Embed everything
max_pages: 100       # ~20K papers
concept_id: C119857082
from_date: 2023-01-01
```

### Monitoring

The workflow monitors the job run and reports:
- Current state (RUNNING, TERMINATED)
- Result (SUCCESS, FAILED)
- Run URL (link to Databricks UI)
- All parameters used
- Task-by-task progress

Maximum wait: 2 hours

### Common Issues

**OpenAlex API Rate Limit**
- Add longer delays in notebook 01
- Reduce `max_pages`

**Missing Volume/Table**
- Run notebooks manually first to create structure
- Verify databricks.yml paths match

**Lakebase Connection**
- Check secret scope and key names
- Verify Lakebase endpoint is running

**Embedding Model Download**
- First run downloads model (slow)
- Subsequent runs use cached model

### Cost Considerations

**Free Edition Compute Quota:**
- Weekly runs are reasonable
- Daily runs may exhaust quota
- Use small parameters for testing

---

## 📋 Complete Workflow Summary

You now have **3 GitHub Actions workflows**:

1. **deploy-dev.yml** - Auto-deploy dev app on push
2. **deploy-prod.yml** - Manual deploy production app
3. **deploy-pipeline.yml** - Orchestrate data pipeline

All workflows:
✅ Use shared workspace locations (`/Workspace/Shared/apps/ci-cd/`)
✅ Use Databricks CLI official GitHub Action
✅ Report detailed status
✅ Are production-ready

Your entire ML research platform is now **fully automated**! 🎉
