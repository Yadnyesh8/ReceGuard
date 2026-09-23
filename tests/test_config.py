"""Config loading, env interpolation, and secret redaction (sections 13, 15)."""

from __future__ import annotations

import pydantic
import pytest
import yaml

from raceguard.config import Config, Target, dump_config, load_config, redact
from raceguard.errors import ConfigError

MINIMAL = {
    "version": 1,
    "app": {"module": "shop.main:app"},
    "targets": [{"id": "purchase", "route": "POST /orders"}],
}


def write(tmp_path, payload) -> str:
    path = tmp_path / "raceguard.yaml"
    path.write_text(yaml.safe_dump(payload))
    return str(path)


def test_loads_a_minimal_config(tmp_path):
    config = load_config(write(tmp_path, MINIMAL))
    assert config.target("purchase").method_and_path() == ("POST", "/orders")


def test_missing_file_names_the_fix(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(tmp_path / "nope.yaml"))
    assert "does not exist" in excinfo.value.message


def test_unknown_schema_version_is_refused(tmp_path):
    payload = {**MINIMAL, "version": 99}
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, payload))
    assert "version" in excinfo.value.message


def test_duplicate_target_ids_are_refused(tmp_path):
    payload = {
        **MINIMAL,
        "targets": [{"id": "a", "route": "POST /x"}, {"id": "a", "route": "POST /y"}],
    }
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, payload))
    assert "duplicate target id" in excinfo.value.message


def test_env_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("RG_TEST_TOKEN", "s3cret")
    payload = {**MINIMAL}
    payload["targets"] = [
        {"id": "a", "route": "POST /x", "headers": {"Authorization": "Bearer ${RG_TEST_TOKEN}"}}
    ]
    config = load_config(write(tmp_path, payload))
    assert config.targets[0].headers["Authorization"] == "Bearer s3cret"


def test_env_default_value(tmp_path, monkeypatch):
    monkeypatch.delenv("RG_ABSENT", raising=False)
    payload = {**MINIMAL, "app": {"module": "m:app", "health_path": "${RG_ABSENT:-/healthz}"}}
    assert load_config(write(tmp_path, payload)).app.health_path == "/healthz"


def test_undefined_env_var_is_an_error_not_an_empty_string(tmp_path, monkeypatch):
    """Silently substituting "" produces an unauthenticated request that 401s,
    and the run then reports 'not reproduced', which would be a lie."""
    monkeypatch.delenv("RG_MISSING", raising=False)
    payload = {**MINIMAL}
    payload["targets"] = [
        {"id": "a", "route": "POST /x", "headers": {"Authorization": "${RG_MISSING}"}}
    ]
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, payload))
    assert "RG_MISSING" in excinfo.value.message


def test_secrets_never_reach_an_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("RG_TEST_TOKEN", "super-secret-value")
    payload = {**MINIMAL}
    payload["targets"] = [
        {"id": "a", "route": "POST /x", "headers": {"Authorization": "Bearer ${RG_TEST_TOKEN}"}}
    ]
    config = load_config(write(tmp_path, payload))
    serialised = str(config.redacted())
    assert "super-secret-value" not in serialised
    assert "redacted" in serialised


@pytest.mark.parametrize(
    "key", ["Authorization", "api_key", "X-Api-Key", "password", "session_token", "Cookie"]
)
def test_redaction_covers_the_usual_names(key):
    assert redact({key: "value"})[key] == "<redacted>"


def test_route_and_group_are_mutually_exclusive():
    with pytest.raises(pydantic.ValidationError):
        Target(id="a", route="POST /x", group=[{"route": "POST /y"}])


def test_group_shape_is_accepted():
    """The multi endpoint shape is reserved in v1 (section 22, question 1)."""
    target = Target(id="a", group=[{"route": "POST /x"}, {"route": "POST /y"}])
    assert len(target.group) == 2


def test_target_overrides_defaults():
    config = Config(
        app={"module": "m:app"},
        defaults={"trials": 20, "concurrency": 8},
        targets=[Target(id="a", route="POST /x", trials=3, concurrency=2)],
    )
    resolved = config.resolved(config.targets[0])
    assert resolved["trials"] == 3 and resolved["concurrency"] == 2


def test_app_needs_some_way_to_boot():
    with pytest.raises(pydantic.ValidationError):
        Config(app={}, targets=[])


def test_shop_config_round_trips(shop):
    config = load_config(str(shop / "raceguard.yaml"))
    reloaded = yaml.safe_load(dump_config(config))
    assert reloaded["version"] == 1
    assert {t["id"] for t in reloaded["targets"]} == {t.id for t in config.targets}


def test_pinned_by_digest_detection():
    assert not Config(
        app={"module": "m:a"}, database={"image": "postgres:17"}
    ).database.pinned_by_digest
    pinned = Config(app={"module": "m:a"}, database={"image": "postgres:17@sha256:abc"})
    assert pinned.database.pinned_by_digest
