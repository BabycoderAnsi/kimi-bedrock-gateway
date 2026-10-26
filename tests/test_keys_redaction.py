import re

import common


def test_master_key_format_and_entropy():
    keys = {common.generate_master_key() for _ in range(200)}
    assert len(keys) == 200
    for key in keys:
        assert key.startswith("sk-kgw-")  # LiteLLM requires the sk- prefix
        assert re.fullmatch(r"sk-kgw-[A-Za-z0-9_-]{43}", key)


def test_fingerprint_is_short_and_stable():
    assert common.fingerprint("abc") == common.fingerprint("abc")
    assert len(common.fingerprint("abc")) == 8
    assert "abc" not in common.fingerprint("abc")


def test_redacts_registered_secret_values():
    common.register_secret("super-secret-value-123")
    assert common.redact("token=super-secret-value-123 end") == "token=[REDACTED] end"


def test_redacts_credential_shapes():
    samples = [
        "Authorization: Bearer abcdefghijklmnop123",  # kgw-scan: allow
        "ABSK" + "Q" * 40,
        "bedrock-api-key-" + "x" * 60,
        "sk-kgw-" + "a" * 43,
        "AKIA" + "A" * 16,
        "aws_secret_access_key = " + "s" * 40,
    ]
    for sample in samples:
        out = common.redact(sample)
        assert "[REDACTED]" in out, sample
        assert sample[-12:] not in out


def test_gateway_error_messages_are_redacted():
    common.register_secret("leaky-token-999999")
    err = common.GatewayError("upstream said leaky-token-999999 is bad", hint="try leaky-token-999999")
    assert "leaky-token" not in str(err) and "leaky-token" not in err.hint


def test_redact_handles_none_and_plain_text():
    assert common.redact(None) == ""
    assert common.redact("nothing secret here") == "nothing secret here"
