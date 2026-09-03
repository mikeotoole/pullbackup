"""The demo instance must publish on loopback only.

``scripts/seed-demo.py`` stands up a throwaway instance whose credentials are
printed on stdout and written into the docs. That is only acceptable while the
instance is unreachable from anything but the machine running it. A Docker
publish spec with no host IP (``-p 18080:8000``) binds ``0.0.0.0`` *and* the
IPv6 wildcard, which puts those known credentials on every interface the host
has, including the LAN.

This test does not read the script's prose or grep its source: it calls ``up()``
with the docker invocation intercepted and asserts on the exact argv that would
have been handed to ``docker run``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "seed-demo.py"

# Every spelling that binds something other than loopback. "" is the one the
# regression actually shipped: `-p 18080:8000` has an empty host-IP field.
WILDCARD_HOST_IPS = {"", "*", "0.0.0.0", "::", "[::]", "::0", "0:0:0:0:0:0:0:0"}


def _load_module():
    spec = importlib.util.spec_from_file_location("seed_demo", SCRIPT)
    assert spec and spec.loader, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_demo"] = module
    spec.loader.exec_module(module)
    return module


def _split_publish_spec(spec: str) -> tuple[str, str, str]:
    """Split a docker -p value into (host_ip, host_port, container_port).

    Docker accepts ``ip:hostPort:containerPort``, ``hostPort:containerPort`` and
    bare ``containerPort``; the last two both mean "all interfaces", which is
    represented here as an empty host IP.
    """
    if spec.startswith("["):  # bracketed IPv6 literal
        close = spec.index("]")
        return spec[1:close], *spec[close + 2:].split(":", 1)  # type: ignore[return-value]
    parts = spec.split(":")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    return "", "", parts[0]


@pytest.fixture()
def captured_run_argv(tmp_path, monkeypatch):
    module = _load_module()
    calls: list[tuple[str, ...]] = []

    def fake_sh(*args: str, check: bool = True, capture: bool = False):
        calls.append(args)
        return None

    monkeypatch.setattr(module, "SCRATCH", tmp_path / "scratch")
    monkeypatch.setattr(module, "sh", fake_sh)
    module.up()

    run_calls = [c for c in calls if c[:2] == ("docker", "run")]
    assert len(run_calls) == 1, f"expected exactly one docker run, got {run_calls}"
    return module, run_calls[0]


def test_demo_container_publishes_only_on_loopback(captured_run_argv):
    module, argv = captured_run_argv
    specs = [argv[i + 1] for i, a in enumerate(argv) if a in ("-p", "--publish")]
    assert specs, f"demo container publishes no port at all: {argv}"

    for spec in specs:
        host_ip, host_port, container_port = _split_publish_spec(spec)
        assert host_ip not in WILDCARD_HOST_IPS, (
            f"demo publish spec {spec!r} binds a wildcard address (host IP "
            f"{host_ip!r}). The demo prints working credentials on stdout, so it "
            f"must be reachable from the local machine only. Use "
            f"'127.0.0.1:{module.PORT}:8000'."
        )
        assert host_ip == "127.0.0.1", (
            f"demo publish spec {spec!r} binds {host_ip!r}, expected 127.0.0.1"
        )
        assert host_port == str(module.PORT), (
            f"demo publish spec {spec!r} does not publish the documented port "
            f"{module.PORT}"
        )
        assert container_port == "8000", (
            f"demo publish spec {spec!r} does not target the app port 8000"
        )


def test_demo_container_is_not_host_networked(captured_run_argv):
    """--network host would defeat the publish spec entirely."""
    _module, argv = captured_run_argv
    for i, a in enumerate(argv):
        if a in ("--network", "--net"):
            assert argv[i + 1] != "host", "demo container must not use host networking"
    assert "--network=host" not in argv and "--net=host" not in argv


def test_split_publish_spec_recognises_wildcard_forms():
    """Guard the helper itself: the shipped regression must be classified."""
    assert _split_publish_spec("18080:8000") == ("", "18080", "8000")
    assert _split_publish_spec("0.0.0.0:18080:8000") == ("0.0.0.0", "18080", "8000")
    assert _split_publish_spec("127.0.0.1:18080:8000") == ("127.0.0.1", "18080", "8000")
    assert _split_publish_spec("[::]:18080:8000") == ("::", "18080", "8000")
    assert _split_publish_spec("8000") == ("", "", "8000")
