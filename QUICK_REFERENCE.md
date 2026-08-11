
# 🎯 IMMEDIATE FIX NEEDED

Your dev app shows "Internal Server Error" because:

❌ Missing volume permissions for service principal: 6faa8167-0a98-40c9-8e78-ae3173cbd9eb

## Run This SQL Now:

GRANT READ VOLUME ON VOLUME workspace.research_copilot.raw 
TO `6faa8167-0a98-40c9-8e78-ae3173cbd9eb`;

Then refresh: https://lakebase-app-yash-dev-7474658713176204.aws.databricksapps.com

---

# 📍 WHERE APPS GET DEPLOYED

## Current Setup
Both dev and prod deploy to: https://dbc-8b028016-8196.cloud.databricks.com

## The Flow:
1. Bundle reads source from: /Workspace/Users/inimitablelol@gmail.com/AI-Research-Learning-Copilot-OpenAlex/app
2. Deploys to workspace in databricks.yml (host field)
3. Copies source to: /Workspace/Users/<app-service-principal-id>/src/<deployment-id>
4. App runs with its own service principal and URL

---

# 🌍 DEPLOYING TO DIFFERENT WORKSPACE

YES! You can deploy prod to a different workspace:

## Edit databricks.yml:

targets:
  prod:
    workspace:
      host: https://your-other-workspace.cloud.databricks.com
    variables:
      app_name: lakebase-app-yash

## Then:
✅ Bundle syncs from YOUR local bundle root
✅ Creates app in THAT workspace  
✅ App runs there with separate service principal
✅ Needs separate permissions in that workspace

## Key Point:
The bundle doesn't care WHERE your source is - it reads from wherever you run 
'databricks bundle deploy' and syncs to the target workspace specified in the config.

---

# ✅ CHECKLIST FOR NEW WORKSPACE

1. Update databricks.yml with new workspace host
2. Run: databricks bundle deploy --target prod
3. Run: databricks apps start lakebase-app-yash
4. Run: databricks apps deploy lakebase-app-yash --source-code-path ./app
5. Grant volume permissions (SQL)
6. Create secret scope if needed
7. Test the app URL
