#!/usr/bin/env uv run

import json
import sys
import re
import argparse
import subprocess
import os
import shlex
import threading
from pathlib import Path

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        print("Error: requires Python 3.11+ or 'tomli' package (pip install tomli)")
        sys.exit(1)

# iterfzf is still the best for TUI loop
try:
    from iterfzf import iterfzf
except ImportError:
    print("Error: missing dependency 'iterfzf'. Run 'uv sync'.")
    sys.exit(1)

# --------------------------
# Constants
# --------------------------
BASE_PLAYLISTS_DIR = Path("playlists")
BASE_PLAYLISTS_DIR.mkdir(exist_ok=True)

# --------------------------
# Load config
# --------------------------
CONFIG_PATH = Path(__file__).parent / "config.toml"
if not CONFIG_PATH.exists():
    print(f"Error: config.toml not found at {CONFIG_PATH}")
    sys.exit(1)

with open(CONFIG_PATH, "rb") as f:
    config = tomllib.load(f)

USE_COOKIES    = config.get("use_cookies", False)
BROWSER        = config.get("browser", "firefox")
REMOTE_COMPONENTS = config.get("remote_components", ["ejs:github"])
PRESETS        = config.get("presets", [])
VIDEO_PLAYER   = config.get("video_player", "mpv")

# --------------------------
# Global state / Helpers
# --------------------------
def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name).strip()

def get_ydl_opts(extra_opts=None):
    opts = {
        "quiet": True,
        "ignoreerrors": True,
        "extractor_args": {"youtubetab": {"skip": ["authcheck"]}},
    }
    if REMOTE_COMPONENTS:
        opts["remote_components"] = REMOTE_COMPONENTS
    if USE_COOKIES:
        opts["cookiesfrombrowser"] = (BROWSER,)
    if extra_opts:
        opts.update(extra_opts)
    return opts

def resolve_preset(duration):
    if duration is None:
        return None, None, "meta"
    for preset in PRESETS:
        max_dur = preset.get("max_duration")
        max_h   = preset.get("max_height", 720)
        if max_dur is None or duration <= max_dur:
            fmt = (
                f"bestvideo[height<={max_h}]+bestaudio"
                f"/bestvideo[height<={max_h}]"
                f"/best[height<={max_h}]"
                f"/best"
            )
            return fmt, "mp4", "video"
    return None, None, "meta"

class PlaylistManager:
    def __init__(self, url=None, metadata_fetch=True, playlist_dir=None):
        self.url = url
        self.playlist_flat = None
        self.playlist_dir = playlist_dir
        self.thumbnails_dir = None
        self.removed_json = None
        self.removed_videos = []
        self.removed_ids = set()
        
        if playlist_dir:
            self.set_dirs(playlist_dir)
        elif url and metadata_fetch:
            self._load_metadata()

    def _load_metadata(self):
        from yt_dlp import YoutubeDL
        try:
            with YoutubeDL(get_ydl_opts({"extract_flat": True, "skip_download": True})) as ydl:
                self.playlist_flat = ydl.extract_info(self.url, download=False)
        except Exception as e:
            print(f"Failed to fetch playlist metadata: {e}", file=sys.stderr)
            sys.exit(1)

        if not self.playlist_flat:
            print("Failed to fetch playlist metadata: No data returned.", file=sys.stderr)
            sys.exit(1)

        title = safe_name(self.playlist_flat.get("title", "playlist"))
        self.playlist_dir = BASE_PLAYLISTS_DIR / title
        self.playlist_dir.mkdir(parents=True, exist_ok=True)
        self.set_dirs(self.playlist_dir)

    def set_dirs(self, playlist_dir):
        self.playlist_dir = Path(playlist_dir)
        self.thumbnails_dir = self.playlist_dir / "thumbnails"
        self.thumbnails_dir.mkdir(parents=True, exist_ok=True)
        self.removed_json = self.playlist_dir / "removed_videos.json"
        
        if self.removed_json.exists():
            try:
                self.removed_videos = json.loads(self.removed_json.read_text())
                self.removed_ids = {v["id"] for v in self.removed_videos}
            except Exception: pass

    def mark_removed(self, video_id, title, url, reason):
        if video_id not in self.removed_ids:
            self.removed_videos.append({"id": video_id, "title": title, "url": url, "reason": reason})
            self.removed_ids.add(video_id)
            try:
                self.removed_json.write_text(json.dumps(self.removed_videos, indent=2))
            except Exception: pass

    def get_existing_ids(self):
        ids = set()
        if not self.playlist_dir: return ids
        for f in self.playlist_dir.iterdir():
            if f.suffix in (".mp4", ".webm", ".mkv") and "[" in f.stem and "]" in f.stem:
                ids.add(f.stem.split("[")[-1].rstrip("]"))
        return ids

    def list_videos(self):
        existing = self.get_existing_ids()
        for entry in self.playlist_flat.get("entries", []):
            if not entry: continue
            vid = entry.get("id")
            title = entry.get("title", "Unknown Title")
            status = "[DONE]" if vid in existing else "[NEW]"
            yield f"{vid}|{status}|{title}"

    def list_downloaded_files(self):
        """List local files for Watch Mode."""
        if not self.playlist_dir: return
        for f in self.playlist_dir.iterdir():
            if f.suffix in (".mp4", ".webm", ".mkv", ".m4a") and "[" in f.stem and "]" in f.stem:
                vid = f.stem.split("[")[-1].rstrip("]")
                yield f"{vid}|{f.name}|{self.thumbnails_dir.absolute()}"

    def get_thumbnail_path(self, video_id, fetch_if_missing=False):
        if not self.thumbnails_dir: return None
        # Fast disk check
        for f in self.thumbnails_dir.iterdir():
            if f"[{video_id}]" in f.name:
                return f.absolute()
        
        if fetch_if_missing:
            from yt_dlp import YoutubeDL
            url = f"https://www.youtube.com/watch?v={video_id}"
            thumb_opts = get_ydl_opts({
                "outtmpl": str(self.thumbnails_dir / "%(title)s [%(id)s].%(ext)s"),
                "skip_download": True,
                "writethumbnail": True,
            })
            try:
                with YoutubeDL(thumb_opts) as ydl:
                    ydl.download([url])
                for f in self.thumbnails_dir.iterdir():
                    if f"[{video_id}]" in f.name:
                        return f.absolute()
            except Exception: pass
        return None

    def prefetch_thumbnails(self):
        """Background thread to download missing thumbnails."""
        if not self.playlist_flat: return
        for entry in self.playlist_flat.get("entries", []):
            if not entry: continue
            vid = entry.get("id")
            if vid:
                self.get_thumbnail_path(vid, fetch_if_missing=True)

    def download_videos(self, ids_to_download=None):
        from yt_dlp import YoutubeDL
        existing = self.get_existing_ids()
        processed = 0
        total = len(ids_to_download) if ids_to_download else len(self.playlist_flat.get("entries", []))
        
        print(f"--- Starting Download Process ({'Selected Only' if ids_to_download else 'New Videos'}) ---")
        
        for entry in self.playlist_flat.get("entries", []):
            if not entry: continue
            vid = entry.get("id")
            if ids_to_download and vid not in ids_to_download:
                continue

            processed += 1
            title = entry.get("title", "Unknown Title")
            url = entry.get("url") or entry.get("webpage_url")

            if vid in existing:
                print(f"[{processed}/{total}] Already on disk: {title} ({vid}) - skipping")
                continue
            
            print(f"[{processed}/{total}] Processing: {title} ({vid})")
            
            if not vid or not url:
                self.mark_removed(vid or "unknown", title, "N/A", "Video unavailable")
                continue

            try:
                with YoutubeDL(get_ydl_opts()) as ydl:
                    info = ydl.extract_info(url, download=False)
                if not info:
                    self.mark_removed(vid, title, url, "No info")
                    continue
                
                fmt, cont, action = resolve_preset(info.get("duration"))
                
                if action == "video":
                    opts = get_ydl_opts({
                        "outtmpl": str(self.playlist_dir / "%(title)s [%(id)s].%(ext)s"),
                        "format": fmt,
                        "merge_output_format": cont,
                    })
                else:
                    opts = get_ydl_opts({
                        "outtmpl": str(self.playlist_dir / "%(title)s [%(id)s].%(ext)s"),
                        "skip_download": True,
                        "writeinfojson": True,
                    })
                with YoutubeDL(opts) as ydl:
                    ydl.download([url])
                
                self.get_thumbnail_path(vid, fetch_if_missing=True)

            except Exception as e:
                self.mark_removed(vid, title, url, str(e))
        
        print("--- Process Complete ---")

def run_playlist_selector():
    playlists = sorted([d.name for d in BASE_PLAYLISTS_DIR.iterdir() if d.is_dir()])
    if not playlists:
        print("No playback history found in 'playlists/'. Download something first!")
        sys.exit(0)
    
    header = "Select Playlists to browse (TAB to select multiple, ENTER to confirm)"
    selected = iterfzf(playlists, multi=True, __extra__=["--reverse", "--header", header])
    if not selected:
        print("No playlists selected.")
        sys.exit(0)
    return selected

def run_tui(url=None, mode="download", playlist_dirs=None):
    script_path = shlex.quote(str(Path(__file__).absolute()))
    
    if mode == "download":
        manager = PlaylistManager(url, metadata_fetch=True)
        header = "ENTER: Download Selected | CTRL-A: Download All New | CTRL-R: Refresh list | ESC: Quit"
        items = list(manager.list_videos())
        # Start background prefetching
        prefetch_thread = threading.Thread(target=manager.prefetch_thumbnails, daemon=True)
        prefetch_thread.start()
        q_thumb_dir = shlex.quote(str(manager.thumbnails_dir.absolute()))
        q_url = shlex.quote(url)
    else: # watch mode
        items = []
        if playlist_dirs:
            for d_name in playlist_dirs:
                p_dir = BASE_PLAYLISTS_DIR / d_name
                m = PlaylistManager(playlist_dir=p_dir)
                items.extend(list(m.list_downloaded_files()))
        elif url:
            manager = PlaylistManager(url, metadata_fetch=True)
            items = list(manager.list_downloaded_files())
        
        if not items:
            print("No downloaded videos found.")
            return
        
        header = "ENTER: Play Selected | CTRL-A: Play All | ESC: Quit"
        # For multi-playlist watch mode, we'll use a dynamic thumb dir logic in the shell preview
        q_thumb_dir = "DYNAMIC" 
        q_url = shlex.quote(url) if url else "OFFLINE"

    # Optimized shell preview command
    # Added logic to handle dynamic thumbnail directories for multi-playlist mode
    preview_cmd = (
        "printf '\\033_Ga=d,d=A\\033\\\\'; "
        "vid_id=$(echo {} | cut -d'|' -f1); "
        "if [ \"$vid_id\" = \"\" ]; then exit; fi; "
        "thumb_dir=$(echo {} | cut -d'|' -f3); "
        f"if [ {q_thumb_dir} != \"DYNAMIC\" ]; then thumb_dir={q_thumb_dir}; fi; "
        "thumb=$(find \"$thumb_dir\" -name \"*[$vid_id]*\" | head -n 1); "
        "if [ -f \"$thumb\" ]; then "
        "kitty +kitten icat --silent --stdin=no --transfer-mode=file "
        "--place=\"${FZF_PREVIEW_COLUMNS}x${FZF_PREVIEW_LINES}@0x0\" \"$thumb\"; "
        "else "
        f"uv run {script_path} {q_url} --preview {{}} --thumb-dir \"$thumb_dir\"; "
        "fi"
    )
    
    list_cmd = f"uv run {script_path} {q_url} --list"
    
    extra_flags = [
        "--reverse",
        "--header", header,
        "--preview", preview_cmd,
        "--preview-window", "right:60%:wrap",
        "--bind", f"ctrl-a:change-query([NEW])+select-all+accept",
        "--bind", f"ctrl-r:reload({list_cmd})"
    ]

    try:
        selected = iterfzf(items, multi=True, ansi=True, __extra__=extra_flags)
    except (KeyboardInterrupt, Exception):
        print("\nQuitting...")
        sys.exit(0)
    
    if selected is None:
        print("\nQuitting...")
        sys.exit(0)

    if mode == "download":
        ids = [line.split("|")[0] for line in selected if "|" in line]
        if ids:
            manager.download_videos(set(ids))
        else:
            print("No videos selected.")
    else: # watch mode
        # Extract filename and its corresponding directory from the combined list
        paths = []
        for line in selected:
            if "|" in line:
                parts = line.split("|")
                vid_id = parts[0]
                filename = parts[1]
                # If we have the 3rd column (thumb_dir), it's the parent of the actual file
                if len(parts) >= 3:
                    p_dir = Path(parts[2]).parent
                    paths.append(str((p_dir / filename).absolute()))
        
        if paths:
            print(f"Playing {len(paths)} videos in {VIDEO_PLAYER}...")
            try:
                subprocess.run([VIDEO_PLAYER] + paths, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                print(f"Error: Video player '{VIDEO_PLAYER}' not found. Please update config.toml.")
        else:
            print("No videos selected.")

def main():
    parser = argparse.ArgumentParser(description="Save YouTube playlists with TUI support")
    parser.add_argument("url", nargs="?", help="Playlist URL (optional for --watch)")
    parser.add_argument("--auto", action="store_true", help="Download all new without TUI")
    parser.add_argument("--watch", action="store_true", help="Watch downloaded videos offline")
    parser.add_argument("--list", action="store_true", help="List videos (ID|STATUS|TITLE)")
    parser.add_argument("--preview", help="Preview video ID")
    parser.add_argument("--thumb-dir", help="Explicit thumbnail directory for instant preview")
    parser.add_argument("--download-ids", help="Comma-separated IDs to download")
    parser.add_argument("--download-new", action="store_true", help="Download all new videos")

    args = parser.parse_args()
    
    # Pre-checks for Watch Mode without URL
    if args.watch and not args.url and not args.preview:
        selected_playlists = run_playlist_selector()
        run_tui(mode="watch", playlist_dirs=selected_playlists)
        return

    if not args.url and not args.preview:
        parser.print_help()
        sys.exit(0)

    metadata_fetch = not bool(args.preview) and args.url != "OFFLINE"
    
    if args.url == "OFFLINE":
        # Special case for previewing without a live manager
        manager = PlaylistManager(metadata_fetch=False)
    else:
        manager = PlaylistManager(args.url, metadata_fetch=metadata_fetch)
    
    if args.thumb_dir:
        manager.set_dirs(Path(args.thumb_dir).parent)

    if args.preview:
        vid = args.preview.split("|")[0]
        # In multi-playlist mode, the previewer passes the thumb-dir in the shell command
        path = manager.get_thumbnail_path(vid, fetch_if_missing=(args.url != "OFFLINE"))
        if path and path.exists():
            sys.stdout.write("\033_Ga=d,d=A\033\\")
            sys.stdout.flush()
            cols = os.environ.get('FZF_PREVIEW_COLUMNS', '80')
            lines = os.environ.get('FZF_PREVIEW_LINES', '24')
            try:
                subprocess.run([
                    "kitty", "+kitten", "icat",
                    "--silent",
                    "--stdin=no",
                    "--transfer-mode=file",
                    f"--place={cols}x{lines}@0x0",
                    str(path)
                ], stderr=subprocess.DEVNULL, check=False)
            except Exception: pass
        else:
            print(f"No thumbnail for {vid}")
    elif args.list:
        if args.watch:
            for line in manager.list_downloaded_files():
                print(line)
        else:
            for line in manager.list_videos():
                print(line)
    elif args.download_ids:
        ids = set(args.download_ids.split(","))
        manager.download_videos(ids)
    elif args.download_new:
        manager.download_videos()
    elif args.auto:
        manager.download_videos()
    else:
        run_tui(args.url, mode="watch" if args.watch else "download")

if __name__ == "__main__":
    main()
