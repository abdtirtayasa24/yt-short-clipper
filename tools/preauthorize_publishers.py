#!/usr/bin/env python3
"""Run one-time local Publishing preauthorization for Bot Control Mode.

This script is intended to be run on a local machine with a browser. It writes
credential/session files that can be copied to the VPS paths used by `/auth`:

    credentials/youtube.json
    credentials/tiktok.session
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from google_auth_oauthlib.flow import InstalledAppFlow

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


class JsonConfigStore:
    """Minimal config interface required by TikTokUploader."""

    def __init__(self, path: Path, initial_config: dict[str, Any] | None = None):
        self.path = path
        if path.exists():
            self.config = json.loads(path.read_text())
        else:
            self.config = initial_config or {}
            self._save()

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.config[key] = value
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.config, indent=2))


def _load_youtube_client_config(client_secret_path: Path, oauth_port: int) -> dict[str, Any]:
    if not client_secret_path.exists():
        raise FileNotFoundError(f"YouTube client secret file not found: {client_secret_path}")

    config = json.loads(client_secret_path.read_text())
    if "installed" in config:
        return config

    if "web" in config:
        redirect_uri = f"http://localhost:{oauth_port}/"
        configured_redirects = config["web"].get("redirect_uris", [])
        if redirect_uri not in configured_redirects:
            raise ValueError(
                "YouTube OAuth client_secret.json is a Web application client, but its redirect URIs do not include "
                f"{redirect_uri!r}. Add that exact URI in Google Cloud Console, or create an OAuth client of type "
                "Desktop app and download that client_secret.json instead."
            )
        return config

    raise ValueError(
        "Unsupported YouTube client_secret.json. Expected a Google OAuth client containing either "
        "an 'installed' Desktop app client or a 'web' client."
    )


def preauthorize_youtube(client_secret_path: Path, output_path: Path, oauth_port: int) -> None:
    client_config = _load_youtube_client_config(client_secret_path, oauth_port)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Opening YouTube OAuth browser flow on http://localhost:{oauth_port}/")
    flow = InstalledAppFlow.from_client_config(client_config, YOUTUBE_SCOPES)
    credentials = flow.run_local_server(
        port=oauth_port,
        prompt="consent",
        success_message="YouTube connected successfully. You can close this window.",
    )
    output_path.write_text(credentials.to_json())
    print(f"✓ YouTube credentials written to {output_path}")


def preauthorize_tiktok(
    client_key: str,
    client_secret: str,
    mode: str,
    output_path: Path,
    working_config_path: Path,
    redirect_uri: str,
) -> None:
    if not client_key or not client_secret:
        raise ValueError("TikTok client key and client secret are required")

    import tiktok_uploader
    from tiktok_uploader import TikTokUploader

    tiktok_uploader.REDIRECT_URI = redirect_uri

    config = JsonConfigStore(
        working_config_path,
        {
            "tiktok": {
                "client_key": client_key,
                "client_secret": client_secret,
                "mode": mode,
            }
        },
    )
    tiktok_config = config.get("tiktok", {})
    tiktok_config.update({"client_key": client_key, "client_secret": client_secret, "mode": mode})
    config.set("tiktok", tiktok_config)

    print(f"Opening TikTok OAuth browser flow with redirect URI: {tiktok_uploader.REDIRECT_URI}")
    uploader = TikTokUploader(config, status_callback=print)
    uploader.authenticate()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(config.get("tiktok", {}), indent=2))
    print(f"✓ TikTok session written to {output_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create YouTube/TikTok preauthorization files to copy to the Bot Control Mode VPS.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("credentials"), help="Directory for generated auth files")
    parser.add_argument("--youtube-client-secret", type=Path, default=Path("client_secret.json"), help="Google OAuth client_secret.json path")
    parser.add_argument("--youtube-oauth-port", type=int, default=8080, help="Localhost port for YouTube OAuth callback")
    parser.add_argument("--skip-youtube", action="store_true", help="Do not run YouTube OAuth")
    parser.add_argument("--tiktok-client-key", default="", help="TikTok app client key")
    parser.add_argument("--tiktok-client-secret", default="", help="TikTok app client secret")
    parser.add_argument("--tiktok-mode", choices=["sandbox", "production"], default="sandbox", help="TikTok API mode")
    parser.add_argument("--tiktok-redirect-uri", default="http://localhost:8080/callback", help="TikTok OAuth redirect URI; must exactly match TikTok Developer Console")
    parser.add_argument("--skip-tiktok", action="store_true", help="Do not run TikTok OAuth")
    parser.add_argument("--copy-env-example", action="store_true", help="Print .env paths for the generated files")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir
    youtube_output = output_dir / "youtube.json"
    tiktok_output = output_dir / "tiktok.session"
    tiktok_working_config = output_dir / ".tiktok-preauth-config.json"

    if not args.skip_youtube:
        preauthorize_youtube(args.youtube_client_secret, youtube_output, args.youtube_oauth_port)
    else:
        print("- Skipped YouTube preauthorization")

    if not args.skip_tiktok:
        preauthorize_tiktok(
            args.tiktok_client_key,
            args.tiktok_client_secret,
            args.tiktok_mode,
            tiktok_output,
            tiktok_working_config,
            args.tiktok_redirect_uri,
        )
    else:
        print("- Skipped TikTok preauthorization")

    print("\nCopy these files to the same paths on the VPS, or set env vars to match:")
    print(f"YOUTUBE_CREDENTIALS_PATH={youtube_output}")
    print(f"TIKTOK_SESSION_PATH={tiktok_output}")

    if args.copy_env_example:
        env_path = output_dir / "publishing.env.example"
        env_path.write_text(
            f"YOUTUBE_CREDENTIALS_PATH={youtube_output}\n"
            f"TIKTOK_SESSION_PATH={tiktok_output}\n"
        )
        print(f"Wrote {env_path}")


if __name__ == "__main__":
    main()
