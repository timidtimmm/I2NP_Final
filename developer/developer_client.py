# developer/developer_client.py - 穩定版（自動判斷連線目標 + 版本防呆 + 未登入自動回登入 + 顯示玩家回饋）

import os, sys, json, asyncio, base64, zipfile, io, socket, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.load(open(ROOT / "config.json", "r", encoding="utf-8"))

SERVER_IP = CONFIG.get("server_ip") or ""

DEV_DIR = Path(__file__).resolve().parent
GAMES_ROOT = DEV_DIR / "games"
GAMES_ROOT.mkdir(parents=True, exist_ok=True)

_runtime_path = ROOT / "server" / "runtime_ports.json"
if _runtime_path.exists():
    SERVER_RUNTIME = json.load(open(_runtime_path, "r", encoding="utf-8"))
else:
    SERVER_RUNTIME = {}

VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

def validate_version(version: str):
    if not version or not isinstance(version, str):
        return False, "版本號不能為空。"
    if not VERSION_RE.match(version):
        return False, "版本格式錯誤，需為：major.minor.patch（例如 1.0.3）。"
    return True, ""

REQUIRED_MANIFEST_KEYS = [
    "name",
    "display_name",
    "type",
    "max_players",
    "entry_server",
    "entry_client",
    "description",
]

def _pick_dev_target():
    endpoint_cfg = CONFIG.get("developer_endpoint", {})
    cfg_host = endpoint_cfg.get("host", "127.0.0.1")
    cfg_port = endpoint_cfg.get("port", 5501)

    env_host = os.getenv("DEV_CONNECT_HOST")
    env_port = os.getenv("DEV_CONNECT_PORT")
    if env_host or env_port:
        host = env_host or (SERVER_IP or cfg_host)
        try:
            port = int(env_port) if env_port else cfg_port
        except ValueError:
            port = cfg_port

        if host == "0.0.0.0":
            pubs = CONFIG.get("public_hosts") or []
            host = pubs[0] if pubs else "127.0.0.1"
        return host, port

    if SERVER_IP:
        return SERVER_IP, cfg_port

    if SERVER_RUNTIME:
        port = SERVER_RUNTIME.get("developer_port", cfg_port)
        try:
            s = socket.socket()
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            return "127.0.0.1", port
        except OSError:
            host = SERVER_RUNTIME.get("dev_host", cfg_host)
            if host == "0.0.0.0":
                host = "127.0.0.1"
            return host, port

    host = cfg_host
    if host == "0.0.0.0":
        pubs = CONFIG.get("public_hosts") or []
        host = pubs[0] if pubs else "127.0.0.1"
    return host, cfg_port

DEV_HOST, DEV_PORT = _pick_dev_target()

CURRENT_TOKEN = None

class AuthExpired(Exception):
    pass

def is_not_logged_in(resp: dict) -> bool:
    if not isinstance(resp, dict):
        return False
    return resp.get("code") == "NOT_LOGGED_IN" or resp.get("error") == "未登入"

async def _read_json_line(reader: asyncio.StreamReader) -> dict:
    buf = b""
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            if not buf:
                raise EOFError("server closed connection with no data")
            break
        buf += chunk
        if b"\n" in buf:
            line, _ = buf.split(b"\n", 1)
            break
    return json.loads(line.decode("utf-8"))

async def send_req(obj: dict):
    try:
        reader, writer = await asyncio.open_connection(DEV_HOST, DEV_PORT)
    except Exception:
        return {"ok": False, "error": "無法連線到開發者伺服器（DevServer）。請稍後再試。"}

    try:
        line = json.dumps(obj) + "\n"
        writer.write(line.encode("utf-8"))
        await writer.drain()

        resp_obj = await _read_json_line(reader)
        writer.close()
        await writer.wait_closed()
        return resp_obj

    except Exception:
        return {"ok": False, "error": "開發者伺服器回應異常或已中斷連線。"}

async def send_req_auth(obj: dict):
    resp = await send_req(obj)
    if is_not_logged_in(resp):
        raise AuthExpired()
    return resp

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

def ask_choice(prompt: str, valid: set[str]) -> str:
    while True:
        try:
            c = input(prompt).strip()
        except EOFError:
            raise
        if c in valid:
            return c
        print("無效的指令，請輸入：", "/".join(sorted(valid)))

async def async_main():
    global CURRENT_TOKEN

    while True:
        token = None
        developer = None
        CURRENT_TOKEN = None

        # ---------- 登入選單 ----------
        while token is None:
            clear_screen()
            print("=== 開發者平台登入 ===")
            print(f"(目前 Developer Server: {DEV_HOST}:{DEV_PORT})")
            print("1) 註冊")
            print("2) 登入")
            print("3) 離開")
            c = ask_choice("請選擇 (1-3): ", set("123"))

            if c == "1":
                u = input("帳號: ").strip()
                p = input("密碼: ").strip()
                resp = await send_req({"kind": "register", "username": u, "password": p})
                print(resp)
                input("\n(按 Enter 繼續) ")
            elif c == "2":
                u = input("帳號: ").strip()
                p = input("密碼: ").strip()
                resp = await send_req({"kind": "login", "username": u, "password": p})
                if resp.get("ok"):
                    token = resp["token"]
                    CURRENT_TOKEN = token
                    developer = u
                    print("登入成功")
                    input("\n(按 Enter 繼續) ")
                else:
                    print(resp)
                    input("\n(按 Enter 繼續) ")
            else:
                return

        # ---------- 主選單 ----------
        while token is not None:
            clear_screen()
            print("=== 開發者主選單 ===")
            print(f"(Developer Server: {DEV_HOST}:{DEV_PORT})")
            print("1) 上傳/更新遊戲")
            print("2) 查看我的遊戲")
            print("3) 下架遊戲")
            print("4) 登出")
            print("5) 離開")
            choice = ask_choice("請選擇 (1-5): ", set("12345"))

            try:
                # 1) 上傳 / 更新
                if choice == "1":
                    game_name = input("遊戲名稱: ").strip()
                    if not game_name:
                        print("❌ 遊戲名稱不可空白")
                        input("\n(按 Enter 繼續) ")
                        continue

                    hint = await send_req_auth({
                        "kind": "version_hint",
                        "token": token,
                        "name": game_name
                    })

                    if not hint.get("ok"):
                        print("✗ 無法取得版本資訊：", hint.get("error"))
                        input("\n(按 Enter 繼續) ")
                        continue

                    if not hint.get("exists"):
                        print(f"📦 這是一款新遊戲：{game_name}")
                        print("   建議初始版本號：1.0.0")
                        suggested = "1.0.0"
                    else:
                        latest = hint.get("latest")
                        suggested = hint.get("suggested", "1.0.0")
                        print(f"📦 遊戲 {game_name} 目前最新版本為：{latest}")
                        print(f"   建議下一個版本號：{suggested}")
                        vers = hint.get("versions") or []
                        if vers:
                            print(f"   目前已有版本列表：{vers}")

                    game_dir = GAMES_ROOT / game_name
                    manifest_path = game_dir / "manifest.json"

                    if not game_dir.exists():
                        print("❌ 找不到遊戲資料夾：", game_dir)
                        input("\n(按 Enter 繼續) ")
                        continue

                    if not manifest_path.exists():
                        print("❌ 遊戲資料夾缺少 manifest.json：", manifest_path)
                        input("\n(按 Enter 繼續) ")
                        continue

                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    except Exception as e:
                        print(f"讀取 manifest.json 失敗：{e}")
                        input("\n(按 Enter 繼續) ")
                        continue

                    missing_keys = [
                        k for k in REQUIRED_MANIFEST_KEYS
                        if manifest.get(k) in (None, "", [])
                    ]
                    if missing_keys:
                        print("❌ manifest.json 缺少以下重要欄位：", ", ".join(missing_keys))
                        print("   請先打開並修正此檔案後再重新上傳：")
                        print(f"   {manifest_path}")
                        input("\n(按 Enter 繼續) ")
                        continue

                    if manifest.get("name") and manifest["name"] != game_name:
                        print(f"⚠ 警告：manifest.json 裡的 name = {manifest['name']}，"
                              f"與資料夾名稱 {game_name} 不同。建議保持一致。")

                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                        for path in game_dir.rglob("*"):
                            if path.is_file():
                                rel = path.relative_to(game_dir)
                                z.write(path, rel.as_posix())
                    zip_bytes = buf.getvalue()
                    zip_b64 = base64.b64encode(zip_bytes).decode("utf-8")

                    while True:
                        ver_input = input(
                            f"版本號（例如 1.0.0；直接 Enter 使用建議值 {suggested}）: "
                        ).strip()
                        version = suggested if not ver_input else ver_input

                        ok_ver, msg_ver = validate_version(version)
                        if not ok_ver:
                            print("❌", msg_ver)
                            retry_ver = ask_choice("要重新輸入版本號嗎？(y/n): ",
                                                   set(["y", "Y", "n", "N"]))
                            if retry_ver.lower() == "y":
                                continue
                            else:
                                break

                        print(f"\n正在上傳 {game_name}@{version} ...")
                        resp = await send_req_auth({
                            "kind": "upload_game",
                            "token": token,
                            "name": game_name,
                            "version": version,
                            "manifest": manifest,
                            "zip_b64": zip_b64
                        })

                        if resp.get("ok"):
                            print(f"✓ 上傳成功：{resp.get('name')} 最新版 {resp.get('latest')} (status={resp.get('status')})")
                            input("\n(按 Enter 繼續) ")
                            break

                        err = resp.get("error", "未知錯誤")
                        print("✗ 上傳失敗：", err)

                        latest = resp.get("latest")
                        suggested2 = resp.get("suggested")
                        if latest and suggested2:
                            print(f"  目前最新版本為 {latest}，建議下一個可用版本號：{suggested2}")
                            suggested = suggested2

                        retry = ask_choice("要重新輸入版本號並重試嗎？(y/n): ",
                                           set(["y", "Y", "n", "N"]))
                        if retry.lower() != "y":
                            break

                # 2) 查看我的遊戲
                elif choice == "2":
                    resp = await send_req_auth({"kind": "my_games", "token": token})
                    if resp.get("ok"):
                        games = resp.get("games", {})
                        if not games:
                            print("你還沒有上傳任何遊戲")
                        else:
                            for name, info in games.items():
                                print(f"\n{'='*50}")
                                print(f"遊戲：{name}")
                                print(f"  狀態：{info.get('status')}")
                                print(f"  最新版本：{info.get('latest')}")

                                versions = info.get('versions', {})
                                version_list = list(versions.keys())
                                print(f"  版本列表：{version_list}")

                                if versions:
                                    print("  版本詳情：")
                                    for ver, ver_info in versions.items():
                                        display_name = ver_info.get('display_name', name)
                                        game_type = ver_info.get('type', 'Unknown')
                                        max_players = ver_info.get('max_players', '?')
                                        print(f"    - {ver}: {display_name} [{game_type}, {max_players}人]")

                                # ✅ 修正順序：先拿 avg 再判斷
                                avg = info.get('avg_rating')
                                count = info.get('review_count', 0)

                                if avg is not None and count > 0:
                                    print(f"  評分：{avg} ⭐ ({count} 則評論)")
                                else:
                                    print("  評分：尚無評論")

                                # ✅ 新增：顯示評論內容
                                reviews = info.get("reviews", {}) or {}
                                if reviews:
                                    print("  --- 玩家回饋 ---")

                                    def _ts(rv):
                                        try:
                                            return int(rv.get("ts", 0))
                                        except:
                                            return 0

                                    for user, rv in sorted(
                                        reviews.items(),
                                        key=lambda kv: _ts(kv[1]),
                                        reverse=True
                                    ):
                                        rating = rv.get("rating", "?")
                                        text = (rv.get("text") or "").strip()
                                        if text:
                                            print(f"   - {user}: {rating} 分 | {text}")
                                        else:
                                            print(f"   - {user}: {rating} 分")

                                print(f"{'='*50}")
                    else:
                        print(resp)
                    input("\n(按 Enter 繼續) ")

                # 3) 下架遊戲
                elif choice == "3":
                    game_name = input("(使用者將無法建立新房間，也看不到該遊戲在任何地方)\n要下架的遊戲名稱: ").strip()
                    resp = await send_req_auth({
                        "kind": "remove_game",
                        "token": token,
                        "name": game_name
                    })
                    print(resp)
                    input("\n(按 Enter 繼續) ")

                # 4) 登出
                elif choice == "4":
                    if token is not None:
                        resp = await send_req({"kind": "logout", "token": token})
                        print(resp.get("msg", "已登出"))
                    token = None
                    CURRENT_TOKEN = None
                    developer = None
                    input("\n(按 Enter 返回登入介面) ")
                    break

                # 5) 離開
                elif choice == "5":
                    if token is not None:
                        try:
                            await send_req({"kind": "logout", "token": token})
                        except Exception:
                            pass
                    CURRENT_TOKEN = None
                    print("再見～")
                    return

            except AuthExpired:
                print("\n⚠ 你的登入已失效或被登出，請重新登入。")
                token = None
                CURRENT_TOKEN = None
                developer = None
                input("(按 Enter 返回登入介面) ")
                break

def main():
    global CURRENT_TOKEN

    try:
        asyncio.run(async_main())
    except (KeyboardInterrupt, EOFError):
        if CURRENT_TOKEN is None:
            print("\n[系統] 再見！")
            return

        async def _cleanup():
            global CURRENT_TOKEN
            try:
                print("\n[系統] 正在釋放 token...")
                resp = await send_req({"kind": "logout", "token": CURRENT_TOKEN})
                if resp.get("ok"):
                    print("[系統] 已成功登出並釋放 token")
                else:
                    print(f"[系統] 登出回應：{resp}")
            except Exception as e:
                print(f"[系統] 登出時發生錯誤（server 可能已關閉）：{e}")
            finally:
                CURRENT_TOKEN = None
                print("[系統] 再見！")

        asyncio.run(_cleanup())

if __name__ == "__main__":
    main()
