"""AI integration test — agent mode with skill invocation and script execution.

Verifies the full agent-mode pipeline end-to-end:

  Layer 1 (Agent):  Creates a JSON file with {"agent": "ok"}
  Layer 2 (Skill):  Updates the JSON file with {"skill": "ok"}
  Layer 3 (Script): Bundled Python script adds {"script": "ok"}

Final verification: the output JSON has all three keys set to "ok".

Requires ANTHROPIC_API_KEY to be set.
"""

import base64
import io
import json
import os
import time

import pytest

from helpers.test_data import make_valid_zip, random_suffix

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not ANTHROPIC_API_KEY,
    reason="ANTHROPIC_API_KEY not set — skipping AI integration test",
)

JSON_FILENAME = "agent-skill-script-test.json"
POLL_INTERVAL = 10  # seconds
MAX_WAIT = 300  # seconds


# ── Content generators ───────────────────────────────────────────


def _agent_content(agent_name: str, skill_name: str) -> str:
    """Agent .md: create JSON file, then invoke skill."""
    return (
        f"---\n"
        f"name: {agent_name}\n"
        f"description: E2E test agent for agent-skill-script pipeline\n"
        f"tools: Read, Write, Edit, Bash, Glob, Grep, Task, Skill, WebFetch, WebSearch, NotebookEdit\n"
        f"---\n"
        f"\n"
        f"You are a test agent. Follow these instructions EXACTLY in order:\n"
        f"\n"
        f"Step 1: Create a file called `{JSON_FILENAME}` with exactly this JSON content:\n"
        f"```json\n"
        f'{{"agent": "ok"}}\n'
        f"```\n"
        f"Use the Write tool to create this file.\n"
        f"\n"
        f"Step 2: After creating the file, invoke the skill named `{skill_name}`.\n"
        f"Use the Skill tool with skill: \"{skill_name}\".\n"
        f"If the skill name is not found, try `ccas-plugin:{skill_name}`.\n"
        f"\n"
        f"Step 3: After the skill completes, your work is done. Do not modify the JSON file further.\n"
        f"\n"
        f"CRITICAL RULES:\n"
        f"- You MUST create the JSON file FIRST, then invoke the skill.\n"
        f"- Do NOT skip any step.\n"
        f"- Do NOT modify the JSON file after the skill runs.\n"
    )


def _skill_content(skill_name: str) -> str:
    """SKILL.md: update JSON with skill marker, then run bundled script."""
    return (
        f"---\n"
        f"name: {skill_name}\n"
        f"description: E2E test skill for agent-skill-script pipeline\n"
        f"---\n"
        f"\n"
        f"Follow these instructions EXACTLY in order:\n"
        f"\n"
        f"Step 1: Use the Read tool to read the file `{JSON_FILENAME}` from the\n"
        f"current working directory. The file already exists and contains JSON\n"
        f'like {{"agent": "ok"}}. You MUST preserve all existing keys.\n'
        f"Use the Edit tool to add a new JSON key `\"skill\"` with value `\"ok\"`\n"
        f"to the existing object. The file should then contain BOTH the original\n"
        f'`\"agent\"` key AND the new `\"skill\"` key, for example:\n'
        f'{{"agent": "ok", "skill": "ok"}}\n'
        f"Do NOT overwrite the file — merge the new key into the existing JSON.\n"
        f"\n"
        f"Step 2: Run the Python script bundled with this skill.\n"
        f"The script is located at `scripts/update_json.py` relative to this\n"
        f"skill's base directory (shown at the top of these instructions).\n"
        f"Use Bash to run:\n"
        f"```\n"
        f"python3 <BASE_DIRECTORY>/scripts/update_json.py\n"
        f"```\n"
        f"Replace `<BASE_DIRECTORY>` with the actual base directory path from above.\n"
        f"\n"
        f"Step 3: After the script completes, your work is done.\n"
        f"\n"
        f"CRITICAL: You MUST preserve all existing keys when updating the JSON file.\n"
        f"Read the file first, then add the new key while keeping everything else.\n"
    )


BUNDLED_SCRIPT = f"""\
#!/usr/bin/env python3
\"\"\"Update {JSON_FILENAME} with script=ok.\"\"\"
import json

filename = "{JSON_FILENAME}"

with open(filename, "r") as f:
    data = json.load(f)

data["script"] = "ok"

with open(filename, "w") as f:
    json.dump(data, f, indent=2)

print(f"Script updated {{filename}} — keys: {{list(data.keys())}}")
"""


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def e2e_setup(api, admin_headers):
    """Create test agent and skill for the E2E pipeline test."""
    suffix = random_suffix()
    agent_name = f"test-e2e-agent-{suffix}"
    skill_name = f"test-e2e-skill-{suffix}"

    # Create agent with custom instructions
    agent_content = _agent_content(agent_name, skill_name)
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": agent_name,
        "content": agent_content,
    })
    assert resp.status_code in (201, 409), f"Agent creation failed: {resp.text}"

    # Create skill ZIP with custom SKILL.md and bundled Python script
    zip_bytes = make_valid_zip({
        f"{skill_name}/SKILL.md": _skill_content(skill_name),
        f"{skill_name}/scripts/update_json.py": BUNDLED_SCRIPT,
    })
    resp = api.post(
        "/v1/admin/skills",
        headers=admin_headers,
        files={"skill_data": ("skill.zip", io.BytesIO(zip_bytes), "application/zip")},
    )
    assert resp.status_code in (201, 409), f"Skill creation failed: {resp.text}"

    yield {
        "agent_name": agent_name,
        "skill_name": skill_name,
    }

    # Teardown
    api.delete(f"/v1/admin/agents/{agent_name}", headers=admin_headers)
    api.delete(f"/v1/admin/skills/{skill_name}", headers=admin_headers)


@pytest.fixture(scope="module")
def e2e_client_headers(api, admin_headers):
    """Create a test client for the E2E pipeline test."""
    cid = f"test-client-e2e-{random_suffix()}"
    resp = api.post("/v1/admin/clients", headers=admin_headers, json={
        "client_id": cid,
        "description": "E2E agent-skill-script test client",
        "role": "client",
    })
    assert resp.status_code == 201
    key = resp.json()["api_key"]

    yield {"Authorization": f"Bearer {key}"}

    api.delete(f"/v1/admin/clients/{cid}", headers=admin_headers)


# ── Tests ────────────────────────────────────────────────────────


def test_agent_skill_script_pipeline(api, e2e_setup, e2e_client_headers, admin_headers):
    """Full E2E: agent creates JSON → skill updates it → script updates it → verify all 3 keys."""
    agent_name = e2e_setup["agent_name"]
    headers = {**e2e_client_headers, "X-Anthropic-Key": ANTHROPIC_API_KEY}

    # ── Submit job in agent mode ─────────────────────────────────
    resp = api.post("/v1/jobs", headers=headers, json={
        "prompt": (
            "Execute the test pipeline now. "
            "Create the JSON file, then invoke the skill exactly as instructed."
        ),
        "agent": agent_name,
        "timeout_seconds": MAX_WAIT,
    })
    assert resp.status_code == 202, f"Job submission failed: {resp.text}"
    job_id = resp.json()["job_id"]

    # ── Verify agent field is persisted on the job ───────────────
    get_resp = api.get(f"/v1/jobs/{job_id}", headers=e2e_client_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["agent"] == agent_name

    # ── Poll for completion ──────────────────────────────────────
    start = time.time()
    while time.time() - start < MAX_WAIT:
        resp = api.get(f"/v1/jobs/{job_id}", headers=e2e_client_headers)
        assert resp.status_code == 200
        status = resp.json()["status"]
        if status in ("COMPLETED", "FAILED", "TIMEOUT"):
            break
        time.sleep(POLL_INTERVAL)
    else:
        pytest.fail(f"Job {job_id} did not complete within {MAX_WAIT}s")

    job = resp.json()
    output_text = job.get("output", {}).get("text", "")

    assert job["status"] == "COMPLETED", (
        f"Job ended with status {job['status']}. "
        f"Error: {job.get('error')}. "
        f"Output (first 1500 chars): {output_text[:1500]}"
    )

    # ── Verify the JSON file exists in output ────────────────────
    output_files = job.get("output", {}).get("files", {})
    assert JSON_FILENAME in output_files, (
        f"{JSON_FILENAME} not found in output files. "
        f"Available files: {sorted(output_files.keys())}. "
        f"Output text (first 500 chars): {output_text[:500]}"
    )

    # ── Decode and validate JSON content ─────────────────────────
    raw = base64.b64decode(output_files[JSON_FILENAME])
    result = json.loads(raw)

    # Layer 1: Agent created the file with {"agent": "ok"}
    assert result.get("agent") == "ok", (
        f"Layer 1 (Agent) failed — expected 'agent': 'ok'. Full result: {result}"
    )

    # Layer 2: Skill updated with {"skill": "ok"}
    assert result.get("skill") == "ok", (
        f"Layer 2 (Skill) failed — expected 'skill': 'ok'. Full result: {result}"
    )

    # Layer 3: Bundled script updated with {"script": "ok"}
    assert result.get("script") == "ok", (
        f"Layer 3 (Script) failed — expected 'script': 'ok'. Full result: {result}"
    )
