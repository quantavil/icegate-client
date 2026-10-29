from icegate_client.shared import config

_default_env_file = config.default_env_file


def test_source_checkout_uses_project_env(tmp_path, monkeypatch):
    root = tmp_path / "icegate-client"
    module = root / "src" / "icegate_client" / "shared" / "config.py"
    module.parent.mkdir(parents=True)
    (root / "pyproject.toml").write_text("")
    monkeypatch.setattr(config, "__file__", str(module))
    assert _default_env_file() == root / ".env"


def test_installed_package_uses_current_directory(tmp_path, monkeypatch):
    module = tmp_path / "site-packages" / "icegate_client" / "shared" / "config.py"
    monkeypatch.setattr(config, "__file__", str(module))
    monkeypatch.chdir(tmp_path)
    assert _default_env_file() == tmp_path / ".env"
