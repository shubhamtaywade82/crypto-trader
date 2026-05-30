import pytest
import time
import requests
from unittest.mock import MagicMock, patch

from crypto_trader.ai.providers.rotation import ResourceRotator
from crypto_trader.ai.providers.ollama_cloud import OllamaCloudProvider
from crypto_trader.ai.providers.ollama_local import OllamaLocalProvider
from crypto_trader.patterns.circuit_breaker import CircuitState, CircuitOpenError


def test_resource_rotator_basic_round_robin():
    rotator = ResourceRotator(["A", "B", "C"], cooldown_s=5)
    assert rotator.total_configured() == 3

    # Proactive round-robin checking
    item1, idx1 = rotator.get_next_available()
    assert item1 == "A"
    assert idx1 == 0

    item2, idx2 = rotator.get_next_available()
    assert item2 == "B"
    assert idx2 == 1

    item3, idx3 = rotator.get_next_available()
    assert item3 == "C"
    assert idx3 == 2

    # Wrap around
    item4, idx4 = rotator.get_next_available()
    assert item4 == "A"
    assert idx4 == 0


def test_resource_rotator_cooldown_and_failover():
    rotator = ResourceRotator(["A", "B", "C"], cooldown_s=10)

    # Put 'B' on cooldown
    rotator.record_failure(1)

    item, idx = rotator.get_next_available()  # A is first
    assert item == "A"
    assert idx == 0
    
    item, idx = rotator.get_next_available()  # skips B, returns C
    assert item == "C"
    assert idx == 2

    rotator.record_failure(0)  # A goes on cooldown
    rotator.record_failure(2)  # C goes on cooldown

    item, idx = rotator.get_next_available()  # all on cooldown
    assert item is None
    assert idx is None


def test_resource_rotator_success_clears_cooldown():
    rotator = ResourceRotator(["A"], cooldown_s=10)
    rotator.record_failure(0)
    
    item, idx = rotator.get_next_available()
    assert item is None

    rotator.record_success(0)
    item, idx = rotator.get_next_available()
    assert item == "A"


@patch("requests.Session.post")
def test_ollama_cloud_provider_failover(mock_post):
    # Setup mock responses: first key fails with 429, second key succeeds
    resp_fail = MagicMock()
    resp_fail.status_code = 429
    
    resp_ok = MagicMock()
    resp_ok.status_code = 200
    resp_ok.json.return_value = {
        "choices": [{"message": {"content": '{"action": "LONG"}'}}]
    }
    
    mock_post.side_effect = [resp_fail, resp_ok]

    provider = OllamaCloudProvider(
        host="https://api.ollama.com",
        model="qwen3.5:cloud",
        api_key="key1,key2",
        cooldown_s=10,
        cb_threshold=2
    )

    assert provider.health() is True
    
    res = provider.chat("sys", "user", 10)
    assert res == '{"action": "LONG"}'
    
    # Assert that key1 index 0 failed and went on cooldown
    assert provider.rotator.cooldown_until[0] > 0.0
    # key2 index 1 succeeded and is not on cooldown
    assert provider.rotator.cooldown_until[1] == 0.0
    # Circuit breaker should not be tripped
    assert provider.circuit_breaker.is_open is False


@patch("requests.Session.post")
def test_ollama_cloud_provider_circuit_breaker_trip(mock_post):
    resp_fail = MagicMock()
    resp_fail.status_code = 500
    resp_fail.raise_for_status.side_effect = requests.HTTPError("Error")
    mock_post.return_value = resp_fail

    provider = OllamaCloudProvider(
        host="https://api.ollama.com",
        model="qwen3.5:cloud",
        api_key="key1",
        cooldown_s=10,
        cb_threshold=2,
        cb_recovery_s=5
    )

    # First fail
    res = provider.chat("sys", "user", 10)
    assert res is None
    assert provider.circuit_breaker.is_open is False

    # Second fail (exceed threshold 2)
    res = provider.chat("sys", "user", 10)
    assert res is None
    assert provider.circuit_breaker.is_open is True
    assert provider.health() is False

    # Subsequent call rejected immediately without calling requests.Session.post
    mock_post.reset_mock()
    res = provider.chat("sys", "user", 10)
    assert res is None
    mock_post.assert_not_called()


@patch("requests.Session.post")
def test_ollama_local_provider_failover(mock_post):
    resp_fail = Exception("Connection Refused")
    
    resp_ok = MagicMock()
    resp_ok.status_code = 200
    resp_ok.json.return_value = {
        "message": {"content": '{"action": "SHORT"}'}
    }
    
    mock_post.side_effect = [resp_fail, resp_ok]

    provider = OllamaLocalProvider(
        host="http://localhost:11434,http://192.168.1.50:11434",
        model="qwen3.5:4b",
        cooldown_s=10,
        cb_threshold=2
    )

    res = provider.chat("sys", "user", 10)
    assert res == '{"action": "SHORT"}'
    
    # First host failed and went on cooldown
    assert provider.rotator.cooldown_until[0] > 0.0
    # Second host succeeded
    assert provider.rotator.cooldown_until[1] == 0.0


@patch("requests.Session.get")
def test_ollama_local_provider_health_check(mock_get):
    resp_ok = MagicMock()
    resp_ok.status_code = 200
    mock_get.return_value = resp_ok

    provider = OllamaLocalProvider(
        host="http://localhost:11434,http://192.168.1.50:11434",
        model="qwen3.5:4b"
    )

    assert provider.health() is True
    assert mock_get.call_count == 1

    # First host fails, but second host responds
    mock_get.reset_mock()
    resp_fail = MagicMock()
    resp_fail.status_code = 500
    mock_get.side_effect = [resp_fail, resp_ok]
    
    assert provider.health() is True
    # The first host went on cooldown
    assert provider.rotator.cooldown_until[0] > 0.0
