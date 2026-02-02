# Client Management

[← Back to README](../README.md)

## Table of Contents

- [Client Roles](#client-roles)
- [First Admin Setup](#first-admin-setup)
  - [Option A: CLI Script](#option-a-cli-script)
  - [Option B: Bootstrap (Production)](#option-b-bootstrap-production)
- [Managing Clients via Admin API](#managing-clients-via-admin-api)
  - [Create a Client](#create-a-client)
  - [List Clients](#list-clients)
  - [Get Client Details](#get-client-details)
  - [Update a Client](#update-a-client)
  - [Deactivate / Activate Client](#deactivate--activate-client)
  - [Delete a Client](#delete-a-client)

---

## Client Roles

The server supports two client roles:

| Role | Description | Access |
|------|-------------|--------|
| `client` | Standard API client | Upload archives, create/view jobs |
| `admin` | Administrator | All client access + manage clients, security profiles, MCP servers, agents, skills |

All clients default to the `client` role and the `common` security profile. At least one admin is required to use the Admin API endpoints.

Each client is bound to a **security profile** that controls tool restrictions, MCP server access, and network policy for their jobs. See [Security — Security Profiles](security-model.md#security-profiles) for details.

---

## First Admin Setup

Before you can use the Admin API, you need to create an admin user. Choose the method that fits your deployment:

### Option A: CLI Script

If you have direct access to the server (local development, SSH access, docker exec):

```bash
python create_admin.py my-admin
```

Or with Docker:
```bash
docker exec <container_name> python create_admin.py my-admin
```

**Output:**
```
============================================================
Admin client created successfully!
============================================================

  Client ID:   my-admin
  API Key:     ccas_Abc123...XYZ789

============================================================
IMPORTANT: Save this API key securely!
It cannot be retrieved later.
============================================================
```

Save the API key immediately - it's shown only once.

---

### Option B: Bootstrap (Production)

For containerized or headless deployments where you can't run CLI commands interactively, use the bootstrap feature. The server will auto-create an admin on first startup and log the API key encrypted with your RSA public key.

**Step 1: Generate RSA Key Pair**

On your local machine (not the server):

```bash
# Generate 2048-bit RSA private key
openssl genrsa -out admin_private.pem 2048

# Extract the public key
openssl rsa -in admin_private.pem -pubout -out admin_public.pem

# Keep admin_private.pem SECRET - store it securely offline
```

**Step 2: Base64-Encode the Public Key**

```bash
# Linux/macOS
base64 -w0 < admin_public.pem

# macOS alternative (if -w0 not supported)
base64 -i admin_public.pem | tr -d '\n'
```

Copy the output (a long base64 string).

**Step 3: Configure Environment Variables**

Set these before starting the server:

```bash
# Enable auto-admin creation
export CCAS_GENERATE_ADMIN_ON_FIRST_STARTUP=true

# Provide your public key (paste the base64 output from Step 2)
export CCAS_ADMIN_TOKEN_ENCRYPTION_KEY="LS0tLS1CRUdJTi..."
```

Or in `docker-compose.yml`:
```yaml
environment:
  CCAS_GENERATE_ADMIN_ON_FIRST_STARTUP: "true"
  CCAS_ADMIN_TOKEN_ENCRYPTION_KEY: "LS0tLS1CRUdJTi..."
```

**Step 4: Start the Server**

```bash
docker-compose up -d
```

**Step 5: Get the Encrypted Token from Logs**

```bash
docker logs <container_name> 2>&1 | grep "admin_bootstrap_complete"
```

Look for the `encrypted_api_key` field in the JSON log:
```json
{"event": "admin_bootstrap_complete", "client_id": "auto-admin", "encrypted_api_key": "cwRtNGp5...base64...==", ...}
```

Copy the full `encrypted_api_key` value.

**Step 6: Decrypt the Token**

On your local machine (where you have `admin_private.pem`):

```bash
echo "PASTE_ENCRYPTED_API_KEY_HERE" | base64 -d | \
  openssl pkeyutl -decrypt -inkey admin_private.pem \
    -pkeyopt rsa_padding_mode:oaep \
    -pkeyopt rsa_oaep_md:sha256 \
    -pkeyopt rsa_mgf1_md:sha256
```

This outputs the plaintext API key (e.g., `ccas_abc123...`). Save it securely.

**Security Notes:**
- The plaintext API key is never logged or stored in environment variables
- Only the encrypted form appears in logs
- Without your private key, the encrypted token is useless
- The bootstrap only runs once - if an admin already exists, it's skipped

---

## Managing Clients via Admin API

All client management is done via HTTP endpoints. See [API Reference — Admin API](api-reference.md#admin-api) for full details.

### Create a Client

```bash
curl -X POST http://localhost:8000/v1/admin/clients \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"client_id": "new-client", "description": "My new client", "role": "client", "security_profile": "common"}'
```

### List Clients

```bash
curl http://localhost:8000/v1/admin/clients \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### Get Client Details

```bash
curl http://localhost:8000/v1/admin/clients/my-client \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### Update a Client

```bash
curl -X PATCH http://localhost:8000/v1/admin/clients/my-client \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"description": "Updated description", "role": "admin", "security_profile": "restrictive"}'
```

### Deactivate / Activate Client

```bash
# Deactivate (soft delete - preserves data, blocks authentication)
curl -X POST http://localhost:8000/v1/admin/clients/my-client/deactivate \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# Reactivate
curl -X POST http://localhost:8000/v1/admin/clients/my-client/activate \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### Delete a Client

```bash
curl -X DELETE http://localhost:8000/v1/admin/clients/my-client \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```
