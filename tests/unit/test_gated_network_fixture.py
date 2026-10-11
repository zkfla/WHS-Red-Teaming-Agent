"""The Docker proof has versioned, bounded inputs independent of data/ files."""

import socket
from unittest.mock import MagicMock

import pytest

from tests.integration.fixtures import gated_network as fixture
from tests.integration.test_gated_window_docker import NETWORK_FIXTURE


def test_network_fixture_is_versioned_and_available():
    assert NETWORK_FIXTURE.is_file()
    assert NETWORK_FIXTURE.parent.name == "fixtures"
    compile(NETWORK_FIXTURE.read_text(encoding="utf-8"), str(NETWORK_FIXTURE), "exec")


@pytest.mark.parametrize("argv", [
    ["tcp", "example.com", "80", "1", "32", "out.log"],
    ["tcp", "127.0.0.1", "0", "1", "32", "out.log"],
    ["udp", "127.0.0.1", "80", "101", "32", "out.log"],
    ["tcp", "127.0.0.1", "80", "1", "65537", "out.log"],
])
def test_probe_refuses_names_and_unbounded_requests(argv):
    with pytest.raises(SystemExit):
        fixture.parser().parse_args(argv)


@pytest.mark.parametrize("response,expected", [(b"ok", " OK"), (b"", " FAIL")])
def test_udp_requires_observed_reply_not_only_successful_send(
    tmp_path, monkeypatch, response, expected,
):
    log = tmp_path / "probe.txt"
    arguments = fixture.parser().parse_args(["udp", "127.0.0.1", "80", "1", "32", str(log)])
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.recv.return_value = response
    factory = MagicMock(return_value=connection)
    monkeypatch.setattr(fixture.socket, "socket", factory)
    monkeypatch.setattr(fixture.time, "sleep", lambda _: None)
    fixture.probe(arguments)
    factory.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
    connection.recv.assert_called_once()
    text = log.read_text(encoding="utf-8")
    assert expected in text
    assert (" OK" in text) == bool(response)
