"""Argparse command hierarchy and command handlers for the CCAS manager."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..api import AdminApiClient, ApiError
from .config import Config, cmd_config_init, cmd_config_show, ensure_key, ensure_url
from ..formatters import Console
from .local import LocalScanner
from .models import SyncStatus
from .picker import pick_entries
from .sync import SyncEngine


# =============================================================================
# API client factory (lazy — only created when a command needs it)
# =============================================================================

def _make_api(config: Config, console: Console) -> AdminApiClient:
    url = ensure_url(config, console)
    key = ensure_key(config, console)
    return AdminApiClient(url, key)


def _make_local(config: Config) -> LocalScanner:
    return LocalScanner(config.local_claude_path)


# =============================================================================
# Status command
# =============================================================================

def cmd_status(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    console.step("Checking server health...")

    try:
        health = api.health()
    except ApiError:
        health = {"status": "unreachable"}

    try:
        status = api.admin_status()
    except ApiError as e:
        console.error(f"Cannot get admin status: {e.detail}")
        if config.json_mode:
            console.json_output({"health": health, "error": e.detail})
        return

    if config.json_mode:
        console.json_output({"health": health, "admin_status": status})
        return

    console.success(f"Server: {config.url}")
    data = {
        "status": health.get("status", "unknown"),
        "active_jobs": str(status.get("active_jobs", 0)),
        "pending_uploads": str(status.get("pending_uploads", 0)),
    }
    mcp = status.get("mcp_servers", {})
    if mcp:
        data["mcp_servers"] = ", ".join(f"{k}: {v}" for k, v in mcp.items())
    console.entity_detail(data)


# =============================================================================
# Skill commands
# =============================================================================

def cmd_skill_list(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    skills = api.list_skills()

    if config.json_mode:
        console.json_output(skills)
        return

    if not skills:
        console.info("No skills configured.")
        return

    headers = ["Name", "Files", "Size", "Added"]
    rows = [
        [
            s["name"],
            str(s.get("file_count", "")),
            Console.format_size(s.get("skill_size_bytes", 0)),
            s.get("added_at", "")[:10],
        ]
        for s in skills
    ]
    console.table(headers, rows)


def cmd_skill_show(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    skill = api.get_skill(args.name)

    if config.json_mode:
        console.json_output(skill)
        return

    data = {
        "name": skill["name"],
        "description": skill.get("description", ""),
        "size": Console.format_size(skill.get("skill_size_bytes", 0)),
        "file_count": str(skill.get("file_count", 0)),
        "added_at": skill.get("added_at", ""),
    }
    fl = skill.get("file_listing", [])
    if fl:
        data["files"] = ", ".join(fl)
    console.entity_detail(data)

    bp = skill.get("body_preview", "")
    if bp:
        console.detail("--- SKILL.md preview ---")
        for line in bp.split("\n")[:20]:
            console.detail(line)


def cmd_skill_add(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)

    if getattr(args, "sync_local", None):
        name = args.sync_local
        local = _make_local(config)
        console.step(f"Creating ZIP from local skill '{name}'...")
        try:
            zip_bytes = local.create_skill_zip(name)
        except FileNotFoundError as e:
            console.error(str(e))
            sys.exit(1)
    elif getattr(args, "from_file", None):
        # from_file can be a directory or a single file
        from pathlib import Path
        import io, zipfile
        source = Path(args.from_file)
        if not source.exists():
            console.error(f"Path not found: {source}")
            sys.exit(1)

        if source.is_dir():
            name = source.name
            files = sorted(f for f in source.rglob("*") if f.is_file()
                          and not any(p.startswith(".") for p in f.relative_to(source).parts))
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for fp in files:
                    zf.write(fp, f"{name}/{fp.relative_to(source)}")
            zip_bytes = buf.getvalue()
        else:
            console.error("--from-file must point to a skill directory")
            sys.exit(1)
    else:
        console.error("Use --sync-local <name> or --from-file <path>")
        sys.exit(1)

    if config.dry_run:
        console.info(f"[dry-run] Would add skill '{name}' ({Console.format_size(len(zip_bytes))})")
        return

    if not config.auto_confirm:
        if not console.confirm(f"Add skill '{name}' to remote?"):
            return

    console.step(f"Uploading skill '{name}'...")
    result = api.add_skill_zip(zip_bytes, name)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"Skill '{result['name']}' added")
    console.detail(f"file_count: {result.get('file_count', '')}")
    console.detail(f"size: {Console.format_size(result.get('skill_size_bytes', 0))}")


def cmd_skill_update(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    name = args.name

    if getattr(args, "sync_local", False):
        local = _make_local(config)
        console.step(f"Creating ZIP from local skill '{name}'...")
        try:
            zip_bytes = local.create_skill_zip(name)
        except FileNotFoundError as e:
            console.error(str(e))
            sys.exit(1)
    elif getattr(args, "from_file", None):
        from pathlib import Path
        import io, zipfile
        source = Path(args.from_file)
        if not source.exists() or not source.is_dir():
            console.error(f"Path must be an existing directory: {source}")
            sys.exit(1)
        files = sorted(f for f in source.rglob("*") if f.is_file()
                      and not any(p.startswith(".") for p in f.relative_to(source).parts))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in files:
                zf.write(fp, f"{name}/{fp.relative_to(source)}")
        zip_bytes = buf.getvalue()
    else:
        console.error("Use --sync-local or --from-file <path>")
        sys.exit(1)

    if config.dry_run:
        console.info(f"[dry-run] Would update skill '{name}'")
        return

    if not config.auto_confirm:
        if not console.confirm(f"Update skill '{name}' on remote?"):
            return

    console.step(f"Updating skill '{name}'...")
    result = api.update_skill_zip(name, zip_bytes)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"Skill '{name}' updated")


def cmd_skill_remove(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    name = args.name

    if config.dry_run:
        console.info(f"[dry-run] Would remove skill '{name}'")
        return

    if not config.auto_confirm:
        if not console.confirm(f"Remove skill '{name}' from remote?"):
            return

    api.remove_skill(name)
    console.success(f"Skill '{name}' removed")


# =============================================================================
# Agent commands
# =============================================================================

def cmd_agent_list(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    agents = api.list_agents()

    if config.json_mode:
        console.json_output(agents)
        return

    if not agents:
        console.info("No agents configured.")
        return

    headers = ["Name", "Size", "Added"]
    rows = [
        [
            a["name"],
            Console.format_size(a.get("prompt_size_bytes", 0)),
            a.get("added_at", "")[:10],
        ]
        for a in agents
    ]
    console.table(headers, rows)


def cmd_agent_show(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    agent = api.get_agent(args.name)

    if config.json_mode:
        console.json_output(agent)
        return

    data = {
        "name": agent["name"],
        "description": agent.get("description", ""),
        "size": Console.format_size(agent.get("prompt_size_bytes", 0)),
        "added_at": agent.get("added_at", ""),
    }
    fm = agent.get("frontmatter")
    if fm:
        data["tools"] = fm.get("tools", "")
    console.entity_detail(data)

    bp = agent.get("body_preview", "")
    if bp:
        console.detail("--- body preview ---")
        for line in bp.split("\n")[:20]:
            console.detail(line)


def cmd_agent_add(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)

    if getattr(args, "sync_local", None):
        name = args.sync_local
        local = _make_local(config)
        try:
            content = local.read_agent_content(name)
        except FileNotFoundError as e:
            console.error(str(e))
            sys.exit(1)
    elif getattr(args, "from_file", None):
        from pathlib import Path
        path = Path(args.from_file)
        if not path.is_file():
            console.error(f"File not found: {path}")
            sys.exit(1)
        content = path.read_text(errors="replace")
        name = path.stem
    else:
        console.error("Use --sync-local <name> or --from-file <path>")
        sys.exit(1)

    description = getattr(args, "description", "") or ""

    if config.dry_run:
        console.info(f"[dry-run] Would add agent '{name}' ({Console.format_size(len(content.encode()))})")
        return

    if not config.auto_confirm:
        if not console.confirm(f"Add agent '{name}' to remote?"):
            return

    console.step(f"Adding agent '{name}'...")
    result = api.add_agent(name, content, description)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"Agent '{result['name']}' added")
    console.detail(f"size: {Console.format_size(result.get('prompt_size_bytes', 0))}")


def cmd_agent_update(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    name = args.name

    content = None
    description = getattr(args, "description", None)

    if getattr(args, "sync_local", False):
        local = _make_local(config)
        try:
            content = local.read_agent_content(name)
        except FileNotFoundError as e:
            console.error(str(e))
            sys.exit(1)
    elif getattr(args, "from_file", None):
        from pathlib import Path
        path = Path(args.from_file)
        if not path.is_file():
            console.error(f"File not found: {path}")
            sys.exit(1)
        content = path.read_text(errors="replace")

    if content is None and description is None:
        console.error("Use --sync-local, --from-file <path>, or --description <d>")
        sys.exit(1)

    if config.dry_run:
        console.info(f"[dry-run] Would update agent '{name}'")
        return

    if not config.auto_confirm:
        if not console.confirm(f"Update agent '{name}' on remote?"):
            return

    console.step(f"Updating agent '{name}'...")
    result = api.update_agent(name, content=content, description=description)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"Agent '{name}' updated")


def cmd_agent_remove(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    name = args.name

    if config.dry_run:
        console.info(f"[dry-run] Would remove agent '{name}'")
        return

    if not config.auto_confirm:
        if not console.confirm(f"Remove agent '{name}' from remote?"):
            return

    api.remove_agent(name)
    console.success(f"Agent '{name}' removed")


# =============================================================================
# MCP commands
# =============================================================================

def cmd_mcp_list(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    servers = api.list_mcp()

    if config.json_mode:
        console.json_output(servers)
        return

    if not servers:
        console.info("No MCP servers configured.")
        return

    headers = ["Name", "Type", "Package", "Added"]
    rows = [
        [
            s["name"],
            s.get("type", ""),
            s.get("package", "") or "",
            s.get("added_at", "")[:10],
        ]
        for s in servers
    ]
    console.table(headers, rows)


def cmd_mcp_show(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    server = api.get_mcp(args.name)

    if config.json_mode:
        console.json_output(server)
        return

    console.entity_detail({
        "name": server["name"],
        "type": server.get("type", ""),
        "description": server.get("description", ""),
        "package_manager": server.get("package_manager", "") or "",
        "package": server.get("package", "") or "",
        "added_at": server.get("added_at", ""),
    })


def cmd_mcp_add(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)

    if getattr(args, "sync_local", None):
        name = args.sync_local
        local = _make_local(config)
        try:
            mcp_config = local.get_mcp_config(name)
        except FileNotFoundError as e:
            console.error(str(e))
            sys.exit(1)
        payload = SyncEngine._build_mcp_payload(name, mcp_config)
    else:
        name = args.name
        if not name:
            console.error("--name is required for manual MCP add")
            sys.exit(1)
        mcp_type = getattr(args, "type", "stdio")
        payload: dict[str, Any] = {"type": mcp_type}

        if mcp_type in ("http", "sse"):
            url = getattr(args, "mcp_url", None)
            if not url:
                console.error("--mcp-url is required for http/sse type")
                sys.exit(1)
            payload["url"] = url
            headers_raw = getattr(args, "headers", None) or []
            if headers_raw:
                payload["headers"] = dict(h.split("=", 1) for h in headers_raw)
        else:
            command = getattr(args, "command", None)
            if not command:
                console.error("--command is required for stdio type")
                sys.exit(1)
            payload["command"] = command
            mcp_args = getattr(args, "args", None)
            if mcp_args:
                payload["args"] = mcp_args
            env_raw = getattr(args, "env", None) or []
            if env_raw:
                payload["env"] = dict(e.split("=", 1) for e in env_raw)

        description = getattr(args, "description", "") or ""
        if description:
            payload["description"] = description

    if config.dry_run:
        console.info(f"[dry-run] Would add MCP server '{name}'")
        return

    if not config.auto_confirm:
        if not console.confirm(f"Add MCP server '{name}' to remote?"):
            return

    console.step(f"Adding MCP server '{name}'...")
    result = api.add_mcp(name, payload)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"MCP server '{result['name']}' added")


def cmd_mcp_install(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    package = args.package
    name = getattr(args, "name", None)
    description = getattr(args, "description", "") or ""
    pip = getattr(args, "pip", False)

    if config.dry_run:
        console.info(f"[dry-run] Would install MCP package '{package}'")
        return

    if not config.auto_confirm:
        if not console.confirm(f"Install MCP package '{package}' on remote?"):
            return

    console.step(f"Installing '{package}' (this may take a moment)...")
    result = api.install_mcp(package, name=name, description=description, pip=pip)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"MCP server '{result['name']}' installed")


def cmd_mcp_remove(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    name = args.name
    keep_package = getattr(args, "keep_package", False)

    if config.dry_run:
        console.info(f"[dry-run] Would remove MCP server '{name}'")
        return

    if not config.auto_confirm:
        if not console.confirm(f"Remove MCP server '{name}' from remote?"):
            return

    api.remove_mcp(name, keep_package=keep_package)
    console.success(f"MCP server '{name}' removed")


def cmd_mcp_health(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    name = getattr(args, "name", None)

    if name:
        console.step(f"Health-checking '{name}'...")
        result = api.health_check(name)
        if config.json_mode:
            console.json_output(result)
            return
        if result.get("healthy"):
            console.success(f"{name}: {result.get('detail', 'ok')}")
        else:
            console.error(f"{name}: {result.get('detail', 'unhealthy')}")
    else:
        console.step("Health-checking all MCP servers...")
        results = api.health_check_all()
        if config.json_mode:
            console.json_output(results)
            return
        if not results:
            console.info("No MCP servers configured.")
            return
        for r in results:
            if r.get("healthy"):
                console.success(f"{r['name']}: {r.get('detail', 'ok')}")
            else:
                console.error(f"{r['name']}: {r.get('detail', 'unhealthy')}")


# =============================================================================
# Client commands
# =============================================================================

def cmd_client_list(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    clients = api.list_clients()

    if config.json_mode:
        console.json_output(clients)
        return

    if not clients:
        console.info("No clients configured.")
        return

    headers = ["ID", "Role", "Profile", "Active", "Created"]
    rows = [
        [
            c["client_id"],
            c.get("role", ""),
            c.get("security_profile", ""),
            "yes" if c.get("active") else "no",
            c.get("created_at", "")[:10],
        ]
        for c in clients
    ]
    console.table(headers, rows)


def cmd_client_show(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    client = api.get_client(args.client_id)

    if config.json_mode:
        console.json_output(client)
        return

    console.entity_detail({
        "client_id": client["client_id"],
        "description": client.get("description", ""),
        "role": client.get("role", ""),
        "security_profile": client.get("security_profile", ""),
        "active": str(client.get("active", False)),
        "created_at": client.get("created_at", ""),
    })


def cmd_client_add(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    client_id = args.client_id
    role = getattr(args, "role", "client") or "client"
    description = getattr(args, "description", "") or ""
    profile = getattr(args, "profile", "common") or "common"

    if config.dry_run:
        console.info(f"[dry-run] Would create client '{client_id}'")
        return

    result = api.add_client(client_id, role=role, description=description,
                            security_profile=profile)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"Client '{client_id}' created")
    console.blank()
    console.warning(f"API Key: {result['api_key']}")
    console.detail("Save this key — it cannot be retrieved later.")
    console.blank()


def cmd_client_update(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    client_id = args.client_id

    kwargs: dict[str, Any] = {}
    if getattr(args, "role", None):
        kwargs["role"] = args.role
    if getattr(args, "description", None) is not None:
        kwargs["description"] = args.description
    if getattr(args, "profile", None):
        kwargs["security_profile"] = args.profile

    if not kwargs:
        console.error("Nothing to update. Use --role, --description, or --profile.")
        sys.exit(1)

    result = api.update_client(client_id, **kwargs)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"Client '{client_id}' updated")


def cmd_client_remove(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    client_id = args.client_id

    if config.dry_run:
        console.info(f"[dry-run] Would delete client '{client_id}'")
        return

    if not config.auto_confirm:
        if not console.confirm(f"Delete client '{client_id}'? This is permanent."):
            return

    api.remove_client(client_id)
    console.success(f"Client '{client_id}' deleted")


def cmd_client_activate(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    result = api.activate_client(args.client_id)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"Client '{args.client_id}' activated")


def cmd_client_deactivate(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)

    if not config.auto_confirm:
        if not console.confirm(f"Deactivate client '{args.client_id}'?"):
            return

    result = api.deactivate_client(args.client_id)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"Client '{args.client_id}' deactivated")


# =============================================================================
# Profile commands
# =============================================================================

def cmd_profile_list(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    profiles = api.list_profiles()

    if config.json_mode:
        console.json_output(profiles)
        return

    if not profiles:
        console.info("No security profiles configured.")
        return

    headers = ["Name", "Built-in", "Default", "Denied Tools"]
    rows = [
        [
            p["name"],
            "yes" if p.get("is_builtin") else "no",
            "yes" if p.get("is_default") else "no",
            ", ".join(p.get("denied_tools", []) or []),
        ]
        for p in profiles
    ]
    console.table(headers, rows)


def cmd_profile_show(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    profile = api.get_profile(args.name)

    if config.json_mode:
        console.json_output(profile)
        return

    console.entity_detail({
        "name": profile["name"],
        "description": profile.get("description", ""),
        "is_builtin": str(profile.get("is_builtin", False)),
        "is_default": str(profile.get("is_default", False)),
        "denied_tools": ", ".join(profile.get("denied_tools", []) or []) or "(none)",
        "allowed_mcp": str(profile.get("allowed_mcp_servers")) if profile.get("allowed_mcp_servers") is not None else "(all)",
        "created_at": profile.get("created_at", ""),
    })

    network = profile.get("network")
    if network and isinstance(network, dict):
        console.detail("--- network policy ---")
        for k, v in network.items():
            console.detail(f"  {k}: {v}")


def cmd_profile_add(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    name = args.name

    kwargs: dict[str, Any] = {}
    if getattr(args, "description", None):
        kwargs["description"] = args.description
    denied = getattr(args, "denied_tools", None)
    if denied:
        kwargs["denied_tools"] = [t.strip() for t in denied.split(",")]

    if config.dry_run:
        console.info(f"[dry-run] Would create profile '{name}'")
        return

    result = api.add_profile(name, **kwargs)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"Profile '{name}' created")


def cmd_profile_update(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    name = args.name

    kwargs: dict[str, Any] = {}
    if getattr(args, "description", None) is not None:
        kwargs["description"] = args.description
    denied = getattr(args, "denied_tools", None)
    if denied is not None:
        kwargs["denied_tools"] = [t.strip() for t in denied.split(",")] if denied else []

    if not kwargs:
        console.error("Nothing to update.")
        sys.exit(1)

    result = api.update_profile(name, **kwargs)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"Profile '{name}' updated")


def cmd_profile_remove(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    name = args.name

    if config.dry_run:
        console.info(f"[dry-run] Would delete profile '{name}'")
        return

    if not config.auto_confirm:
        if not console.confirm(f"Delete profile '{name}'?"):
            return

    api.remove_profile(name)
    console.success(f"Profile '{name}' deleted")


def cmd_profile_set_default(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    name = args.name

    result = api.set_default_profile(name)

    if config.json_mode:
        console.json_output(result)
        return

    console.success(f"Profile '{name}' is now the default")


# =============================================================================
# Sync commands
# =============================================================================

def cmd_sync_status(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    local = _make_local(config)

    if not local.exists:
        console.error(f"Local Claude Code directory not found: {local.home}")
        sys.exit(1)

    entity_types = None
    type_filter = getattr(args, "type", None)
    if type_filter:
        entity_types = [type_filter]

    console.step("Scanning local and remote...")
    engine = SyncEngine(api, local)
    entries = engine.compute_status(entity_types)

    if config.json_mode:
        console.json_output([
            {
                "entity_type": e.entity_type,
                "name": e.name,
                "status": e.status.value,
                "detail": e.detail,
            }
            for e in entries
        ])
        return

    if not entries:
        console.info("No entities found.")
        return

    console.sync_table(entries, url=config.url, local_path=str(local.home))


def cmd_sync_push(args, config: Config, console: Console) -> None:
    api = _make_api(config, console)
    local = _make_local(config)

    if not local.exists:
        console.error(f"Local Claude Code directory not found: {local.home}")
        sys.exit(1)

    engine = SyncEngine(api, local)

    push_all = getattr(args, "all", False)
    pick_mode = getattr(args, "pick", False)
    entity_type = getattr(args, "type", None)
    entity_name = getattr(args, "name", None)
    local_only = getattr(args, "local_only", False)
    include_diverged = getattr(args, "include_diverged", False)

    if entity_name and entity_type:
        # Push single entity
        entries = engine.compute_status([entity_type])
        target = None
        for e in entries:
            if e.name == entity_name:
                target = e
                break

        if target is None:
            console.error(f"Entity not found: {entity_type} '{entity_name}'")
            sys.exit(1)

        if target.status == SyncStatus.INCOMPATIBLE:
            console.error(
                f"Cannot push '{entity_name}': {target.detail or 'name incompatible'}. "
                "Rename the entity locally."
            )
            sys.exit(1)

        if target.status == SyncStatus.REMOTE_ONLY:
            console.error(f"'{entity_name}' only exists on remote, nothing to push.")
            sys.exit(1)

        if config.dry_run:
            console.info(f"[dry-run] Would push {entity_type} '{entity_name}'")
            return

        if not config.auto_confirm:
            action = "update" if target.status in (SyncStatus.SYNCED, SyncStatus.DIVERGED) else "create"
            if not console.confirm(f"{action.capitalize()} {entity_type} '{entity_name}' on remote?"):
                return

        if entity_type == "agent":
            result = engine.push_agent(entity_name)
        elif entity_type == "skill":
            result = engine.push_skill(entity_name)
        else:
            console.error(f"Unknown type: {entity_type}")
            sys.exit(1)

        if result.success:
            console.success(f"{entity_type} '{entity_name}': {result.message}")
        else:
            console.error(f"{entity_type} '{entity_name}': {result.message}")
            sys.exit(1)

    elif push_all:
        # Push all
        types = [entity_type] if entity_type else None
        console.step("Computing sync status...")
        entries = engine.compute_status(types)

        # Filter pushable entries
        pushable = []
        for e in entries:
            if e.status == SyncStatus.INCOMPATIBLE:
                continue
            if e.status == SyncStatus.REMOTE_ONLY:
                continue
            if e.status == SyncStatus.SYNCED:
                continue
            if local_only and e.status != SyncStatus.LOCAL_ONLY:
                continue
            if not include_diverged and e.status == SyncStatus.DIVERGED:
                continue
            if e.status in (SyncStatus.LOCAL_ONLY, SyncStatus.DIVERGED):
                pushable.append(e)

        if not pushable:
            console.info("Nothing to push.")
            return

        console.blank()
        console.info(f"Will push {len(pushable)} entities:")
        for e in pushable:
            console.detail(f"  {e.entity_type} '{e.name}' ({e.status.value})")
        console.blank()

        if config.dry_run:
            console.info("[dry-run] No changes made.")
            return

        if not config.auto_confirm:
            if not console.confirm(f"Push {len(pushable)} entities to remote?"):
                return

        results = engine.push_batch(pushable, console, config.auto_confirm)

        # Summary
        success_count = sum(1 for r in results if r.success)
        fail_count = len(results) - success_count
        console.blank()
        if fail_count == 0:
            console.success(f"{success_count}/{len(results)} pushed successfully")
        else:
            console.warning(f"{success_count}/{len(results)} pushed, {fail_count} failed")
    elif pick_mode or (not entity_name and not push_all and sys.stdin.isatty()):
        # Interactive picker mode
        types = [entity_type] if entity_type else None
        console.step("Computing sync status...")
        entries = engine.compute_status(types)

        picked = pick_entries(entries)
        if picked is None:
            console.info("Cancelled.")
            return
        if not picked:
            console.info("Nothing to push (no pushable entities).")
            return

        console.blank()
        console.info(f"Will push {len(picked)} entities:")
        for e in picked:
            console.detail(f"  {e.entity_type} '{e.name}' ({e.status.value})")
        console.blank()

        if config.dry_run:
            console.info("[dry-run] No changes made.")
            return

        if not config.auto_confirm:
            if not console.confirm(f"Push {len(picked)} entities to remote?"):
                return

        results = engine.push_batch(picked, console, config.auto_confirm)

        success_count = sum(1 for r in results if r.success)
        fail_count = len(results) - success_count
        console.blank()
        if fail_count == 0:
            console.success(f"{success_count}/{len(results)} pushed successfully")
        else:
            console.warning(f"{success_count}/{len(results)} pushed, {fail_count} failed")
    else:
        console.error(
            "Specify what to push:\n"
            "  ccas sync push <type> <name>    Push one entity\n"
            "  ccas sync push <type> --all     Push all of a type\n"
            "  ccas sync push --all            Push everything\n"
            "  ccas sync push --pick           Interactive picker"
        )
        sys.exit(1)


# =============================================================================
# Parser builder
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccas",
        description="Claude Code API Server — admin CLI manager",
    )

    # Global flags
    parser.add_argument("--url", default=None,
                        help="CCAS server URL (env: CCAS_URL). Required.")
    parser.add_argument("--key", default=None,
                        help="Admin API key (env: CCAS_ADMIN_API_KEY)")
    parser.add_argument("--json", action="store_true", default=False,
                       help="Machine-readable JSON output")
    parser.add_argument("--no-color", action="store_true", default=False,
                       help="Disable colored output")
    parser.add_argument("--verbose", "-v", action="store_true", default=False,
                       help="Verbose output")
    parser.add_argument("--dry-run", action="store_true", default=False,
                       help="Preview changes without applying")
    parser.add_argument("--yes", "-y", action="store_true", default=False,
                       help="Skip confirmation prompts")

    sub = parser.add_subparsers(dest="command")

    # --- status ---
    p_status = sub.add_parser("status", help="Server health and admin status")
    p_status.set_defaults(func=cmd_status)

    # --- config ---
    p_config = sub.add_parser("config", help="Configuration management")
    config_sub = p_config.add_subparsers(dest="config_command")

    p_config_init = config_sub.add_parser("init", help="Interactive config setup")
    p_config_init.set_defaults(func=cmd_config_init)

    p_config_show = config_sub.add_parser("show", help="Show effective config")
    p_config_show.set_defaults(func=cmd_config_show)

    # --- skill ---
    p_skill = sub.add_parser("skill", help="Skill management")
    skill_sub = p_skill.add_subparsers(dest="skill_command")

    p = skill_sub.add_parser("list", help="List all remote skills")
    p.set_defaults(func=cmd_skill_list)

    p = skill_sub.add_parser("show", help="Show skill details")
    p.add_argument("name", help="Skill name")
    p.set_defaults(func=cmd_skill_show)

    p = skill_sub.add_parser("add", help="Add a skill")
    p.add_argument("--sync-local", metavar="NAME", help="Push from ~/.claude/skills/<name>/")
    p.add_argument("--from-file", metavar="PATH", help="Add from local path (directory)")
    p.set_defaults(func=cmd_skill_add)

    p = skill_sub.add_parser("update", help="Update a skill")
    p.add_argument("name", help="Skill name")
    p.add_argument("--sync-local", action="store_true", help="Update from local version")
    p.add_argument("--from-file", metavar="PATH", help="Update from path")
    p.set_defaults(func=cmd_skill_update)

    p = skill_sub.add_parser("remove", help="Remove a skill")
    p.add_argument("name", help="Skill name")
    p.set_defaults(func=cmd_skill_remove)

    # --- agent ---
    p_agent = sub.add_parser("agent", help="Agent management")
    agent_sub = p_agent.add_subparsers(dest="agent_command")

    p = agent_sub.add_parser("list", help="List all remote agents")
    p.set_defaults(func=cmd_agent_list)

    p = agent_sub.add_parser("show", help="Show agent details")
    p.add_argument("name", help="Agent name")
    p.set_defaults(func=cmd_agent_show)

    p = agent_sub.add_parser("add", help="Add an agent")
    p.add_argument("--sync-local", metavar="NAME", help="Push from ~/.claude/agents/<name>.md")
    p.add_argument("--from-file", metavar="PATH", help="Add from .md file")
    p.add_argument("--description", help="Agent description")
    p.set_defaults(func=cmd_agent_add)

    p = agent_sub.add_parser("update", help="Update an agent")
    p.add_argument("name", help="Agent name")
    p.add_argument("--sync-local", action="store_true", help="Update from local version")
    p.add_argument("--from-file", metavar="PATH", help="Update from file")
    p.add_argument("--description", help="Update description")
    p.set_defaults(func=cmd_agent_update)

    p = agent_sub.add_parser("remove", help="Remove an agent")
    p.add_argument("name", help="Agent name")
    p.set_defaults(func=cmd_agent_remove)

    # --- mcp ---
    p_mcp = sub.add_parser("mcp", help="MCP server management")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_command")

    p = mcp_sub.add_parser("list", help="List all remote MCP servers")
    p.set_defaults(func=cmd_mcp_list)

    p = mcp_sub.add_parser("show", help="Show MCP server details")
    p.add_argument("name", help="MCP server name")
    p.set_defaults(func=cmd_mcp_show)

    p = mcp_sub.add_parser("add", help="Add an MCP server")
    p.add_argument("--sync-local", metavar="NAME", help="Push from settings.json")
    p.add_argument("--name", help="Server name (for manual add)")
    p.add_argument("--type", choices=["stdio", "http", "sse"], default="stdio",
                  help="Server type")
    p.add_argument("--command", help="Command (stdio)")
    p.add_argument("--args", nargs="*", help="Command args (stdio)")
    p.add_argument("--env", nargs="*", metavar="KEY=VAL", help="Env vars (stdio)")
    p.add_argument("--mcp-url", help="Server URL (http/sse)")
    p.add_argument("--headers", nargs="*", metavar="KEY=VAL", help="Headers (http/sse)")
    p.add_argument("--description", help="Description")
    p.set_defaults(func=cmd_mcp_add)

    p = mcp_sub.add_parser("install", help="Install MCP from npm/pip")
    p.add_argument("package", help="Package name")
    p.add_argument("--pip", action="store_true", help="Use pip instead of npm")
    p.add_argument("--name", help="Override server name")
    p.add_argument("--description", help="Description")
    p.set_defaults(func=cmd_mcp_install)

    p = mcp_sub.add_parser("remove", help="Remove MCP server")
    p.add_argument("name", help="Server name")
    p.add_argument("--keep-package", action="store_true", help="Keep installed package")
    p.set_defaults(func=cmd_mcp_remove)

    p = mcp_sub.add_parser("health", help="Health-check MCP servers")
    p.add_argument("name", nargs="?", default=None, help="Specific server (or all)")
    p.set_defaults(func=cmd_mcp_health)

    # --- client ---
    p_client = sub.add_parser("client", help="Client management")
    client_sub = p_client.add_subparsers(dest="client_command")

    p = client_sub.add_parser("list", help="List all clients")
    p.set_defaults(func=cmd_client_list)

    p = client_sub.add_parser("show", help="Show client details")
    p.add_argument("client_id", help="Client ID")
    p.set_defaults(func=cmd_client_show)

    p = client_sub.add_parser("add", help="Create a client")
    p.add_argument("client_id", help="Client ID")
    p.add_argument("--role", choices=["admin", "client"], default="client")
    p.add_argument("--description", help="Description")
    p.add_argument("--profile", default="common", help="Security profile")
    p.set_defaults(func=cmd_client_add)

    p = client_sub.add_parser("update", help="Update a client")
    p.add_argument("client_id", help="Client ID")
    p.add_argument("--role", choices=["admin", "client"])
    p.add_argument("--description", help="Description")
    p.add_argument("--profile", help="Security profile")
    p.set_defaults(func=cmd_client_update)

    p = client_sub.add_parser("remove", help="Delete a client")
    p.add_argument("client_id", help="Client ID")
    p.set_defaults(func=cmd_client_remove)

    p = client_sub.add_parser("activate", help="Reactivate a client")
    p.add_argument("client_id", help="Client ID")
    p.set_defaults(func=cmd_client_activate)

    p = client_sub.add_parser("deactivate", help="Deactivate a client")
    p.add_argument("client_id", help="Client ID")
    p.set_defaults(func=cmd_client_deactivate)

    # --- profile ---
    p_profile = sub.add_parser("profile", help="Security profile management")
    profile_sub = p_profile.add_subparsers(dest="profile_command")

    p = profile_sub.add_parser("list", help="List security profiles")
    p.set_defaults(func=cmd_profile_list)

    p = profile_sub.add_parser("show", help="Show profile details")
    p.add_argument("name", help="Profile name")
    p.set_defaults(func=cmd_profile_show)

    p = profile_sub.add_parser("add", help="Create a profile")
    p.add_argument("name", help="Profile name")
    p.add_argument("--description", help="Description")
    p.add_argument("--denied-tools", help="Comma-separated denied tools")
    p.set_defaults(func=cmd_profile_add)

    p = profile_sub.add_parser("update", help="Update a profile")
    p.add_argument("name", help="Profile name")
    p.add_argument("--description", help="Description")
    p.add_argument("--denied-tools", help="Comma-separated denied tools")
    p.set_defaults(func=cmd_profile_update)

    p = profile_sub.add_parser("remove", help="Delete a profile")
    p.add_argument("name", help="Profile name")
    p.set_defaults(func=cmd_profile_remove)

    p = profile_sub.add_parser("set-default", help="Set default profile")
    p.add_argument("name", help="Profile name")
    p.set_defaults(func=cmd_profile_set_default)

    # --- sync ---
    p_sync = sub.add_parser("sync", help="Local-remote synchronization")
    sync_sub = p_sync.add_subparsers(dest="sync_command")

    p = sync_sub.add_parser("status", help="Show sync status table")
    p.add_argument("--type", choices=["skill", "agent"],
                  help="Filter by entity type")
    p.set_defaults(func=cmd_sync_status)

    p = sync_sub.add_parser("push", help="Push entities to remote")
    p.add_argument("type", nargs="?", choices=["skill", "agent"],
                  help="Entity type")
    p.add_argument("name", nargs="?", help="Entity name")
    p.add_argument("--all", action="store_true", help="Push all entities")
    p.add_argument("--local-only", action="store_true",
                  help="Only push LOCAL_ONLY entities")
    p.add_argument("--include-diverged", action="store_true",
                  help="Also push DIVERGED entities")
    p.add_argument("--pick", action="store_true",
                  help="Interactive entity picker")
    p.set_defaults(func=cmd_sync_push)

    return parser
