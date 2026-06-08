from qfieldcloud_sdk import sdk
client = sdk.Client(url="https://app.qfield.cloud/api/v1/")
client.login("***REMOVED***", "***REMOVED***")

jobs = client.list_jobs("f829bc38-1f8c-4ea9-a891-521b0f67d58b")
for j in jobs[:10]:
    print(j.get("type"), "|", j.get("status"), "|", j.get("id"))