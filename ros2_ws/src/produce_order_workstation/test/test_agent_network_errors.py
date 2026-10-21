"""Transport failures must not be mislabeled as invalid keys or leak headers."""

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError
import pytest

from produce_order_workstation.agent import explain_model_error, create_model


def test_network_error_reports_cause_types_without_private_error_text():
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    exc = APIConnectionError(message="private-key-must-not-appear", request=request)
    exc.__cause__ = httpx.ConnectError("private-key-must-not-appear", request=request)
    text = explain_model_error(exc)
    assert "APIConnectionError" in text and "ConnectError" in text
    assert "不是 Key 鉴权失败" in text
    assert "private-key" not in text


@pytest.mark.parametrize("code", [401, 403, 429, 500])
def test_provider_status_is_reported_without_echoing_private_body(code):
    request = httpx.Request("POST", "https://example.invalid")
    exc = APIStatusError("private-key", response=httpx.Response(code, request=request), body={"secret": "private-key"})
    text = explain_model_error(exc)
    assert str(code) in text and "private-key" not in text


def test_timeout_is_transport_not_authentication_failure():
    text = explain_model_error(APITimeoutError(request=httpx.Request("POST", "https://example.invalid")))
    assert "APITimeoutError" in text and "不是 Key 鉴权失败" in text


def test_dns_failure_is_distinguished_from_invalid_key():
    request = httpx.Request("POST", "https://example.invalid")
    exc = APIConnectionError(request=request)
    exc.__cause__ = httpx.ConnectError("[Errno -3] Temporary failure in name resolution", request=request)
    assert "DNS 解析失败" in explain_model_error(exc)


def test_model_proxy_is_explicit_and_not_inherited(monkeypatch):
    from unittest.mock import MagicMock

    factory = MagicMock()
    sdk = MagicMock()
    monkeypatch.setattr(httpx, "Client", factory)
    monkeypatch.setattr("openai.OpenAI", sdk)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "fixture-never-real-key")
    monkeypatch.delenv("PRODUCE_AGENT_PROXY_URL", raising=False)
    create_model({"base_url": "https://example.invalid", "timeout_s": 1, "proxy_url": "http://127.0.0.1:7897"})
    factory.assert_called_once_with(proxy="http://127.0.0.1:7897", trust_env=False)
    assert sdk.call_args.kwargs["http_client"] is factory.return_value
