# Harness Horse Tracker — Web App

A Flask web application for managing harness horse stables.
Track bills, race results, ownership splits, and P/L per owner.

## Local development

```bash
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000

## Deploy to Railway (free)

1. Create a free account at railway.app
2. Connect your GitHub repository
3. Railway auto-detects Python and deploys

Set these environment variables in Railway:
- SECRET_KEY — any long random string
- DATABASE_URL — Railway provides this automatically with Postgres addon
