"""OPTIONAL live integration test against real Amazon Bedrock.

Runs only when you explicitly opt in, because it uses your stored credentials and
bills a few tokens to your AWS account:

    KGW_LIVE_BEDROCK=1 .venv/bin/python -m pytest -m live tests/test_live_bedrock.py

It reads the token from the configured credential store at runtime and never prints it.
"""

import os

import pytest

import common
import validate

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("KGW_LIVE_BEDROCK") != "1", reason="set KGW_LIVE_BEDROCK=1 to opt in")]


@pytest.fixture(scope="module")
def configured():
    settings = common.load_settings()
    token = common.store_for(settings).get(common.SECRET_BEDROCK_TOKEN)
    if not token:
        pytest.skip("no Bedrock token stored; run setup first")
    common.register_secret(token)
    return settings, token


def test_kimi_direct(configured):
    settings, token = configured
    result = validate.probe_bedrock(settings, token)
    assert result.ok, f"{result.code}: {result.message}"


def test_kimi_through_running_gateway(configured):
    settings, _ = configured
    master = common.store_for(settings).get(common.SECRET_MASTER_KEY)
    if not validate.proxy_health(settings, master).ok:
        pytest.skip("gateway not running; start it with ./start.sh --background")
    result = validate.proxy_messages_test(settings, master)
    assert result.ok, f"{result.code}: {result.message}"


def test_classifier_route_if_configured(configured):
    settings, token = configured
    if settings["classifier"]["backend"] != "bedrock":
        pytest.skip("Bedrock classifier route not configured")
    model = settings["classifier"].get("bedrock_model_id") or \
        common.default_classifier_model_for_region(settings["aws_region"])
    result = validate.probe_bedrock(settings, token, model, kind="claude")
    assert result.ok, f"{result.code}: {result.message}"
