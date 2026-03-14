#!/usr/bin/env python3
"""
MEGA公開フォルダ → GitHub Release 自動リリーサー
"""

import os, re, json, subprocess, tempfile, hashlib, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

# ── ★ここだけリポジトリごとに変える ──────────────────────────
APK_PATTERN = "YouTube-Music-[\d.]+-Morphe\.apk"
APP_LABEL   = "Morphe YouTube Music"
# ────────────────────────────────────────────────────────────

REPO      = os.environ["REPO"]
GH_TOKEN  = os.environ["GH_TOKEN"]
MEGA_LINK = "https://mega.nz/folder/A2RE0TwD#JIiV5Sy82y6bH_sMeRNH0Q"

STATE_PATH = Path(".release_state.json")
JST        = ZoneInfo("Asia/Tokyo")


# ── MEGA ────────────────────────────────────────────────────
def list_mega() -> list[str]:
    r = subprocess.run(
        ["megals", "--reload", MEGA_LINK],
        capture_output=True, text=True, check=True
    )
    return [line.split("/")[-1].strip() for line in r.stdout.splitlines() if line.strip()]

def download_mega(filename: str, dest: str) -> str:
    subprocess.run(
        ["megaget", "--path", dest, f"{MEGA_LINK}/{filename}"],
        check=True
    )
    return os.path.join(dest, filename)


# ── APK 情報 ────────────────────────────────────────────────
def apk_info(path: str) -> tuple[str, int]:
    r = subprocess.run(
        ["aapt", "dump", "badging", path],
        capture_output=True, text=True, check=True
    )
    vname = re.search(r"versionName='([^']+)'", r.stdout)
    vcode = re.search(r"versionCode='([^']+)'", r.stdout)
    return (
        vname.group(1) if vname else "0.0.0",
        int(vcode.group(1)) if vcode else 0,
    )

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
    except Exception:
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

    print(f"📡 MEGAファイル一覧取得中...")
    files = list_mega()
    print(f"  {len(files)} ファイル検出")

    matched = sorted([f for f in files if pattern.search(f)])
    if not matched:
        print(f"⚠️  マッチするAPKなし (pattern: {APK_PATTERN})")
        print(f"   ファイル一覧: {files}")
        return

    latest = matched[-1]
    print(f"📦 対象APK: {latest}")

    with tempfile.TemporaryDirectory() as tmp:
        print(f"⬇️  ダウンロード中...")
        apk = download_mega(latest, tmp)

        version_name, version_code = apk_info(apk)
        now_jst = datetime.now(JST)
        tag     = now_jst.strftime("v%Y%m%d-%H%M")
        print(f"🏷️  {tag}  ({version_name} / versionCode: {version_code})")

        # versionCode で更新判定
        last_code = state.get("version_code", 0)
        if version_code <= last_code:
            print(f"✅ 更新なし (versionCode {version_code} ≤ {last_code})")
            return

        # タグ重複時は分を+1してずらす
        while tag in tags:
            now_jst = now_jst.replace(minute=now_jst.minute + 1)
            tag = now_jst.strftime("v%Y%m%d-%H%M")

        checksum = sha256(apk)
        body = "\n".join([
            f"## {APP_LABEL} {version_name}",
            "",
            "| 項目 | 値 |",
            "|---|---|",
            f"| versionName | `{version_name}` |",
            f"| versionCode | `{version_code}` |",
            f"| ファイル名 | `{latest}` |",
            f"| SHA-256 | `{checksum}` |",
            f"| 更新日時 | {now_jst.strftime('%Y-%m-%d %H:%M JST')} |",
        ])

        print(f"🚀 リリース作成中: {tag}")
        create_release(tag, f"{APP_LABEL} {version_name}", body, apk)

        state = {
            "version_name": version_name,
            "version_code": version_code,
            "tag":          tag,
            "filename":     latest,
            "released_at":  now_jst.isoformat(),
        }
        STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        print(f"🎉 完了!")

if __name__ == "__main__":
    main()
