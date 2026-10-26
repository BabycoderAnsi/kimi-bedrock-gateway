"""Setup can run repeatedly without regenerating keys or overwriting credentials."""

import argparse
import io

import pytest

import common
import manage
import validate


def _setup_args(**kw):
    base = dict(region=None, model_id=None, endpoint_url=None, credential_backend="file", token_stdin=True,
                replace_token=False, reset=False, skip_live=True, no_validate=False, save_unvalidated=False,
                classifier=None, separate_credential=False, yes=True, allow_rosetta=False, non_interactive=True)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture(autouse=True)
def _no_aws(monkeypatch):
    monkeypatch.setattr(validate, "probe_bedrock", lambda *a, **k: validate.ProbeResult(True, "ok", "mock ok"))
    monkeypatch.setattr(manage, "report_claude", lambda *_a: None)


def _run_setup(monkeypatch, token="first-token-1111", **kw):
    monkeypatch.setattr("sys.stdin", io.StringIO(token + "\n"))
    return manage.cmd_setup(_setup_args(**kw))


def test_first_run_creates_settings_master_key_and_token(kgw_home, monkeypatch):
    assert _run_setup(monkeypatch) == 0
    s = common.load_settings()
    store = common.store_for(s)
    assert store.get(common.SECRET_BEDROCK_TOKEN) == "first-token-1111"
    assert store.get(common.SECRET_MASTER_KEY).startswith("sk-kgw-")
    assert (kgw_home / "runtime" / "litellm-config.yaml").exists()
    assert not (kgw_home / "setup-progress.json").exists()


def test_second_run_keeps_master_key_and_token(kgw_home, monkeypatch):
    _run_setup(monkeypatch)
    store = common.store_for(common.load_settings())
    key1 = store.get(common.SECRET_MASTER_KEY)
    assert _run_setup(monkeypatch, token="different-2222") == 0  # token offered but not requested
    assert store.get(common.SECRET_MASTER_KEY) == key1
    assert store.get(common.SECRET_BEDROCK_TOKEN) == "first-token-1111"


def test_replace_token_flag_updates_only_token(kgw_home, monkeypatch):
    _run_setup(monkeypatch)
    store = common.store_for(common.load_settings())
    key1 = store.get(common.SECRET_MASTER_KEY)
    _run_setup(monkeypatch, token="second-3333", replace_token=True)
    assert store.get(common.SECRET_BEDROCK_TOKEN) == "second-3333"
    assert store.get(common.SECRET_MASTER_KEY) == key1


def test_region_update_preserves_credentials(kgw_home, monkeypatch):
    _run_setup(monkeypatch)
    _run_setup(monkeypatch, region="us-east-1", model_id="us.moonshotai.kimi-k3")
    s = common.load_settings()
    assert (s["aws_region"], s["bedrock_model_id"]) == ("us-east-1", "us.moonshotai.kimi-k3")
    assert common.store_for(s).get(common.SECRET_BEDROCK_TOKEN) == "first-token-1111"


def test_incompatible_region_is_rejected_without_substitution(kgw_home, monkeypatch):
    _run_setup(monkeypatch)
    with pytest.raises(common.GatewayError) as exc:
        _run_setup(monkeypatch, region="us-east-1")
    assert "us.moonshotai.kimi-k3" in (exc.value.hint or "")
    assert common.load_settings()["aws_region"] == "ap-south-1"


def test_reset_keeps_backup_and_credentials(kgw_home, monkeypatch):
    _run_setup(monkeypatch, region="ap-south-2")
    _run_setup(monkeypatch, reset=True)
    assert common.load_settings()["aws_region"] == "ap-south-1"
    assert any((kgw_home / "backups").iterdir())
    assert common.store_for(common.load_settings()).get(common.SECRET_BEDROCK_TOKEN) == "first-token-1111"


def test_failed_validation_on_first_run_stores_nothing(kgw_home, monkeypatch):
    monkeypatch.setattr(validate, "probe_bedrock",
                        lambda *a, **k: validate.ProbeResult(False, "invalid_token", "rejected", "new key"))
    with pytest.raises(common.GatewayError):
        _run_setup(monkeypatch)
    store = common.store_for(common.load_settings())
    assert store.get(common.SECRET_BEDROCK_TOKEN) is None
    # Interrupted setup leaves a progress marker for the next run.
    assert (kgw_home / "setup-progress.json").exists()
    monkeypatch.setattr(validate, "probe_bedrock", lambda *a, **k: validate.ProbeResult(True, "ok", "ok"))
    assert _run_setup(monkeypatch) == 0
    assert not (kgw_home / "setup-progress.json").exists()


def test_venv_fingerprint_changes_with_litellm_version(tmp_path):
    s = common.default_settings()
    import sys
    from pathlib import Path

    py = Path(sys.executable)
    a = manage.requirements_fingerprint_for(py, s)
    s["litellm_version"] = "9.9.9"
    assert manage.requirements_fingerprint_for(py, s) != a
