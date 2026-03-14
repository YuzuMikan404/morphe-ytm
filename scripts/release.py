#!/usr/bin/env python3
"""
MEGA公開フォルダ → GitHub Release 自動リリーサー (rclone使用)
"""

import os, re, json, subprocess, tempfile, hashlib, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

# ── ★ここだけリポジトリごとに変える ──────────────────────────
APK_PATTERN = r"YouTube-Music-[\d.]+-Morphe\.apk"
APP_LABEL   = "Morphe YouTube Music"
# ────────────────────────────────────────────────────────────

REPO        = os.environ["REPO"]
GH_TOKEN    = os.environ["GH_TOKEN"]
MEGA_REMOTE = "mega:"   # ルート直下

STATE_PATH = Path(".release_state.json")
JST        = ZoneInfo("Asia/Tokyo")


# ── MEGA (rclone) ────────────────────────────────────────────
def list_mega() -> list[tuple[str, int]]:
    r = subprocess.run(
        ["rclone", "lsjson", MEGA_REMOTE],
        capture_output=True, text=True, check=True
    )
    files = []
    for entry in json.loads(r.stdout):
        if entry.get("IsDir"):
            continue
        name = entry["Name"]
        try:
            ts = int(datetime.fromisoformat(entry.get("ModTime", "")).timestamp())
        except Exception:
            ts = 0
        files.append((name, ts))
    return files

def download_mega(filename: str, dest: str) -> str:
    r = subprocess.run(
        ["rclone", "copy", f"{MEGA_REMOTE}{filename}", dest],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError(f"rclone copy failed:\n{r.stderr}")
    return os.path.join(dest, filename)


# ── APK 情報 ────────────────────────────────────────────────
def apk_version(path: str) -> str:
    r = subprocess.run(
        ["aapt", "dump", "badging", path],
        capture_output=True, text=True, check=True
    )
    m = re.search(r"versionName='([^']+)'", r.stdout)
    return m.group(1) if m else "0.0.0"

def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── GitHub API ──────────────────────────────────────────────
def gh(method: str, url: str, data=None, binary=None, ct="application/json"):
    if not url.startswith("http"):
        url = f"https://api.github.com{url}"
    body = json.dumps(data).encode() if data else binary
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"token {GH_TOKEN}",
        "Accept":        "application/vnd.github+json",
        "Content-Type":  ct,
    })
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def existing_tags() -> set[str]:
    try:
        releases = gh("GET", f"/repos/{REPO}/releases?per_page=100")
        return {r["tag_name"] for r in releases}
    except Exception as e:
        print(f"  ⚠️ タグ一覧取得失敗（継続）: {e}")
        return set()

def create_release(tag: str, title: str, body: str, apk_path: str):
    release = gh("POST", f"/repos/{REPO}/releases", {
        "tag_name":   tag,
        "name":       title,
        "body":       body,
        "draft":      False,
        "prerelease": False,
    })
    upload_url = release["upload_url"].split("{")[0]
    filename   = Path(apk_path).name
    apk_bytes  = Path(apk_path).read_bytes()
    asset = gh(
        "POST",
        f"{upload_url}?name={urllib.parse.quote(filename)}",
        binary=apk_bytes,
        ct="application/vnd.android.package-archive",
    )
    print(f"  📎 {asset['browser_download_url']}")


# ── メイン ──────────────────────────────────────────────────
def main():
    state   = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    pattern = re.compile(APK_PATTERN, re.IGNORECASE)
    tags    = existing_tags()

    print("📡 MEGAファイル一覧取得中...")
    all_files = list_mega()
    print(f"  {len(all_files)} ファイル検出: {[f for f, _ in all_files]}")

    matched = sorted(
        [(f, ts) for f, ts in all_files if pattern.search(f)],
        key=lambda x: x[1], reverse=True,
    )
    if not matched:
        print(f"⚠️  マッチするAPKなし (pattern: {APK_PATTERN})")
        return

    latest_name, latest_ts = matched[0]
    print(f"📦 対象APK: {latest_name}  (更新: {datetime.fromtimestamp(latest_ts, JST).strftime('%Y-%m-%d %H:%M JST')})")

    last_ts = state.get("file_ts", 0)
    if latest_ts <= last_ts:
        last_dt = datetime.fromtimestamp(last_ts, JST).strftime("%Y-%m-%d %H:%M JST")
        print(f"✅ 更新なし (前回: {last_dt})")
        return

    with tempfile.TemporaryDirectory() as tmp:
        print("⬇️  ダウンロード中...")
        apk = download_mega(latest_name, tmp)

        version_name = apk_version(apk)
        now_jst      = datetime.now(JST)
        tag          = now_jst.strftime("v%Y%m%d-%H%M")
        print(f"🏷️  {tag}  ({version_name})")

        while tag in tags:
            now_jst = now_jst.replace(minute=now_jst.minute + 1)
            tag = now_jst.strftime("v%Y%m%d-%H%M")

        mega_updated = datetime.fromtimestamp(latest_ts, JST).strftime("%Y-%m-%d %H:%M JST")
        checksum     = sha256(apk)
        body = "\n".join([
            f"## {APP_LABEL} {version_name}",
            "",
            "| 項目 | 値 |",
            "|---|---|",
            f"| versionName | `{version_name}` |",
            f"| ファイル名 | `{latest_name}` |",
            f"| MEGA更新日時 | `{mega_updated}` |",
            f"| SHA-256 | `{checksum}` |",
            f"| リリース日時 | {now_jst.strftime('%Y-%m-%d %H:%M JST')} |",
        ])

        print(f"🚀 リリース作成中: {tag}")
        create_release(tag, f"{APP_LABEL} {version_name}", body, apk)

        state = {
            "version_name": version_name,
            "file_ts":      latest_ts,
            "tag":          tag,
            "filename":     latest_name,
            "released_at":  now_jst.isoformat(),
        }
        STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        print("🎉 完了!")

if __name__ == "__main__":
    main()
