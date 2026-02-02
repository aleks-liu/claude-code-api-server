# Usage Examples

[← Back to README](../README.md)

## Table of Contents

- [Complete Workflow with cURL](#complete-workflow-with-curl)
- [Python Client Example](#python-client-example)
- [n8n Webhook Integration](#n8n-webhook-integration)

---

## Complete Workflow with cURL

```bash
# Configuration
SERVER="http://localhost:8000"
API_KEY="ccas_your_server_key_here"
ANTHROPIC_KEY="sk-ant-your_anthropic_key_here"

# 1. Create a ZIP archive of your code
cd /path/to/your/project
zip -r /tmp/project.zip . -x "*.git*" -x "node_modules/*" -x "venv/*"

# 2. Upload the archive
UPLOAD_RESPONSE=$(curl -s -X POST "$SERVER/v1/uploads" \
  -H "Authorization: Bearer $API_KEY" \
  -F "file=@/tmp/project.zip")

UPLOAD_ID=$(echo $UPLOAD_RESPONSE | jq -r '.upload_id')
echo "Upload ID: $UPLOAD_ID"

# 3. Create a job
JOB_RESPONSE=$(curl -s -X POST "$SERVER/v1/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-Anthropic-Key: $ANTHROPIC_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"upload_id\": \"$UPLOAD_ID\",
    \"prompt\": \"Analyze this codebase for security vulnerabilities. Focus on: 1) SQL injection, 2) XSS, 3) Authentication issues. Write detailed findings to security_report.json\",
    \"timeout_seconds\": 1800
  }")

JOB_ID=$(echo $JOB_RESPONSE | jq -r '.job_id')
echo "Job ID: $JOB_ID"

# 4. Poll for completion
while true; do
  STATUS_RESPONSE=$(curl -s "$SERVER/v1/jobs/$JOB_ID" \
    -H "Authorization: Bearer $API_KEY")

  STATUS=$(echo $STATUS_RESPONSE | jq -r '.status')
  echo "Status: $STATUS"

  if [ "$STATUS" = "COMPLETED" ] || [ "$STATUS" = "FAILED" ] || [ "$STATUS" = "TIMEOUT" ]; then
    echo "$STATUS_RESPONSE" | jq .
    break
  fi

  sleep 10
done

# 5. Extract output file (if any)
echo $STATUS_RESPONSE | jq -r '.output.files["security_report.json"]' | base64 -d > security_report.json
```

---

## Python Client Example

```python
import base64
import json
import time
from pathlib import Path
import httpx

class ClaudeCodeClient:
    def __init__(self, server_url: str, api_key: str, anthropic_key: str):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.anthropic_key = anthropic_key
        self.client = httpx.Client(timeout=60.0)

    def _headers(self, include_anthropic: bool = False) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if include_anthropic:
            headers["X-Anthropic-Key"] = self.anthropic_key
        return headers

    def upload(self, zip_path: Path) -> str:
        """Upload a ZIP archive and return upload_id."""
        with open(zip_path, "rb") as f:
            response = self.client.post(
                f"{self.server_url}/v1/uploads",
                headers=self._headers(),
                files={"file": (zip_path.name, f, "application/zip")}
            )
        response.raise_for_status()
        return response.json()["upload_id"]

    def create_job(
        self,
        upload_id: str,
        prompt: str,
        claude_md: str = None,
        timeout: int = 1800
    ) -> str:
        """Create a job and return job_id."""
        response = self.client.post(
            f"{self.server_url}/v1/jobs",
            headers=self._headers(include_anthropic=True),
            json={
                "upload_id": upload_id,
                "prompt": prompt,
                "claude_md": claude_md,
                "timeout_seconds": timeout
            }
        )
        response.raise_for_status()
        return response.json()["job_id"]

    def get_job(self, job_id: str) -> dict:
        """Get job status and results."""
        response = self.client.get(
            f"{self.server_url}/v1/jobs/{job_id}",
            headers=self._headers()
        )
        response.raise_for_status()
        return response.json()

    def wait_for_completion(
        self,
        job_id: str,
        poll_interval: int = 10,
        max_wait: int = 3600
    ) -> dict:
        """Wait for job to complete and return results."""
        start_time = time.time()

        while time.time() - start_time < max_wait:
            result = self.get_job(job_id)
            status = result["status"]

            if status in ("COMPLETED", "FAILED", "TIMEOUT"):
                return result

            time.sleep(poll_interval)

        raise TimeoutError(f"Job {job_id} did not complete within {max_wait}s")

    def analyze(self, zip_path: Path, prompt: str, **kwargs) -> dict:
        """Convenience method: upload, create job, wait for result."""
        upload_id = self.upload(zip_path)
        job_id = self.create_job(upload_id, prompt, **kwargs)
        return self.wait_for_completion(job_id)


# Usage
if __name__ == "__main__":
    client = ClaudeCodeClient(
        server_url="http://localhost:8000",
        api_key="ccas_your_key_here",
        anthropic_key="sk-ant-your_key_here"
    )

    result = client.analyze(
        zip_path=Path("./project.zip"),
        prompt="Find all SQL injection vulnerabilities"
    )

    print(f"Status: {result['status']}")
    print(f"Cost: ${result.get('cost_usd', 0):.4f}")
    print(f"Output: {result['output']['text'][:500]}...")

    # Decode output files
    for filename, content_b64 in result["output"]["files"].items():
        content = base64.b64decode(content_b64)
        print(f"\n{filename}:")
        print(content.decode("utf-8")[:500])
```

---

## n8n Webhook Integration

In n8n, create a workflow:

1. **Trigger**: Webhook or Schedule
2. **HTTP Request** (Upload):
   - Method: POST
   - URL: `http://your-server:8000/v1/uploads`
   - Header: `Authorization: Bearer {{$credentials.claudeApiKey}}`
   - Body: Form-Data with file
3. **HTTP Request** (Create Job):
   - Method: POST
   - URL: `http://your-server:8000/v1/jobs`
   - Headers: Auth + `X-Anthropic-Key`
   - Body: JSON with upload_id and prompt
4. **Wait** node: 30 seconds
5. **Loop**: Poll `/v1/jobs/{job_id}` until complete
6. **Output**: Process results
