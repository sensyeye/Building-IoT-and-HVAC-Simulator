"""Smoke tests verifying the package can be imported."""

import simulator
from simulator import (
    config_loader,
    integrations,
    main,
    models,
    outputs,
    sensors,
    utils,
    validators,
)


def test_package_has_version():
    assert isinstance(simulator.__version__, str)


def test_submodules_importable():
    # Just verify modules import cleanly.
    assert config_loader is not None
    assert main is not None
    assert models is not None
    assert sensors is not None
    assert outputs is not None
    assert validators is not None
    assert integrations is not None
    assert utils is not None


def test_main_cli_runs(capsys):
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    rc = main.main(
        ["dry-run-config", "--config", str(repo_root / "configs" / "demo_office.yaml")]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "Effective mode" in captured.out
