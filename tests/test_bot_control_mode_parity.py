from pathlib import Path


DESKTOP_ONLY_PATHS = [
    "app.py",
    "webview_app.py",
    "pages",
    "dialogs",
    "components",
    "config",
    "web",
    "build.spec",
    "build_macos.spec",
    "build_web.spec",
    "requirements_web.txt",
]


def test_desktop_gui_artifacts_are_removed_after_bot_control_mode_parity():
    repo_root = Path(__file__).resolve().parents[1]

    assert [path for path in DESKTOP_ONLY_PATHS if (repo_root / path).exists()] == []


def test_runtime_dependencies_do_not_include_desktop_gui_packages():
    requirements = Path("requirements.txt").read_text().lower()

    assert "customtkinter" not in requirements
    assert "pywebview" not in requirements


def test_readme_points_to_bot_control_mode_instead_of_desktop_startup():
    readme = Path("README.md").read_text().lower()

    assert "bot control mode" in readme
    assert "uvicorn bot_app.main:create_app" in readme
    assert "python app.py" not in readme
    assert "pyinstaller" not in readme
