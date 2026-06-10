"""P3 config surface tests (mirror of plugins/minni/tests/providers-config.test.mjs).

Covers the ~/.minni/providers.json loader, secret resolution (apiKeyEnv /
0600 apiKeyFile under ~/.minni/secrets/), the G13 model target allowlist with
the MINNI_MODEL_ALLOWED_TARGETS alias + HTTPS-required for non-loopback, and
the negative-path proof that the cloud key never appears in error output.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

SECRET_KEY = "sk-test-vErYsEcReT-cLoUdKeY-12345"


# --- providers.json loader ------------------------------------------------------


def test_load_providers_config_defaults_when_missing():
    from config import load_providers_config

    config = load_providers_config("/tmp/definitely-missing-minni-providers.json")
    assert config["chain"] == ["afm"]
    assert config["operations"] == {"retrieval": {"localOnly": True}}
    assert config["providers"] == {}


def test_load_providers_config_parses_documented_shape(tmp_path):
    from config import load_providers_config

    file = tmp_path / "providers.json"
    file.write_text(
        json.dumps(
            {
                "chain": ["afm", "mlx", "ollama"],
                "operations": {"retrieval": {"localOnly": True}, "prepare": {"localOnly": False}},
                "providers": {
                    "mlx": {"baseUrl": "http://127.0.0.1:8080", "model": "mlx-community/some-model"},
                    "ollama": {"baseUrl": "http://127.0.0.1:11434", "model": "qwen3"},
                    "cloud": {
                        "enabled": False,
                        "vendor": "anthropic",
                        "model": "claude-haiku",
                        "apiKeyEnv": "MINNI_CLOUD_KEY",
                        "privacyMax": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_providers_config(os.fspath(file))
    assert config["chain"] == ["afm", "mlx", "ollama"]
    assert config["operations"]["retrieval"]["localOnly"] is True
    assert config["providers"]["cloud"]["vendor"] == "anthropic"


def test_load_providers_config_rejects_inline_api_key(tmp_path):
    from config import load_providers_config

    file = tmp_path / "providers.json"
    file.write_text(
        json.dumps({"chain": ["afm"], "providers": {"cloud": {"enabled": True, "apiKey": SECRET_KEY}}}),
        encoding="utf-8",
    )
    config = load_providers_config(os.fspath(file))
    assert config["providers"]["cloud"]["enabled"] is False
    assert "apiKey" not in config["providers"]["cloud"]
    assert SECRET_KEY not in json.dumps(config)


def test_load_providers_config_degrades_on_invalid_json(tmp_path):
    from config import load_providers_config

    file = tmp_path / "providers.json"
    file.write_text("{not json", encoding="utf-8")
    assert load_providers_config(os.fspath(file))["chain"] == ["afm"]


def test_default_provider_chain_respects_providers_json(tmp_path, monkeypatch):
    from model_provider import default_provider_chain

    file = tmp_path / "providers.json"
    file.write_text(
        json.dumps({"chain": ["afm", "mlx"], "operations": {"prepare": {"localOnly": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MINNI_PROVIDERS_CONFIG", os.fspath(file))
    chain = default_provider_chain()
    # mlx is parsed but not implemented until P4 — only afm is instantiated.
    assert [p.name for p in chain.providers] == ["afm"]
    assert chain.operations["prepare"].local_only is True
    assert chain.operations["retrieval"].local_only is True


# --- secret resolution ------------------------------------------------------------


def test_resolve_cloud_api_key_env(monkeypatch):
    from config import resolve_cloud_api_key

    monkeypatch.setenv("MINNI_TEST_CLOUD_KEY", SECRET_KEY)
    assert resolve_cloud_api_key({"enabled": True, "apiKeyEnv": "MINNI_TEST_CLOUD_KEY"}) == {"key": SECRET_KEY}

    monkeypatch.delenv("MINNI_TEST_CLOUD_KEY")
    result = resolve_cloud_api_key({"enabled": True, "apiKeyEnv": "MINNI_TEST_CLOUD_KEY"})
    assert "cloud_key_unavailable" in result["error"]
    assert SECRET_KEY not in json.dumps(result)


def test_resolve_cloud_api_key_file_0600(tmp_path):
    from config import resolve_cloud_api_key

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key_file = secrets / "cloud.key"
    key_file.write_text(SECRET_KEY + "\n", encoding="utf-8")
    key_file.chmod(0o600)
    result = resolve_cloud_api_key(
        {"enabled": True, "apiKeyFile": os.fspath(key_file)}, secrets_dir=os.fspath(secrets)
    )
    assert result == {"key": SECRET_KEY}


def test_resolve_cloud_api_key_denies_loose_permissions(tmp_path):
    from config import resolve_cloud_api_key

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key_file = secrets / "cloud.key"
    key_file.write_text(SECRET_KEY, encoding="utf-8")
    key_file.chmod(0o644)
    result = resolve_cloud_api_key(
        {"enabled": True, "apiKeyFile": os.fspath(key_file)}, secrets_dir=os.fspath(secrets)
    )
    assert "cloud_key_denied" in result["error"]
    assert "0600" in result["error"]
    assert SECRET_KEY not in json.dumps(result)


def test_resolve_cloud_api_key_denies_files_outside_secrets_dir(tmp_path):
    from config import resolve_cloud_api_key

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    outside = tmp_path / "cloud.key"
    outside.write_text(SECRET_KEY, encoding="utf-8")
    outside.chmod(0o600)
    result = resolve_cloud_api_key(
        {"enabled": True, "apiKeyFile": os.fspath(outside)}, secrets_dir=os.fspath(secrets)
    )
    assert "cloud_key_denied" in result["error"]
    assert "secrets" in result["error"]


def test_resolve_cloud_api_key_requires_a_source():
    from config import resolve_cloud_api_key

    assert "cloud_key_unavailable" in resolve_cloud_api_key({"enabled": True})["error"]
    assert resolve_cloud_api_key({"enabled": False, "apiKeyEnv": "X"}) == {}
    assert resolve_cloud_api_key(None) == {}


# --- G13 model target allowlist -----------------------------------------------------


def test_check_model_target_loopback_and_default_denial(monkeypatch):
    from config import check_model_target

    monkeypatch.delenv("MINNI_AFM_ALLOWED_TARGETS", raising=False)
    monkeypatch.delenv("MINNI_MODEL_ALLOWED_TARGETS", raising=False)

    assert check_model_target("http://127.0.0.1:11437/v1/chat/completions")["allowed"] is True
    assert check_model_target("http://localhost:11434/api")["allowed"] is True
    denied = check_model_target("https://api.example.com/v1")
    assert denied["allowed"] is False
    assert denied["reason"] == "not_allowlisted"


def test_check_model_target_alias_and_https_required(monkeypatch):
    from config import check_model_target

    monkeypatch.delenv("MINNI_AFM_ALLOWED_TARGETS", raising=False)
    monkeypatch.setenv("MINNI_MODEL_ALLOWED_TARGETS", "api.example.com")

    http_result = check_model_target("http://api.example.com/v1")
    assert http_result["allowed"] is False
    assert http_result["reason"] == "https_required"
    assert check_model_target("https://api.example.com/v1")["allowed"] is True


def test_gate_provider_chain_denies_non_allowlisted_cloud_host(monkeypatch):
    """GATE: non-allowlisted cloud host -> structured denial; key never leaks."""
    from model_provider import AfmProvider, ChatRequest

    monkeypatch.delenv("MINNI_AFM_ALLOWED_TARGETS", raising=False)
    monkeypatch.delenv("MINNI_MODEL_ALLOWED_TARGETS", raising=False)
    monkeypatch.setenv("MINNI_TEST_CLOUD_KEY", SECRET_KEY)

    def forbidden_client(*_args, **_kwargs):
        raise AssertionError("denied target must never reach a transport")

    result = AfmProvider().chat(
        ChatRequest(
            payload={"messages": [], "metadata": {"authorization": f"Bearer {SECRET_KEY}"}},
            operation="prepare",
            url="https://api.openai.com/v1/chat/completions",
            mode="bridge",
        ),
        client=forbidden_client,
    )
    assert result.ok is False
    assert result.status == "target_denied"
    assert result.error.startswith("afm_target_denied:")
    assert "api.openai.com" not in result.error
    assert SECRET_KEY not in repr(result)


def test_gate_provider_chain_requires_https_for_allowlisted_hosts(monkeypatch):
    from model_provider import AfmProvider, ChatRequest

    monkeypatch.setenv("MINNI_MODEL_ALLOWED_TARGETS", "api.example.com")

    result = AfmProvider().chat(
        ChatRequest(payload={"messages": []}, operation="prepare", url="http://api.example.com/v1", mode="bridge"),
        client=lambda *_a, **_k: {},
    )
    assert result.ok is False
    assert result.error == "afm_target_denied: non-loopback model targets require https"


# --- negative path: key never appears in sanitized error output ----------------------


def test_safe_status_error_strips_auth_material():
    from afm_provider import _safe_status_error

    leaked = (
        f"HTTP 401 authorization: Bearer {SECRET_KEY} "
        f"x-api-key={SECRET_KEY} api_key: {SECRET_KEY} plain {SECRET_KEY}"
    )
    sanitized = _safe_status_error(leaked)
    assert SECRET_KEY not in sanitized
    assert "[redacted" in sanitized


def test_generation_health_detail_never_contains_key(monkeypatch):
    from afm_provider import reset_afm_generation_probe_cache, verify_afm_generation

    reset_afm_generation_probe_cache()

    def leaky_client(payload, url, timeout):
        raise RuntimeError(f"upstream rejected authorization: Bearer {SECRET_KEY}")

    health = verify_afm_generation("bridge", client=leaky_client)
    reset_afm_generation_probe_cache()
    assert health["ok"] is False
    assert SECRET_KEY not in json.dumps(health)


def test_safe_status_error_redacts_json_quoted_header_keys():
    """GATE: serialized-header form (quoted key names) must also be redacted —
    a vendor-style opaque key matches neither the sk- nor the bearer backstop."""
    from afm_provider import _safe_status_error

    aws_style_key = "QUOTEDFAKEKEY1234567890"
    leaked = (
        'request to upstream failed: headers {"x-api-key":"%s",'
        '"api_key":"%s","access_token":"%s","authorization":"Basic %s"}'
    ) % (aws_style_key, aws_style_key, aws_style_key, aws_style_key)
    sanitized = _safe_status_error(leaked)
    assert aws_style_key not in sanitized
    assert "[redacted]" in sanitized


def test_resolve_cloud_api_key_denies_symlink_escape(tmp_path):
    """SEC: a symlink under secrets/ pointing at any 0600 file elsewhere must
    fail containment (realpath on both sides; mirror of config.ts)."""
    from config import resolve_cloud_api_key

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    outside = tmp_path / "exfil-target.key"
    outside.write_text(SECRET_KEY, encoding="utf-8")
    outside.chmod(0o600)
    link = secrets / "cloud.key"
    link.symlink_to(outside)

    result = resolve_cloud_api_key(
        {"enabled": True, "apiKeyFile": os.fspath(link)}, secrets_dir=os.fspath(secrets)
    )
    assert "key" not in result
    assert "cloud_key_denied" in result["error"]
    assert SECRET_KEY not in json.dumps(result)


def test_resolve_cloud_api_key_denies_non_regular_file(tmp_path):
    from config import resolve_cloud_api_key

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    dir_key = secrets / "cloud.key"
    dir_key.mkdir()
    dir_key.chmod(0o700)

    result = resolve_cloud_api_key(
        {"enabled": True, "apiKeyFile": os.fspath(dir_key)}, secrets_dir=os.fspath(secrets)
    )
    assert "key" not in result
    assert "regular file" in result["error"]


def test_check_model_target_ipv6_loopback_parity(monkeypatch):
    """Cross-language parity: urlparse strips IPv6 brackets ("::1"), the TS
    mirror strips them explicitly — both must allow an IPv6-loopback bridge."""
    from config import check_model_target

    monkeypatch.delenv("MINNI_AFM_ALLOWED_TARGETS", raising=False)
    monkeypatch.delenv("MINNI_MODEL_ALLOWED_TARGETS", raising=False)

    assert check_model_target("http://[::1]:11437/v1/chat/completions")["allowed"] is True
    assert check_model_target("https://[::1]:11437/v1/chat/completions")["allowed"] is True
    non_loopback = check_model_target("http://[2001:db8::1]:11437/v1")
    assert non_loopback["allowed"] is False
    assert non_loopback["reason"] == "not_allowlisted"
