# Deployment Guide - Research Copilot App

## Using Declarative Automation Bundles (DAB)

This project uses Databricks Asset Bundles for reproducible deployments across environments.

### Quick Start

#### 1. Deploy to Dev
```bash
cd /Workspace/Users/inimitablelol@gmail.com/AI-Research-Learning-Copilot-OpenAlex
databricks bundle deploy --target dev
databricks apps start lakebase-app-yash-dev
databricks apps deploy lakebase-app-yash-dev \
  --source-code-path /Workspace/Users/inimitablelol@gmail.com/AI-Research-Learning-Copilot-OpenAlex/app
```

#### 2. Deploy to Production
```bash
databricks bundle deploy --target prod
databricks apps start lakebase-app-yash
databricks apps deploy lakebase-app-yash \
  --source-code-path /Workspace/Users/inimitablelol@gmail.com/AI-Research-Learning-Copilot-OpenAlex/app
```

### Grant Permissions

After deploying a new environment, grant volume access to the app's service principal:

**For Dev:**
```sql
GRANT READ VOLUME ON VOLUME workspace.research_copilot.raw 
TO `6faa8167-0a98-40c9-8e78-ae3173cbd9eb`;
```

**For Prod:**
```sql
GRANT READ VOLUME ON VOLUME workspace.research_copilot.raw 
TO `e0c84985-f86f-4fac-a55d-e8f6459363f2`;
```

### Environment Configuration

The bundle defines two targets:

* **dev** (default): `lakebase-app-yash-dev`
  * Development mode with source-linked deployment
  * Changes sync automatically
  
* **prod**: `lakebase-app-yash`
  * Production mode with snapshot deployment
  * Requires explicit deployment

### App URLs

* **Dev**: https://lakebase-app-yash-dev-7474658713176204.aws.databricksapps.com
* **Prod**: https://lakebase-app-yash-7474658713176204.aws.databricksapps.com

### Common Commands

```bash
# Validate bundle
databricks bundle validate

# Show deployment plan
databricks bundle plan --target dev

# Deploy
databricks bundle deploy --target prod

# Check app status
databricks apps get lakebase-app-yash-dev

# View app logs
databricks apps logs lakebase-app-yash-dev

# Stop app
databricks apps stop lakebase-app-yash-dev
```

### Secrets Configuration

The app expects the following secret:

```bash
# Lakebase PostgreSQL connection string
databricks secrets put --scope lakebase --key openalex_pgvector_url
```

### Architecture

```
┌─────────────────────────────────────────────────┐
│ Databricks App (Flask)                          │
│  • lakebase-app-yash (prod)                     │
│  • lakebase-app-yash-dev (dev)                  │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │ Agent Loop (loop.py)                     │   │
│  │  • OpenAI-compatible adapter             │   │
│  │  • Tool calling orchestration            │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │ Tools (tools.py)                         │   │
│  │  • Hybrid search (vector + full-text)    │   │
│  │  • Embedding model (HuggingFace fallback)│   │
│  │  • PostgreSQL query execution            │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
                      │
                      ├─► Foundation Model Endpoint
                      │   (databricks-meta-llama-3-3-70b-instruct)
                      │
                      └─► Lakebase PostgreSQL
                          (OpenAlex corpus with pgvector)
```

### Troubleshooting

#### App won't start
* Check compute is ACTIVE: `databricks apps get <app-name>`
* Verify source code path is absolute
* Check app logs: `databricks apps logs <app-name>`

#### Authentication errors
* Verify service principal has volume permissions
* Check secret scope access
* Confirm MODEL_ENDPOINT is correct

#### Search not working
* First search will download embedding model from HuggingFace (~10-20s)
* Subsequent searches use cached model
* Check Lakebase connection string is valid

#### 404 errors in browser
* App may still be starting (check status)
* Hard refresh: Ctrl+Shift+R
* Check deployment succeeded: look for "App started successfully"