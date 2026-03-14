#!/usr/bin/env python3
"""
MEGA公開フォルダ → GitHub Release 自動リリーサー
"""

import os, re, json, subprocess, tempfile, hashlib, urllib.request, urllib.parse, base64
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

# ── ★ここだけリポジトリごとに変える ──────────────────────────
APK_PATTERN = r"YouTube-Music-[\d.]+-Morphe\.apk"
APP_LABEL   = "Morphe YouTube Music"
# ────────────────────────────────────────────────────────────

REPO      = os.environ["REPO"]
GH_TOKEN  = os.environ["GH_TOKEN"]
MEGA_LINK = "https://mega.nz/folder/A2RE0TwD#JIiV5Sy82y6bH_sMeRNH0Q"

STATE_PATH = Path(".release_state.json")
JST        = ZoneInfo("Asia/Tokyo")


# ── MEGA 公開フォルダ ────────────────────────────────────────
def _b64dec(s: str) -> bytes:
    s = s.replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (4 - len(s) % 4) % 4)

def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, (b * (len(a) // len(b) + 1))[:len(a)]))

def _aes_cbc_dec(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    c = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16), backend=default_backend())
    d = c.decryptor()
    return d.update(data) + d.finalize()

def list_mega() -> list[tuple[str, int]]:
    """MEGA APIでファイル一覧を (name, ts) のリストで返す"""
    path_part  = MEGA_LINK.split("/folder/")[1]
    folder_id, folder_key_b64 = path_part.split("#", 1)
    folder_key = _b64dec(folder_key_b64)[:16]

    req_data = json.dumps([{"a": "f", "c": 1, "r": 1, "n": folder_id}]).encode()
    req = urllib.request.Request(
        "https://g.api.mega.co.nz/cs",
        data=req_data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        nodes = json.loads(resp.read())[0].get("f", [])

    files = []  # (name, ts) のリスト
    for node in nodes:
        if node.get("t") != 0:   # 0=ファイル, 1=フォルダ
            continue
        ts = node.get("ts", 0)   # UNIXタイムスタンプ（復号不要）
        try:
            raw_key  = _b64dec(node["k"].split(":")[1])
            node_key = _xor(raw_key, folder_key)
            aes_key  = bytes(
                node_key[i] ^ node_key[i + 16]
                for i in range(16)
            ) if len(node_key) >= 32 else node_key[:16]
            attr_raw = _aes_cbc_dec(_b64dec(node["a"]), aes_key)
            attr_str = attr_raw.decode("utf-8", errors="ignore").lstrip("\x00")
            if attr_str.startswith("MEGA"):
                name = json.loads(attr_str[4:]).get("n", "")
                if name:
                    files.append((name, ts))
        except Exception as e:
            print(f"  ⚠️ ノード解析スキップ ({node.get('h','?')}): {e}")
    return files

def download_mega(filename: str, dest: str) -> str:
    """megadl で公開フォルダから特定ファイルをダウンロード"""
    subprocess.run(
        ["megadl", "--path", dest, f"{MEGA_LINK}/{filename}"],
        check=True, timeout=300
    )
    return os.path.join(dest, filename)


# ── APK 情報 ────────────────────────────────────────────────
def apk_version(path: str) -> str:
    r = subprocess.run(
        ["aapt", "dump", "badging", path],
        capture_output=True, text=True, check=True
    )
    vname = re.search(r"versionName='([^']+)'", r.stdout)
    return vname.group(1) if vname else "0.0.0"

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
    all_files = list_mega()
    print(f"  {len(all_files)} ファイル検出: {[f for f,_ in all_files]}")

    # パターンにマッチするファイルを ts 降順でソートして最新を選ぶ
    matched = sorted(
        [(f, ts) for f, ts in all_files if pattern.search(f)],
        key=lambda x: x[1], reverse=True
    )
    if not matched:
        print(f"⚠️  マッチするAPKなし (pattern: {APK_PATTERN})")
        return

    latest_name, latest_ts = matched[0]
    print(f"📦 対象APK: {latest_name}  (MEGA ts: {latest_ts})")

    # MEGAのタイムスタンプで更新判定
    last_ts = state.get("file_ts", 0)
    if latest_ts <= last_ts:
        last_dt = datetime.fromtimestamp(last_ts, JST).strftime("%Y-%m-%d %H:%M JST")
        print(f"✅ 更新なし (ts {latest_ts} ≤ {last_ts} / 前回: {last_dt})")
        return

    with tempfile.TemporaryDirectory() as tmp:
        print(f"⬇️  ダウンロード中...")
        apk = download_mega(latest_name, tmp)

        version_name = apk_version(apk)
        now_jst = datetime.now(JST)
        tag     = now_jst.strftime("v%Y%m%d-%H%M")
        print(f"🏷️  {tag}  ({version_name})")

        # タグ重複時は分を+1してずらす
        while tag in tags:
            now_jst = now_jst.replace(minute=now_jst.minute + 1)
            tag = now_jst.strftime("v%Y%m%d-%H%M")

        mega_updated = datetime.fromtimestamp(latest_ts, JST).strftime("%Y-%m-%d %H:%M JST")
        checksum = sha256(apk)
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
        print(f"🎉 完了!")

if __name__ == "__main__":
    main()
