import json

import pytest
import yaml

import common


def test_defaults_match_expected_deployment():
    s = common.default_settings()
    assert s["model_alias"] == "kimi-k3"
    assert s["bedrock_model_id"] == "in.moonshotai.kimi-k3"
    assert s["aws_region"] == "ap-south-1"
    assert (s["proxy_host"], s["proxy_port"]) == ("127.0.0.1", 4000)
    assert s["classifier"]["backend"] == "none"
    assert [i for i in common.validate_settings(s) if i.level == "error"] == []


def test_load_merges_user_values_over_defaults(kgw_home):
    kgw_home.mkdir()
    (kgw_home / "settings.json").write_text(json.dumps({"aws_region": "us-east-1",
                                                         "bedrock_model_id": "us.moonshotai.kimi-k3",
                                                         "classifier": {"backend": "bedrock"}}))
    s = common.load_settings()
    assert s["aws_region"] == "us-east-1"
    assert s["classifier"]["backend"] == "bedrock"
    assert s["classifier"]["request_model"] == "claude-sonnet-5"  # default kept inside nested dict
    assert s["proxy_port"] == 4000


def test_invalid_json_is_actionable(kgw_home):
    kgw_home.mkdir()
    (kgw_home / "settings.json").write_text("{nope")
    with pytest.raises(common.GatewayError) as exc:
        common.load_settings()
    assert exc.value.code == "invalid_settings"


def test_save_settings_is_private_and_roundtrips(kgw_home):
    s = common.default_settings()
    s["aws_region"] = "ap-south-2"
    common.save_settings(s)
    assert common.is_private(kgw_home / "settings.json")
    assert common.load_settings()["aws_region"] == "ap-south-2"


@pytest.mark.parametrize("region,model,ok", [
    ("ap-south-1", "in.moonshotai.kimi-k3", True),
    ("ap-south-2", "in.moonshotai.kimi-k3", True),
    ("us-east-1", "in.moonshotai.kimi-k3", False),
    ("us-west-2", "us.moonshotai.kimi-k3", True),
    ("eu-west-1", "us.moonshotai.kimi-k3", False),
    ("eu-west-1", "global.moonshotai.kimi-k3", True),
    ("ap-south-1", "moonshotai.kimi-k3", False),  # no In-Region availability
])
def test_region_profile_compatibility(region, model, ok):
    s = common.default_settings()
    s["aws_region"], s["bedrock_model_id"] = region, model
    errors = [i for i in common.validate_settings(s) if i.level == "error"]
    assert (not errors) == ok


def test_region_mismatch_suggests_profile_but_does_not_substitute():
    s = common.default_settings()
    s["aws_region"] = "us-east-1"
    errors = [i for i in common.validate_settings(s) if i.level == "error"]
    assert "us.moonshotai.kimi-k3" in errors[0].hint
    assert s["bedrock_model_id"] == "in.moonshotai.kimi-k3"  # untouched


def test_unknown_model_is_warning_not_error():
    s = common.default_settings()
    s["bedrock_model_id"] = "apac.moonshotai.kimi-k4"
    levels = {i.level for i in common.validate_settings(s)}
    assert levels == {"warning"}


@pytest.mark.parametrize("host,allow,level", [
    ("127.0.0.1", False, None), ("0.0.0.0", False, "error"), ("0.0.0.0", True, "warning"),
])
def test_non_loopback_requires_opt_in(host, allow, level):
    s = common.default_settings()
    s["proxy_host"], s["allow_non_loopback"] = host, allow
    levels = {i.level for i in common.validate_settings(s)}
    assert (level in levels) if level else not levels


@pytest.mark.parametrize("bad", [{"proxy_port": 80}, {"proxy_port": "4000"}, {"aws_region": "mumbai"},
                                 {"model_alias": "claude-kimi"}, {"credential_backend": "plaintext"},
                                 {"bedrock_endpoint_url": "http://evil.example.com"}])
def test_invalid_values_rejected(bad):
    s = common.default_settings()
    s.update(bad)
    assert any(i.level == "error" for i in common.validate_settings(s))


def test_classifier_must_be_claude():
    s = common.default_settings()
    s["classifier"].update(backend="bedrock", bedrock_model_id="in.moonshotai.kimi-k3")
    assert any("not a Claude model" in i.message for i in common.validate_settings(s))
    s["classifier"].update(bedrock_model_id=None, request_model="kimi-k3")
    assert any(i.level == "error" for i in common.validate_settings(s))


@pytest.mark.parametrize("region,expected", [("ap-south-1", "in."), ("us-east-1", "us."), ("eu-west-1", "eu."),
                                             ("ap-southeast-2", "au."), ("ap-northeast-1", "global.")])
def test_classifier_profile_by_region(region, expected):
    assert common.default_classifier_model_for_region(region).startswith(expected)


def test_rendered_config_references_env_and_has_no_secrets():
    s = common.default_settings()
    text = common.render_runtime_config(s)
    cfg = yaml.safe_load(text)
    kimi = cfg["model_list"][0]
    assert kimi["model_name"] == "kimi-k3"
    assert kimi["litellm_params"]["model"] == "bedrock/chat_completions/in.moonshotai.kimi-k3"
    assert kimi["litellm_params"]["aws_region_name"] == "ap-south-1"
    assert kimi["litellm_params"]["api_key"] == "os.environ/AWS_BEARER_TOKEN_BEDROCK"
    assert cfg["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    assert cfg["litellm_settings"]["callbacks"] == ["kimi_gateway_hooks.proxy_handler_instance"]
    assert len(cfg["model_list"]) == 1  # no classifier route unless configured
    assert "sk-" not in text and "ABSK" not in text


def test_rendered_config_with_classifier_and_endpoint():
    s = common.default_settings()
    s["bedrock_endpoint_url"] = "http://127.0.0.1:4556"
    s["classifier"].update(backend="bedrock", credential="separate")
    cfg = yaml.safe_load(common.render_runtime_config(s))
    names = [m["model_name"] for m in cfg["model_list"]]
    assert names == ["kimi-k3", "claude-sonnet-5"]
    cls = cfg["model_list"][1]["litellm_params"]
    assert cls["model"] == "bedrock/in.anthropic.claude-sonnet-5"
    assert cls["api_key"] == "os.environ/KGW_CLASSIFIER_BEDROCK_TOKEN"
    assert all(m["litellm_params"]["aws_bedrock_runtime_endpoint"] == "http://127.0.0.1:4556"
               for m in cfg["model_list"])


def test_template_has_no_unknown_placeholders(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("x: __NOT_A_PLACEHOLDER__\n")
    with pytest.raises(common.GatewayError):
        common.render_runtime_config(common.default_settings(), bad)


def test_kimi_api_and_effort_defaults_and_validation():
    s = common.default_settings()
    assert (s["kimi_api"], s["kimi_reasoning_effort"]) == ("chat_completions", "low")
    s["kimi_api"] = "converse"
    cfg = yaml.safe_load(common.render_runtime_config(s))
    assert cfg["model_list"][0]["litellm_params"]["model"] == "bedrock/converse/in.moonshotai.kimi-k3"
    assert any("ignores reasoning_effort" in i.message for i in common.validate_settings(s))
    for bad in ({"kimi_api": "invoke"}, {"kimi_reasoning_effort": "minimal"}):
        t = common.default_settings()
        t.update(bad)
        assert any(i.level == "error" for i in common.validate_settings(t))


APP_ARN = "arn:aws:bedrock:ap-south-1:123456789012:application-inference-profile/abc123xyz"


def test_application_profile_forces_converse_and_back():
    s = common.default_settings()
    common.set_kimi_model(s, APP_ARN)
    assert s["kimi_api"] == "converse"
    assert [i for i in common.validate_settings(s) if i.level == "error"] == []
    cfg = yaml.safe_load(common.render_runtime_config(s))
    assert cfg["model_list"][0]["litellm_params"]["model"] == f"bedrock/converse/{APP_ARN}"
    common.set_kimi_model(s, "in.moonshotai.kimi-k3")
    assert s["kimi_api"] == "chat_completions"


def test_application_profile_rejects_chat_completions_and_region_mismatch():
    s = dict(common.default_settings(), bedrock_model_id=APP_ARN)  # kimi_api left at chat_completions
    errors = [i.message for i in common.validate_settings(s) if i.level == "error"]
    assert any("Chat Completions API does not accept" in e for e in errors)
    s = dict(common.default_settings(), bedrock_model_id=APP_ARN, kimi_api="converse", aws_region="us-east-1")
    errors = [i.message for i in common.validate_settings(s) if i.level == "error"]
    assert any("is in ap-south-1 but aws_region is us-east-1" in e for e in errors)


def test_access_key_auth_renders_env_references_only():
    s = dict(common.default_settings(), aws_auth="access_key", classifier=dict(
        common.default_settings()["classifier"], backend="bedrock"))
    assert [i for i in common.validate_settings(s) if i.level == "error"] == []
    text = common.render_runtime_config(s)
    cfg = yaml.safe_load(text)
    for entry in cfg["model_list"]:  # Kimi and the shared-credential classifier route
        params = entry["litellm_params"]
        assert params["aws_access_key_id"] == "os.environ/KGW_AWS_ACCESS_KEY_ID"
        assert params["aws_secret_access_key"] == "os.environ/KGW_AWS_SECRET_ACCESS_KEY"
        assert "api_key" not in params
    assert [i for i in common.validate_settings(dict(s, aws_auth="sso")) if i.level == "error"]


def test_aws_keys_repr_hides_secret():
    keys = common.AwsKeys("AKIA" + "ABCDEFGHIJKLMNOP", "s" * 40)  # kgw-scan: allow (fake test key)
    assert "s" * 40 not in repr(keys) and "ABCDEFGHIJKLMNOP" not in repr(keys)


CLAUDE_ARN = "arn:aws:bedrock:ap-south-1:123456789012:application-inference-profile/zzz999"


def test_classifier_application_profile_renders_base_model_and_model_id():
    s = common.default_settings()
    s["classifier"].update(backend="bedrock", bedrock_model_id=CLAUDE_ARN)
    assert [i for i in common.validate_settings(s) if i.level == "error"] == []
    route = yaml.safe_load(common.render_runtime_config(s))["model_list"][1]
    assert route["model_name"] == "claude-sonnet-5"
    assert route["litellm_params"]["model"] == "bedrock/anthropic.claude-sonnet-5"
    assert route["litellm_params"]["model_id"] == CLAUDE_ARN


@pytest.mark.parametrize("change,needle", [
    ({"base_model": "moonshotai.kimi-k3"}, "is not a Claude model"),
    ({"bedrock_model_id": CLAUDE_ARN.replace("ap-south-1", "us-east-1")}, "not in ap-south-1"),
])
def test_classifier_application_profile_validation(change, needle):
    s = common.default_settings()
    s["classifier"].update({"backend": "bedrock", "bedrock_model_id": CLAUDE_ARN, **change})
    assert any(needle in i.message for i in common.validate_settings(s) if i.level == "error")


def test_classifier_cannot_reuse_the_kimi_arn():
    s = common.default_settings()
    common.set_kimi_model(s, APP_ARN)
    s["classifier"].update(backend="bedrock", bedrock_model_id=APP_ARN)
    assert any("same ARN as the Kimi model" in i.message for i in common.validate_settings(s) if i.level == "error")
