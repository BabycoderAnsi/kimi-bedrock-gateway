"""Credential storage, rotation and failure recovery (no real keychain, no AWS)."""

import argparse
import json
import os

import pytest

import common
import manage
import validate


def _ns(**kw):
    base = dict(yes=False, no_validate=False, save_unvalidated=False, restart=False, rollback=False,
                forget_previous=False, non_interactive=True)
    base.update(kw)
    return argparse.Namespace(**base)


def _ok(*_a, **_k):
    return validate.ProbeResult(True, "ok", "fine")


def _bad(code="invalid_token"):
    return lambda *_a, **_k: validate.ProbeResult(False, code, "Bedrock rejected the bearer token", "make a new one")


@pytest.fixture
def file_store(kgw_home):
    s = common.default_settings()
    s["credential_backend"] = "file"
    common.save_settings(s)
    return s, common.store_for(s)


def test_file_backend_is_0600_and_roundtrips(file_store, kgw_home):
    _, store = file_store
    store.set(common.SECRET_BEDROCK_TOKEN, "tok-AAAA1111")
    assert store.get(common.SECRET_BEDROCK_TOKEN) == "tok-AAAA1111"
    if os.name != "nt":
        assert oct((kgw_home / "secrets.json").stat().st_mode & 0o777) == "0o600"
    assert store.delete(common.SECRET_BEDROCK_TOKEN)
    assert store.get(common.SECRET_BEDROCK_TOKEN) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check")
def test_file_backend_refuses_world_readable(file_store, kgw_home):
    _, store = file_store
    store.set(common.SECRET_BEDROCK_TOKEN, "tok-AAAA1111")
    os.chmod(kgw_home / "secrets.json", 0o644)
    with pytest.raises(common.GatewayError) as exc:
        store.get(common.SECRET_BEDROCK_TOKEN)
    assert exc.value.code == "insecure_permissions"


def test_store_rejects_empty_or_padded_values(file_store):
    _, store = file_store
    for bad in ("", " tok", "tok\n"):
        with pytest.raises(common.GatewayError):
            store.set(common.SECRET_BEDROCK_TOKEN, bad)


def test_keyring_chunks_long_tokens(kgw_home, monkeypatch):
    from conftest import MemoryKeyring

    kr = MemoryKeyring(max_len=1200)  # stricter than Windows' 2560-byte UTF-16 blob limit
    monkeypatch.setattr(common.SecretStore, "_keyring", staticmethod(lambda: kr))
    store = common.SecretStore("keyring")
    long_token = "bedrock-api-key-" + "Z" * 3000
    store.set(common.SECRET_BEDROCK_TOKEN, long_token)
    assert store.get(common.SECRET_BEDROCK_TOKEN) == long_token
    assert kr.data[(common.keyring_service(), common.SECRET_BEDROCK_TOKEN)].startswith("kgw-chunked:")
    store.set(common.SECRET_BEDROCK_TOKEN, "short-token-1")  # replacing removes old parts
    assert not any(".part" in name for (_, name) in kr.data)
    assert store.delete(common.SECRET_BEDROCK_TOKEN)
    assert kr.data == {}


def test_read_secret_from_pipe_strips_and_registers(monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("piped-token-777777\n"))
    assert common.read_secret("x") == "piped-token-777777"
    assert common.redact("piped-token-777777") == "[REDACTED]"
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    with pytest.raises(common.GatewayError):
        common.read_secret("x")


def test_update_saves_validated_token_and_keeps_previous(file_store, monkeypatch):
    s, store = file_store
    store.set(common.SECRET_BEDROCK_TOKEN, "old-token-1111")
    monkeypatch.setattr(common, "read_secret", lambda _p: "new-token-2222")
    monkeypatch.setattr(validate, "probe_bedrock", _ok)
    assert manage.update_token(_ns(yes=True), s, store) is True
    assert store.get(common.SECRET_BEDROCK_TOKEN) == "new-token-2222"
    assert store.get(common.SECRET_BEDROCK_TOKEN + ".previous") == "old-token-1111"


def test_update_failure_preserves_existing_token(file_store, monkeypatch):
    s, store = file_store
    store.set(common.SECRET_BEDROCK_TOKEN, "old-token-1111")
    monkeypatch.setattr(common, "read_secret", lambda _p: "bad-token-3333")
    monkeypatch.setattr(validate, "probe_bedrock", _bad())
    with pytest.raises(common.GatewayError) as exc:
        manage.update_token(_ns(yes=True), s, store)
    assert exc.value.code == "invalid_token"
    assert store.get(common.SECRET_BEDROCK_TOKEN) == "old-token-1111"
    assert store.get(common.SECRET_BEDROCK_TOKEN + ".previous") is None


def test_update_failure_can_be_overridden_explicitly(file_store, monkeypatch):
    s, store = file_store
    monkeypatch.setattr(common, "read_secret", lambda _p: "unverified-4444")
    monkeypatch.setattr(validate, "probe_bedrock", _bad("network_error"))
    manage.update_token(_ns(yes=True, save_unvalidated=True), s, store)
    assert store.get(common.SECRET_BEDROCK_TOKEN) == "unverified-4444"


def test_update_requires_confirmation_to_replace(file_store, monkeypatch):
    s, store = file_store
    store.set(common.SECRET_BEDROCK_TOKEN, "old-token-1111")
    monkeypatch.setattr(common, "read_secret", lambda _p: "new-token-2222")
    monkeypatch.setattr(validate, "probe_bedrock", _ok)
    with pytest.raises(common.GatewayError) as exc:
        manage.update_token(_ns(yes=False), s, store)  # non-interactive without --yes
    assert exc.value.code == "confirmation_required"
    assert store.get(common.SECRET_BEDROCK_TOKEN) == "old-token-1111"


def test_update_rejects_master_key_reuse(file_store, monkeypatch):
    s, store = file_store
    store.set(common.SECRET_MASTER_KEY, "sk-kgw-same-value")
    monkeypatch.setattr(common, "read_secret", lambda _p: "sk-kgw-same-value")
    with pytest.raises(common.GatewayError) as exc:
        manage.update_token(_ns(yes=True), s, store)
    assert exc.value.code == "key_reuse"


def test_rollback_restores_previous(file_store, monkeypatch):
    s, store = file_store
    store.set(common.SECRET_BEDROCK_TOKEN, "current-5555")
    store.set(common.SECRET_BEDROCK_TOKEN + ".previous", "older-6666")
    assert manage.cmd_update_key(_ns(rollback=True, yes=True)) == 0
    assert store.get(common.SECRET_BEDROCK_TOKEN) == "older-6666"
    assert store.get(common.SECRET_BEDROCK_TOKEN + ".previous") == "current-5555"


def test_token_never_printed(file_store, monkeypatch, capsys):
    s, store = file_store
    monkeypatch.setattr(common, "read_secret", lambda _p: (common.register_secret("visible-7777-token") or
                                                          "visible-7777-token"))
    monkeypatch.setattr(validate, "probe_bedrock",
                        lambda *_a, **_k: validate.ProbeResult(False, "invalid_token",
                                                               "upstream echoed visible-7777-token"))
    with pytest.raises(common.GatewayError):
        manage.update_token(_ns(yes=True), s, store)
    out = capsys.readouterr().out
    assert "visible-7777-token" not in out


def test_probe_bedrock_classifies_errors(monkeypatch):
    s = common.default_settings()
    cases = [
        (403, {"x-amzn-errortype": "AccessDeniedException:x"},
         b'{"message":"Authentication failed: Please make sure your API Key is valid."}', "invalid_token"),
        (403, {"x-amzn-errortype": "AccessDeniedException:x"},
         b'{"message":"You don\'t have access to the model with the specified model ID."}', "model_access_denied"),
        (400, {"x-amzn-errortype": "ValidationException:x"},
         b'{"message":"The provided model identifier is invalid."}', "unsupported_model_or_region"),
        (429, {"x-amzn-errortype": "ThrottlingException:x"}, b'{"message":"slow down"}', "throttled"),
        (400, {}, b'{"message":"structured output schema uses unsupported regex negative look-ahead"}',
         "structured_output_regex"),
        (503, {}, b'{"message":"unavailable"}', "bedrock_server_error"),
    ]
    for status, headers, body, code in cases:
        monkeypatch.setattr(validate, "_http", lambda *a, _r=(status, headers, body), **k: _r)
        assert validate.probe_bedrock(s, "tok-xyz-123456").code == code


def test_probe_network_error(monkeypatch):
    import urllib.error

    def boom(*_a, **_k):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(validate, "_http", boom)
    res = validate.probe_bedrock(common.default_settings(), "tok-xyz-123456")
    assert res.code == "network_error" and not res.ok


@pytest.mark.parametrize("status,body,code", [
    # Chat Completions reports IAM denials as 401: must not be blamed on the token.
    (401, {"error": {"message": "User: arn:aws:iam::1:user/u is not authorized to perform: bedrock:InvokeModel "
                                "on resource: x"}}, "model_access_denied"),
    (403, {"Message": "User: arn:aws:iam::1:user/u is not authorized to perform: bedrock:CallWithBearerToken "
                      "on resource: *"}, "bearer_token_not_allowed"),
    (400, {"error": {"message": "Application inference profiles are not supported by this API."}}, "unsupported_api"),
    (401, {"message": "Invalid API Key format"}, "invalid_token"),
])
def test_bedrock_error_classification(status, body, code):
    result = validate.classify_bedrock_error(status, {}, json.dumps(body).encode(), "m", "ap-south-1")
    assert result.code == code


def test_sigv4_signature_matches_botocore():
    pytest.importorskip("botocore")
    import datetime as dt  # noqa: PLC0415

    from botocore.auth import SigV4Auth  # noqa: PLC0415
    from botocore.awsrequest import AWSRequest  # noqa: PLC0415
    from botocore.credentials import Credentials  # noqa: PLC0415

    keys = common.AwsKeys("AKIA" + "ABCDEFGHIJKLMNOP", "abcdefghijklmnopqrstuvwxyz" + "0123456789/+AB")  # kgw-scan: allow (fake test key)
    url = ("https://bedrock-runtime.ap-south-1.amazonaws.com/model/"
           "arn%3Aaws%3Abedrock%3Aap-south-1%3A123456789012%3Aapplication-inference-profile%2Fabc/converse")
    body = b'{"messages": []}'
    req = AWSRequest(method="POST", url=url, data=body)
    SigV4Auth(Credentials(keys.access_key_id, keys.secret_access_key), "bedrock", "ap-south-1").add_auth(req)
    stamp = req.headers["X-Amz-Date"]
    now = dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
    ours = validate.sigv4_headers("POST", url, body, "ap-south-1", keys, now=now)
    assert ours["Authorization"] == req.headers["Authorization"]


def test_access_key_probe_errors_name_the_key_pair():
    result = validate.classify_bedrock_error(403, {}, b'{"message":"The security token included in the request is invalid."}',
                                             "m", "ap-south-1")
    assert result.code == "invalid_token"  # bearer wording; probe_bedrock rewrites it for AwsKeys


class FlakyKeyring:
    """MemoryKeyring whose ``fail_after``-th set_password call (and later ones) raises."""

    def __init__(self, fail_after=None):
        from conftest import MemoryKeyring

        self.inner, self.fail_after, self.sets = MemoryKeyring(), fail_after, 0
        self.data = self.inner.data

    def get_password(self, service, name):
        return self.inner.get_password(service, name)

    def set_password(self, service, name, value):
        self.sets += 1
        if self.fail_after is not None and self.sets > self.fail_after:
            raise OSError("keychain locked")
        self.inner.set_password(service, name, value)

    def delete_password(self, service, name):
        self.inner.delete_password(service, name)


@pytest.mark.parametrize("old,new", [("short-old-token", "Y" * 2500),     # short -> chunked
                                     ("X" * 2500, "Y" * 2500),            # chunked -> chunked
                                     ("X" * 2500, "short-new-token")],    # chunked -> short
                         ids=["short-to-chunked", "chunked-to-chunked", "chunked-to-short"])
def test_failed_keyring_write_keeps_the_old_value(kgw_home, monkeypatch, old, new):
    kr = FlakyKeyring()
    monkeypatch.setattr(common.SecretStore, "_keyring", staticmethod(lambda: kr))
    store = common.SecretStore("keyring")
    store.set(common.SECRET_BEDROCK_TOKEN, old)
    before = dict(kr.data)
    writes = 3 + 1 if len(new) > 1000 else 1  # 3 parts + head, or just the head
    for fail_after in range(writes):  # fail at each write of the replacement in turn
        kr.sets, kr.fail_after = 0, fail_after
        with pytest.raises(common.GatewayError) as exc:
            store.set(common.SECRET_BEDROCK_TOKEN, new)
        assert "previously stored value was kept" in str(exc.value)
        kr.fail_after = None
        assert store.get(common.SECRET_BEDROCK_TOKEN) == old
        assert kr.data == before  # no orphaned parts of the half-written value
    store.set(common.SECRET_BEDROCK_TOKEN, new)
    assert store.get(common.SECRET_BEDROCK_TOKEN) == new
    expected_parts = 3 if len(new) > 1000 else 0
    assert sum(".part" in name for (_, name) in kr.data) == expected_parts  # old parts cleaned up


def test_legacy_chunked_entries_still_read_and_are_migrated(kgw_home, memory_keyring):
    service = common.keyring_service()
    name = common.SECRET_BEDROCK_TOKEN
    memory_keyring.data.update({(service, name): "kgw-chunked:2", (service, f"{name}.part0"): "A" * 1000,
                                (service, f"{name}.part1"): "B" * 10})
    store = common.SecretStore("keyring")
    assert store.get(name) == "A" * 1000 + "B" * 10
    store.set(name, "C" * 1500)
    assert store.get(name) == "C" * 1500
    assert (service, f"{name}.part0") not in memory_keyring.data
    assert memory_keyring.data[(service, name)].count(":") == 2  # kgw-chunked:N:TAG
    assert store.delete(name) and memory_keyring.data == {}


def _classifier_ns(**kw):
    base = dict(backend="bedrock", model_id=None, base_model=None, separate_credential=True, no_validate=False,
                replace_token=False, non_interactive=True)
    base.update(kw)
    return argparse.Namespace(**base)


def test_separate_classifier_key_is_validated_before_it_replaces_the_stored_one(file_store, monkeypatch):
    s, store = file_store
    store.set(common.SECRET_CLASSIFIER_BEDROCK_TOKEN, "classifier-old-1111")
    monkeypatch.setattr(common, "read_secret", lambda _p: "classifier-new-2222")
    monkeypatch.setattr(validate, "probe_bedrock", _bad("access_denied"))
    with pytest.raises(common.GatewayError):
        manage.enable_classifier(_classifier_ns(replace_token=True), s, store)
    assert store.get(common.SECRET_CLASSIFIER_BEDROCK_TOKEN) == "classifier-old-1111"
    assert common.load_settings()["classifier"]["backend"] == "none"

    monkeypatch.setattr(validate, "probe_bedrock", _ok)
    manage.enable_classifier(_classifier_ns(replace_token=True), s, store)
    assert store.get(common.SECRET_CLASSIFIER_BEDROCK_TOKEN) == "classifier-new-2222"


def test_separate_classifier_key_is_reused_unless_replace_token(file_store, monkeypatch):
    s, store = file_store
    store.set(common.SECRET_CLASSIFIER_BEDROCK_TOKEN, "classifier-old-1111")
    probed = []
    monkeypatch.setattr(common, "read_secret", lambda _p: pytest.fail("must not prompt for a new key"))
    monkeypatch.setattr(validate, "probe_bedrock", lambda _s, token, *_a, **_k: probed.append(token) or _ok())
    manage.enable_classifier(_classifier_ns(), s, store)
    assert probed == ["classifier-old-1111"]
    assert common.load_settings()["classifier"]["credential"] == "separate"
