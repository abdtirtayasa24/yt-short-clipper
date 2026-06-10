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


def preauthorize_youtube(client_secret_path: Path, output_path: Path) -> None:
    if not client_secret_path.exists():
        raise FileNotFoundError(f"YouTube client secret file not found: {client_secret_path}")

    import youtube_uploader
    from youtube_uploader import YouTubeUploader

    output_path.parent.mkdir(parents=True, exist_ok=True)
    youtube_uploader.CLIENT_SECRET_FILE = client_secret_path
    youtube_uploader.CREDENTIALS_FILE = output_path

    uploader = YouTubeUploader(status_callback=print)
    uploader.authenticate()
    if not output_path.exists():
        raise RuntimeError(f"YouTube authentication finished but did not write {output_path}")
    print(f"✓ YouTube credentials written to {output_path}")


def preauthorize_tiktok(
    client_key: str,
    client_secret: str,
    mode: str,
    output_path: Path,
    working_config_path: Path,
) -> None:
    if not client_key or not client_secret:
        raise ValueError("TikTok client key and client secret are required")

    from tiktok_uploader import TikTokUploader

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
    parser.add_argument("--skip-youtube", action="store_true", help="Do not run YouTube OAuth")
    parser.add_argument("--tiktok-client-key", default="", help="TikTok app client key")
    parser.add_argument("--tiktok-client-secret", default="", help="TikTok app client secret")
    parser.add_argument("--tiktok-mode", choices=["sandbox", "production"], default="sandbox", help="TikTok API mode")
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
        preauthorize_youtube(args.youtube_client_secret, youtube_output)
    else:
        print("- Skipped YouTube preauthorization")

    if not args.skip_tiktok:
        preauthorize_tiktok(
            args.tiktok_client_key,
            args.tiktok_client_secret,
            args.tiktok_mode,
            tiktok_output,
            tiktok_working_config,
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
