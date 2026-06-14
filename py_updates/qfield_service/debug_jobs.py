"""
debug_jobs.py
-------------
Quick debug helper: list the most recent QFieldCloud jobs for the configured
project. Credentials and the project id are read from .env via config.py —
never hardcode secrets here.
"""
from qfieldcloud_sdk import sdk

import config

client = sdk.Client(url=config.QFC_URL, token=config.QFC_TOKEN)
if not config.QFC_TOKEN:
    client.login(config.QFC_USER, config.QFC_PASS)

jobs = client.list_jobs(config.PROJECT_ID)
for j in jobs[:10]:
    print(j.get("type"), "|", j.get("status"), "|", j.get("id"))
