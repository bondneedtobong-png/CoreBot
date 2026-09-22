"""Task 07: multi-VPS fleet automation (mock/local validation, no real VPS).

Covers the 8 DoD points without Ansible installed and without real hosts:
inventory parsing (2+ instances, distinct env/persistent dirs, no secret keys,
no real IPs), .env template rendering (flat KEY=value, per-host secrets
differ, production validator passes), vault schema without plaintext secrets,
canary/serial logic (mock failure blocks the queue), no_log on secret tasks,
idempotency markers, secret-scan over ops/ansible, ansible.cfg safety.

Control node per ADR 0001 is WSL/Linux with Ansible; here (Windows, no
ansible/ansible-playbook module, no ansible in WSL) YAML is validated with
PyYAML and rollout logic is simulated — see FLEET_OPERATIONS.md §1.
"""

from __future__ import annotations

import configparser
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jinja2 import Template

REPO_ROOT = Path(__file__).resolve().parent.parent
FLEET = REPO_ROOT / "ops" / "ansible"
INVENTORY_EXAMPLE = FLEET / "inventory" / "hosts.yml.example"
VAULT_EXAMPLE = FLEET / "group_vars" / "all" / "vault.yml.example"
DOTENV_TEMPLATE = FLEET / "roles" / "config" / "templates" / "dotenv.j2"
ANSIBLE_CFG = FLEET / "ansible.cfg"
REPORT_TEMPLATE = FLEET / "templates" / "fleet_report.j2"
PLAYBOOKS = {name: FLEET / "playbooks" / f"{name}.yml" for name in ("install", "update", "verify", "backup", "rollback")}

SECRET_KEYS = ("bot_token", "api_hash", "api_id", "jwt_secret", "admin_password", "agent_token", "openrouter")
REAL_TOKEN_RE = re.compile(r"\b\d{6,15}:[A-Za-z0-9_-]{20,}\b")
REAL_IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b")
HEX64_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")


def load_yaml(path: Path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def fleet_hosts() -> dict:
    data = load_yaml(INVENTORY_EXAMPLE)
    return data["all"]["children"]["corebot_fleet"]["hosts"]


def all_yml_files() -> list[Path]:
    return sorted(FLEET.rglob("*.yml")) + sorted(FLEET.rglob("*.yaml"))


# --- inventory: 2+ instances, non-secret identifiers only ---------------------


def test_all_fleet_yaml_parses():
    assert all_yml_files(), "no yaml files under ops/ansible"
    for path in all_yml_files() + [p for p in PLAYBOOKS.values()]:
        assert load_yaml(path) is not None, f"unparsable YAML: {path}"


def test_inventory_two_instances_without_playbook_copy():
    hosts = fleet_hosts()
    assert len(hosts) >= 2, "need at least two test instances"
    allowed = {
        "ansible_host", "ansible_user", "ansible_port", "instance_name",
        "corebot_target_tag", "corebot_target_sha", "parser_embedded",
        "cp_enabled", "owner_id", "corebot_app_dir", "corebot_database_url",
        "corebot_bot_database_url", "corebot_cp_database_url",
    }
    for name, vars_ in hosts.items():
        for key in ("instance_name", "ansible_host", "ansible_user", "ansible_port",
                    "parser_embedded", "cp_enabled"):
            assert key in vars_, f"{name}: missing non-secret identifier {key}"
        assert ("corebot_target_tag" in vars_) or ("corebot_target_sha" in vars_), \
            f"{name}: missing desired version pin"
        for key in vars_:
            assert key in allowed, f"{name}: non-inventory key {key} (no playbook copy, no secrets)"
            assert not any(s in key.lower() for s in SECRET_KEYS), f"{name}: secret key in inventory: {key}"
        # No embedded playbook logic in inventory.
        assert "ansible.builtin" not in yaml.safe_dump(vars_)
        # Placeholder domains only, no real IPs.
        assert str(vars_["ansible_host"]).endswith(".invalid"), f"{name}: host must be .invalid placeholder"
    dumped = INVENTORY_EXAMPLE.read_text(encoding="utf-8")
    assert REAL_IPV4_RE.search(dumped) is None, "real IPv4 in inventory example"
    assert REAL_TOKEN_RE.search(dumped) is None, "secret-like token in inventory example"


def test_instances_get_distinct_env_and_persistent_dirs():
    hosts = fleet_hosts()
    owners = {v["owner_id"] for v in hosts.values()}
    assert len(owners) == len(hosts), "OWNER_ID must differ per instance"
    for key in ("corebot_database_url", "corebot_bot_database_url", "corebot_cp_database_url"):
        values = {str(v[key]) for v in hosts.values()}
        assert len(values) == len(hosts), f"{key} must differ per instance"
        for value in values:
            assert value.startswith("sqlite"), f"{key} must be sqlite URL: {value}"
            assert "////" in value, f"{key} must use absolute path: {value}"


# --- .env template: flat KEY=value, per-host secrets differ ------------------


def _synthetic_env(suffix: str) -> dict:
    return {
        "vault_api_id": "1000001",
        "vault_api_hash": "a" * 32,
        "instance_bot_token": f"100000{suffix}:TEST-SYNTHETIC-{suffix}-TOKEN-VALUE",
        "owner_id": f"100000{suffix}",
        "corebot_database_url": f"sqlite+aiosqlite:////opt/corebot/app/data/corebot-{suffix}.db",
        "corebot_bot_database_url": f"sqlite:////opt/corebot/app/data/corebot-{suffix}.db",
        "corebot_cp_database_url": f"sqlite:////opt/corebot/app/data/control-plane-{suffix}.db",
        "corebot_log_level": "INFO",
        "parser_embedded": "1",
        "corebot_parser_poll_sec": "2",
        "corebot_cp_agent_enabled": "0",
        "corebot_cp_agent_name": "corebot-agent",
        "instance_cp_agent_token": "",
        "instance_cp_jwt_secret": "TEST-JWT-" + suffix * 8 + "-0123456789abcdef",
        "corebot_cp_admin_username": "admin",
        "instance_cp_admin_password": f"TEST-Admin-Pass-{suffix}-xyz!",
        "instance_cp_telegram_bot_token": "",
        "corebot_cp_telegram_chat_id": "",
        "instance_openrouter_api_key": "",
    }


def _render_dotenv(env: dict) -> str:
    return Template(DOTENV_TEMPLATE.read_text(encoding="utf-8")).render(**env)


def test_dotenv_template_flat_no_expansion_per_host_differs():
    raw = DOTENV_TEMPLATE.read_text(encoding="utf-8")
    assert "${" not in raw and "$(" not in raw, "template must not use shell expansion"
    out_a = _render_dotenv(_synthetic_env("001"))
    out_b = _render_dotenv(_synthetic_env("002"))
    for out in (out_a, out_b):
        for line in out.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert re.match(r"^[A-Z0-9_]+=", stripped), f"non-flat line: {line!r}"
            assert not stripped.lower().startswith("export "), "no export prefix allowed"
        assert "${" not in out, "rendered .env must not contain expansion"
    assert out_a != out_b, "per-host renders must differ"
    assert "TEST-SYNTHETIC-001-TOKEN" in out_a and "TEST-SYNTHETIC-002-TOKEN" in out_b
    assert "\nOWNER_ID=100000001\n" in out_a and "\nOWNER_ID=100000002\n" in out_b


def test_rendered_dotenv_passes_production_validator(tmp_path):
    from tools.validate_config import load_env_file, validate

    env = _synthetic_env("007")
    env["instance_cp_jwt_secret"] = "f" * 64
    env["instance_cp_admin_password"] = "StrongPass-007-xyz"
    rendered = _render_dotenv(env)
    env_file = tmp_path / ".env"
    env_file.write_text(rendered, encoding="utf-8")
    snapshot = load_env_file(str(env_file))
    result = validate("production", snapshot)
    assert result.ok, f"fleet-rendered .env must pass production validation: {result.errors}"


# --- vault schema: names only, VAULT: placeholders ---------------------------


def _walk_scalars(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk_scalars(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_scalars(value)
    elif isinstance(node, str):
        yield node


def test_vault_example_schema_no_plaintext_secrets():
    data = load_yaml(VAULT_EXAMPLE)
    assert "vault_api_id" in data and "vault_api_hash" in data
    assert isinstance(data.get("vault_instances"), dict) and len(data["vault_instances"]) >= 2
    for instance, secrets in data["vault_instances"].items():
        for key in ("bot_token", "cp_jwt_secret", "cp_bootstrap_admin_password"):
            assert key in secrets, f"{instance}: missing vault key {key}"
    for value in _walk_scalars(data):
        if not value.strip():
            continue
        assert value.startswith("VAULT:"), f"vault example value must be VAULT: placeholder, got {value!r}"
        assert REAL_TOKEN_RE.search(value) is None, f"secret-like token in vault example: {value!r}"
        assert HEX64_RE.search(value) is None, f"real-looking hex secret in vault example: {value!r}"


# --- canary/serial: mock failure blocks the queue ----------------------------


def _play_hosts(play: dict) -> list:
    hosts = play.get("hosts")
    return [hosts] if isinstance(hosts, str) else list(hosts or [])


def simulate_canary_rollout(hosts: list[str], failed: set[str], serial: list) -> list[str]:
    """Mirror update.yml semantics: serial batches in order; any failure in a
    batch (max_fail_percentage=0 / any_errors_fatal) stops the whole queue."""
    batches: list[list[str]] = []
    rest = list(hosts)
    for entry in serial:
        if not rest:
            break
        if isinstance(entry, int):
            batches.append(rest[:entry])
            rest = rest[entry:]
        elif isinstance(entry, str) and entry.endswith("%"):
            n = max(1, int(round(len(rest) * int(entry[:-1]) / 100)))
            batches.append(rest[:n])
            rest = rest[n:]
        else:
            batches.append(rest)
            rest = []
    if rest:
        batches[-1] = batches[-1] + rest
    executed: list[str] = []
    for batch in batches:
        executed.extend(batch)
        if any(h in failed for h in batch):
            break
    return executed


def test_update_canary_blocks_rest_on_failure():
    play = load_yaml(PLAYBOOKS["update"])[0]
    assert _play_hosts(play) == ["corebot_fleet"]
    assert play.get("serial") == [1, "50%"], "canary first, then batches"
    assert play.get("max_fail_percentage") == 0
    assert play.get("any_errors_fatal") is True
    hosts = ["corebot-test-alpha", "corebot-test-beta", "corebot-test-gamma"]
    assert simulate_canary_rollout(hosts, {"corebot-test-alpha"}, play["serial"]) == ["corebot-test-alpha"]
    assert simulate_canary_rollout(hosts, set(), play["serial"]) == hosts
    # Mid-queue stop with one-by-one serial: failure in batch 2 blocks batch 3.
    assert simulate_canary_rollout(hosts, {"corebot-test-beta"}, [1, 1, 1]) == hosts[:2]


# --- no_log on secret tasks / report without secrets -------------------------


def _task_chunks(path: Path) -> list[str]:
    return re.split(r"(?m)^\s*-\s*name:", path.read_text(encoding="utf-8"))


def test_no_log_on_secret_tasks():
    secret_ref = re.compile(r"vault_|instance_bot_token|instance_cp_jwt|instance_cp_admin|bot_token|jwt_secret|admin_password|agent_token", re.IGNORECASE)
    checked = 0
    for path in list((FLEET / "playbooks").glob("*.yml")) + list((FLEET / "roles").rglob("tasks/*.yml")):
        for chunk in _task_chunks(path):
            if secret_ref.search(chunk):
                checked += 1
                assert re.search(r"no_log\s*:\s*true", chunk, re.IGNORECASE), \
                    f"secret-touching task without no_log in {path}: {chunk[:120]!r}"
    assert checked >= 3, "expected secret-touching tasks to check"


def test_report_template_has_no_secrets_and_covers_contract():
    raw = REPORT_TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"bot_token|jwt_secret|admin_password|api_hash|agent_token", raw, re.IGNORECASE), \
        "report template must never reference secrets"
    for field in ("version_before", "version_after", "backup_archive", "readiness", "errors"):
        assert field in raw, f"report missing contract field: {field}"


# --- idempotency markers + check/dry-run -------------------------------------


def test_idempotency_markers_and_check_mode_support():
    tasks_text = "\n".join((p.read_text(encoding="utf-8") for p in (FLEET / "roles").rglob("tasks/*.yml")))
    assert "creates:" in tasks_text, "need creates: guards (e.g. venv)"
    assert "changed_when:" in tasks_text, "need changed_when markers"
    assert "check_mode:" in tasks_text, "need check_mode awareness"
    assert "ansible.builtin.user" in tasks_text and "ansible.builtin.file" in tasks_text
    assert "ansible.builtin.git" in tasks_text, "deploy must converge via git module"
    assert "changed_when: false" in tasks_text, "read-only tasks must declare changed_when: false"


def test_all_five_playbooks_exist_with_limit_support():
    for name, path in PLAYBOOKS.items():
        assert path.exists(), f"missing playbook: {name}"
        plays = load_yaml(path)
        assert isinstance(plays, list) and plays, f"{name}: empty playbook"
        assert _play_hosts(plays[0]) == ["corebot_fleet"], f"{name}: must target corebot_fleet (use --limit for one host)"


# --- ansible.cfg safety + fleet-wide secret scan ------------------------------


def test_ansible_cfg_keeps_host_key_checking_and_local_vault():
    parser = configparser.ConfigParser()
    parser.read(ANSIBLE_CFG, encoding="utf-8")
    assert parser.getboolean("defaults", "host_key_checking") is True, "host key checking must stay ON"
    vault_id = parser.get("defaults", "vault_identity_list")
    assert "~" in vault_id or ".ansible" in vault_id, "vault password must live outside the repo"
    assert "ops/" not in vault_id and "group_vars" not in vault_id
    raw = ANSIBLE_CFG.read_text(encoding="utf-8")
    assert "host_key_checking = False" not in raw and "host_key_checking=False" not in raw


def test_secret_scan_no_secret_like_values_in_fleet():
    offenders: list[str] = []
    for path in FLEET.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if REAL_TOKEN_RE.search(text):
            offenders.append(f"{path}: real-like BOT_TOKEN pattern")
        if HEX64_RE.search(text):
            offenders.append(f"{path}: 64-hex secret pattern")
        for match in REAL_IPV4_RE.finditer(text):
            if not match.group(0).startswith("127."):
                offenders.append(f"{path}: non-loopback IPv4 {match.group(0)}")
    assert offenders == [], f"secret scan failed: {offenders}"


def test_vault_file_ignored_and_example_only_in_git():
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "ops/ansible/group_vars/all/vault.yml" in gitignore
    assert "ops/ansible/inventory/hosts.yml" in gitignore
    assert not (FLEET / "group_vars" / "all" / "vault.yml").exists(), "real vault file must not exist in repo"
    assert not (FLEET / "inventory" / "hosts.yml").exists(), "real inventory must not exist in repo"
