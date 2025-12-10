# player/lobby_client.py - 最終交作業版（自動判斷連線目標 + SSE 房間 UI + 未登入自動回登入）

import os, sys, json, asyncio, base64, zipfile, io, shutil, subprocess, socket, signal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAYER_DIR = Path(__file__).resolve().parent
CONFIG = json.load(open(ROOT / "config.json", "r", encoding="utf-8"))

SERVER_IP = CONFIG.get("server_ip") or ""

runtime_path = ROOT / "server" / "runtime_ports.json"
SERVER_RUNTIME = {}
if runtime_path.exists():
    SERVER_RUNTIME = json.load(open(runtime_path, "r", encoding="utf-8"))

def _pick_target(endpoint_key: str, rt_host_key: str, rt_port_key: str, default_port: int,
                 env_host_key: str, env_port_key: str):
    endpoint_cfg = CONFIG.get(endpoint_key, {})

    env_host = os.getenv(env_host_key)
    env_port = os.getenv(env_port_key)
    if env_host and env_port:
        try:
            return env_host, int(env_port)
        except:
            pass
    elif env_host and not env_port:
        if SERVER_RUNTIME:
            port = SERVER_RUNTIME.get(rt_port_key) or endpoint_cfg.get("port", default_port)
        else:
            port = endpoint_cfg.get("port", default_port)
        return env_host, port
    elif env_port and not env_host:
        try:
            forced_port = int(env_port)
        except:
            forced_port = default_port
    else:
        forced_port = None

    if SERVER_RUNTIME:
        port = forced_port or SERVER_RUNTIME.get(rt_port_key) or endpoint_cfg.get("port", default_port)

        if SERVER_IP:
            return SERVER_IP, port

        try:
            s = socket.socket()
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            return "127.0.0.1", port
        except OSError:
            host = SERVER_RUNTIME.get(rt_host_key) or endpoint_cfg.get("host", "127.0.0.1")
            if host == "0.0.0.0":
                host = "127.0.0.1"
            return host, port

    if SERVER_IP:
        host = SERVER_IP
        port = forced_port or endpoint_cfg.get("port", default_port)
        return host, port

    host = endpoint_cfg.get("host", "127.0.0.1")
    port = forced_port or endpoint_cfg.get("port", default_port)

    if host == "0.0.0.0":
        pubs = CONFIG.get("public_hosts") or []
        host = pubs[0] if pubs else "127.0.0.1"

    return host, port

LOBBY_HOST, LOBBY_PORT = _pick_target(
    "lobby_endpoint", "lobby_host", "lobby_port", 5502,
    "LOBBY_CONNECT_HOST", "LOBBY_CONNECT_PORT"
)
DEV_HOST, DEV_PORT = _pick_target(
    "developer_endpoint", "dev_host", "developer_port", 5501,
    "DEV_CONNECT_HOST", "DEV_CONNECT_PORT"
)

def remote_logout(lobby_host, lobby_port, token):
    if not token:
        return
    try:
        s = socket.socket()
        s.settimeout(1.5)
        s.connect((lobby_host, lobby_port))
        s.sendall((json.dumps({
            "kind": "logout",
            "token": token
        }, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            s.recv(4096)
        except:
            pass
        s.close()
        print("[LobbyClient] token released by Ctrl+C")
    except Exception:
        pass

def install_sigint_handler(get_lobby_host, get_lobby_port, get_token):
    def handler(sig, frame):
        remote_logout(get_lobby_host(), get_lobby_port(), get_token())
        print("\n[LobbyClient] bye")
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
    try:
        signal.signal(signal.SIGTERM, handler)
    except Exception:
        pass

DOWNLOADS_ROOT = PLAYER_DIR / "downloads"
DOWNLOADS_ROOT.mkdir(parents=True, exist_ok=True)

def has_local_game_version(player_name: str, game: str, version: str) -> bool:
    p = DOWNLOADS_ROOT / player_name / game / version / "start_client.py"
    return p.exists()

def safe_extract_zip(b: bytes, dest: Path):
    with zipfile.ZipFile(io.BytesIO(b), "r") as z:
        z.extractall(dest)

def get_local_client_dir(player, game, version):
    base = DOWNLOADS_ROOT / player / game / version
    if (base / "manifest.json").exists():
        return base
    return None

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

def ask_choice(prompt: str, valid: set[str]) -> str:
    while True:
        c = input(prompt).strip()
        if c in valid:
            return c
        print("無效的指令，請輸入：", "/".join(sorted(valid)))

# ----------------- 統一未登入判斷 ----------------- #

class AuthExpired(Exception):
    pass

def is_not_logged_in(resp: dict) -> bool:
    if not isinstance(resp, dict):
        return False
    return resp.get("code") == "NOT_LOGGED_IN" or resp.get("error") == "未登入"

async def send_req(payload):
    try:
        reader, writer = await asyncio.open_connection(LOBBY_HOST, LOBBY_PORT)
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        writer.write(line.encode("utf-8"))
        await writer.drain()

        data = await reader.readline()
        writer.close()
        await writer.wait_closed()

        return json.loads(data.decode("utf-8"))
    except ConnectionRefusedError:
        return {"ok": False, "error": "無法連線到大廳伺服器"}
    except Exception as e:
        return {"ok": False, "error": f"連線錯誤：{e}"}

async def send_req_auth(payload):
    resp = await send_req(payload)
    if is_not_logged_in(resp):
        raise AuthExpired()
    return resp

async def fetch_playable_games(token):
    resp = await send_req_auth({"kind":"list_games","token":token})
    if not resp.get("ok"):
        print(resp); return {}
    return resp.get("games", {})

def print_game_menu(games: dict):
    if not games:
        print("【目前無上架遊戲】"); return []
    items = sorted(games.items())
    print("\n# 可玩遊戲（伺服器上架）")
    for i,(name,info) in enumerate(items,1):
        versions = info.get("versions", [])
        latest = info.get("latest", "-")
        author = info.get("author", "未知")
        display_name = info.get("display_name", name)
        avg = info.get("avg_rating")
        cnt = info.get("review_count", 0)
        rating_str = f"{avg} 分／{cnt} 則" if (avg is not None and cnt > 0) else "尚無評分"
        print(f"{i:>2}) {display_name} ({name})  作者: {author}  "
              f"最新版: {latest}  共 {len(versions)} 版  評分: {rating_str}")
    return items

async def fetch_rooms(token):
    resp = await send_req_auth({"kind":"list_rooms","token":token})
    if not resp.get("ok"):
        print(resp); return {}
    return resp.get("rooms", {})

def print_room_menu(rooms: dict):
    if not rooms:
        print("【目前沒有房間】")
        return []
    items = sorted(rooms.items())
    print("\n# 房間列表")
    for i,(rid,r) in enumerate(items,1):
        players = r.get("players", [])
        ready_players = r.get("ready_players", [])
        max_players = r.get("max_players", "?")
        status = r.get("status","?")
        print(f"{i:>2}) {rid}")
        print(f"     遊戲: {r['game']}@{r['version']}")
        print(f"     位址: {r['host']}:{r['port']}")
        print(f"     狀態: {status}  人數: {len(players)}/{max_players}")
        print(f"     玩家: {', '.join(players)}")
        print(f"     已就緒: {', '.join(ready_players) if ready_players else '無'}")
    return items

# ----------------- SSE 房間 UI（保持原邏輯，改用 send_req_auth） ----------------- #

class AsyncRoomUI:
    def __init__(self, token, player, room_id, join_info):
        self.token = token
        self.player = player
        self.room_id = room_id
        self.join_info = join_info
        self.room_info = None
        self.player_ready = False
        self.running = True
        self.game_started = False
        self.reader = None
        self.writer = None
        self.last_start_state = None

    async def connect_stream(self):
        try:
            self.reader, self.writer = await asyncio.open_connection(LOBBY_HOST, LOBBY_PORT)
            line = json.dumps({"kind": "subscribe_room", "token": self.token, "room_id": self.room_id}) + "\n"
            self.writer.write(line.encode("utf-8"))
            await self.writer.drain()

            data = await self.reader.readline()
            resp = json.loads(data.decode("utf-8"))

            if is_not_logged_in(resp):
                raise AuthExpired()

            if not resp.get("ok"):
                print(f"訂閱失敗：{resp.get('error')}")
                return False

            if "room" in resp:
                self.room_info = resp["room"]
                self.display()

            return True
        except AuthExpired:
            raise
        except Exception as e:
            print(f"訂閱失敗：{e}")
            return False

    async def update_loop(self):
        try:
            while self.running:
                data = await self.reader.readline()
                if not data:
                    break

                msg = json.loads(data.decode("utf-8"))
                if msg.get("event") == "room_update":
                    prev_state = self.last_start_state
                    self.room_info = msg.get("room")
                    self.last_start_state = (self.room_info or {}).get("start", {}).get("state")

                    self.display()

                    status = (self.room_info or {}).get("status")
                    start_state = (self.room_info or {}).get("start", {}).get("state")
                    if status == "waiting" and start_state in (None, "idle"):
                        self.game_started = False
                        self.player_ready = (
                            self.player in (self.room_info or {}).get("ready_players", [])
                        )

                    if self.should_auto_start(prev_state, self.last_start_state):
                        print("\n🎮 【所有玩家就緒！自動啟動遊戲...】")
                        await asyncio.sleep(1)
                        await self.start_game()

        except Exception as e:
            if self.running:
                print(f"\n[更新錯誤] {e}")

    def should_auto_start(self, prev_state, curr_state) -> bool:
        if not self.room_info or self.game_started:
            return False
        return (
            prev_state != "agreed"
            and curr_state == "agreed"
            and self.player in (self.room_info or {}).get("players", [])
        )

    async def start_game(self):
        if self.game_started:
            return

        self.game_started = True

        if not has_local_game_version(self.player, self.join_info["game"], self.join_info["version"]):
            print("❌ 請先去商城下載最新版遊戲")
            self.game_started = False
            return

        client_dir = get_local_client_dir(self.player, self.join_info["game"], self.join_info["version"])
        manifest = json.load(open(client_dir / "manifest.json", "r", encoding="utf-8"))
        entry = manifest.get("entry_client", "start_client.py")

        env = os.environ.copy()
        env.update({
            "GAME_HOST": self.join_info["host"],
            "GAME_PORT": str(self.join_info["port"]),
            "ROOM_ID": self.room_id,
            "GAME_NAME": self.join_info["game"],
            "GAME_VERSION": self.join_info["version"],
            "PLAYER_USERNAME": self.player,
            "PLAYER_NAME": self.player
        })

        print(f"\n🎮 正在啟動遊戲客戶端：{entry}")

        if os.name == "nt":
            print("【注意】遊戲將在新視窗中執行")
            subprocess.Popen(
                [sys.executable, entry],
                cwd=str(client_dir),
                env=env,
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
        else:
            print("【注意】遊戲將在當前終端執行")
            subprocess.Popen(
                [sys.executable, entry],
                cwd=str(client_dir),
                env=env
            )

        print("✓ 遊戲客戶端已啟動")

        await asyncio.sleep(1)
        self.game_started = False

        if self.room_info is not None:
            self.room_info["start"] = {"state": "idle"}
            self.room_info["status"] = "waiting"
            self.player_ready = False
            self.display()

    def display(self):
        clear_screen()
        print(f"=== 房間 {self.room_id} ===")
        print(f"(Lobby Server: {LOBBY_HOST}:{LOBBY_PORT})")
        print(f"遊戲：{self.join_info['game']}@{self.join_info['version']}")
        print(f"位址：{self.join_info['host']}:{self.join_info['port']}")

        if self.room_info:
            players = self.room_info.get("players", [])
            status = self.room_info.get("status", "?")
            ready_players = self.room_info.get("ready_players", [])
            max_players = self.room_info.get("max_players", "?")

            if self.player not in players:
                print("\n[系統] 遊戲結束，房間已關閉，按下Enter返回大廳...")
                self.running = False
                return

            print(f"\n【房間狀態】: {status}")
            print(f"【玩家列表】: {len(players)}/{max_players} 人")
            for p in players:
                ready_mark = "✓" if p in ready_players else "✗"
                you_mark = " (你)" if p == self.player else ""
                print(f"  {ready_mark} {p}{you_mark}")

            start = (self.room_info or {}).get("start", {"state":"idle"})
            owner = (self.room_info or {}).get("owner")
            is_owner = (owner == self.player)
            status = self.room_info.get("status", "waiting")

            if status in ("waiting", "ready"):
                if start.get("state") == "idle":
                    if is_owner:
                        print("\n👉 你是房主：按 [s] 提議開始對局")
                    else:
                        print("\n等待房主提議開始…")

                elif start.get("state") == "proposed":
                    responses = start.get("responses", {})
                    guests = [p for p in players if p != owner]

                    if is_owner:
                        print("\n⌛ 已送出開始提議，等待房客回覆：")
                        for guest in guests:
                            if responses.get(guest):
                                print(f"   ✅ {guest}: 已同意")
                            else:
                                print(f"   ⏳ {guest}: 尚未回應")
                    else:
                        if responses.get(self.player):
                            not_responded = [g for g in guests if not responses.get(g, False) and g != self.player]
                            if not_responded:
                                print(f"\n✅ 你已同意，等待其他玩家：{', '.join(not_responded)}")
                            else:
                                print("\n✅ 所有人都已同意，即將開始...")
                        else:
                            print("\n❓ 房主想開始對局：同意請按 [y]，拒絕按 [n]")

                elif start.get("state") == "rejected":
                    rejected_by = start.get("rejected_by")
                    if is_owner:
                        if rejected_by:
                            print(f"\n⚠️ {rejected_by} 拒絕了開始提議")
                        else:
                            print("\n⚠️ 房客已拒絕，請稍後再提議或聊天協調")
                        print("👉 你是房主：按 [s] 提議開始對局")
                    else:
                        if rejected_by == self.player:
                            print("\n你已拒絕此輪開始提議")
                        elif rejected_by:
                            print(f"\n⚠️ {rejected_by} 拒絕了開始提議")
                        else:
                            print("\n⚠️ 有人拒絕了開始提議")

            elif status == "in_game":
                if start.get("state") == "agreed":
                    if self.game_started:
                        print("\n🎮 遊戲已啟動，請在遊戲視窗中操作。")
                    else:
                        print("\n✅ 所有人都同意！即將啟動遊戲…")

        else:
            print("\n[等待房間資料...]")

        print("\n" + "="*50)
        if not self.player_ready:
            print("r) 標記為就緒 (Ready)")
        else:
            print("r) 取消就緒")
        print("q) 離開房間並返回大廳")
        print("\n(房間狀態會自動更新)")

    async def handle_input(self):
        loop = asyncio.get_event_loop()

        while self.running:
            try:
                cmd = await loop.run_in_executor(None, input, "")
                cmd = cmd.strip().lower()

                if self.game_started:
                    if cmd:
                        print("⚠ 遊戲進行中，請在遊戲視窗操作；此視窗指令暫時無效。")
                    continue

                if cmd == "r":
                    if not self.player_ready:
                        resp = await send_req_auth({"kind": "player_ready", "token": self.token, "room_id": self.room_id})
                        if resp.get("ok"):
                            self.player_ready = True
                        else:
                            print(resp.get("error"))
                    else:
                        resp = await send_req_auth({"kind": "player_unready", "token": self.token, "room_id": self.room_id})
                        if resp.get("ok"):
                            self.player_ready = False
                        else:
                            print(resp.get("error"))

                elif cmd == "q":
                    await send_req_auth({
                        "kind": "leave_room",
                        "token": self.token,
                        "room_id": self.room_id
                    })
                    print("\n[系統] 已要求離開房間，返回大廳...")
                    self.running = False

                    if self.writer and not self.writer.is_closing():
                        self.writer.close()
                        try:
                            await self.writer.wait_closed()
                        except Exception:
                            pass
                    break

                elif cmd == "s":
                    resp = await send_req_auth({"kind":"propose_start","token": self.token,"room_id": self.room_id})
                    if not resp.get("ok"):
                        print(resp.get("error"))

                elif cmd == "y":
                    resp = await send_req_auth({"kind":"respond_start","token": self.token,"room_id": self.room_id,"accept": True})
                    if not resp.get("ok"):
                        print(resp.get("error"))

                elif cmd == "n":
                    resp = await send_req_auth({"kind":"respond_start","token": self.token,"room_id": self.room_id,"accept": False})
                    if not resp.get("ok"):
                        print(resp.get("error"))

            except AuthExpired:
                raise
            except Exception as e:
                print(f"輸入錯誤：{e}")
                await asyncio.sleep(0.1)

    async def run(self):
        if not await self.connect_stream():
            input("\n(按 Enter 返回大廳) ")
            return

        self.display()

        try:
            await asyncio.gather(
                self.update_loop(),
                self.handle_input()
            )
        finally:
            self.running = False
            if self.writer:
                self.writer.close()
                try:
                    await self.writer.wait_closed()
                except Exception:
                    pass

async def room_interface(token, player, room_id, join_info):
    ui = AsyncRoomUI(token, player, room_id, join_info)
    await ui.run()

# ----------------- 主流程 ----------------- #

async def async_main():
    token = None
    player = None

    install_sigint_handler(
        lambda: LOBBY_HOST,
        lambda: LOBBY_PORT,
        lambda: token
    )

    while True:
        # ---------- 登入選單 ----------
        while token is None:
            clear_screen()
            print("=== Lobby 登入選單 ===")
            print(f"(Lobby Server: {LOBBY_HOST}:{LOBBY_PORT})")
            print("1) 註冊")
            print("2) 登入")
            print("3) 離開")
            c = ask_choice("請選擇 (1-3): ", set("123"))

            if c == "1":
                u = input("帳號: ").strip()
                p = input("密碼: ").strip()
                resp = await send_req({"kind":"register","username":u,"password":p})
                print(resp)
                input("\n(按 Enter 繼續) ")
            elif c == "2":
                u = input("帳號: ").strip()
                p = input("密碼: ").strip()
                resp = await send_req({"kind":"login","username":u,"password":p})
                if resp.get("ok"):
                    token = resp["token"]
                    player = u
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
            print("=== Lobby 主選單 ===")
            print(f"(Lobby Server: {LOBBY_HOST}:{LOBBY_PORT})")
            print("1) 商城 → 瀏覽遊戲/詳細資訊/下載更新")
            print("2) 大廳 → 建立/查看/加入房間")
            print("3) 我的紀錄 → 評分與評論")
            print("4) 登出並返回登入選單")
            print("5) 離開")
            choice = ask_choice("請選擇 (1-5): ", set("12345"))

            try:
                if choice == "1":
                    # ---------- 商城 ----------
                    while True:
                        clear_screen()
                        print("=== 商城 ===")
                        print(f"(Lobby Server: {LOBBY_HOST}:{LOBBY_PORT})")
                        print("1) 瀏覽遊戲列表")
                        print("2) 查看遊戲詳細資訊")
                        print("3) 下載 / 更新遊戲")
                        print("4) 返回")
                        c2 = ask_choice("選擇 (1-4): ", set("1234"))

                        if c2 == "1":
                            games = await fetch_playable_games(token)
                            _ = print_game_menu(games)
                            input("\n(按 Enter 繼續) ")

                        elif c2 == "2":
                            games = await fetch_playable_games(token)
                            items = print_game_menu(games)
                            if not items:
                                input("\n(按 Enter 繼續) ")
                                continue
                            valid = set(str(i) for i in range(1, len(items)+1))
                            idx = ask_choice("請輸入遊戲編號：", valid)
                            name, info = items[int(idx)-1]

                            resp = await send_req_auth({"kind":"game_details","token":token,"name":name})
                            if resp.get("ok"):
                                d = resp["details"]
                                print(f"\n遊戲：{name}")
                                print(f"作者：{d.get('author','?')}")
                                print(f"狀態：{d.get('status','active')}")
                                print(f"最新版本：{info.get('latest')}")

                                avg = d.get("avg_rating")
                                cnt = d.get("review_count", 0)
                                if avg is not None and cnt > 0:
                                    print(f"平均評分：{avg} 分（{cnt} 則評論）")
                                else:
                                    print("平均評分：尚無評論")

                                reviews = d.get("reviews", {})
                                if reviews:
                                    print("\n--- 評論列表 ---")
                                    for user, rv in reviews.items():
                                        print(f"- {user}：{rv.get('rating', '?')} 分")
                                        text = (rv.get("text") or "").strip()
                                        if text:
                                            print(f"  {text}")
                                else:
                                    print("\n目前還沒有任何評論。")
                            else:
                                print(resp.get("error"))
                            input("\n(按 Enter 繼續) ")

                        elif c2 == "3":
                            games = await fetch_playable_games(token)
                            items = print_game_menu(games)
                            if not items:
                                input("\n目前沒有可下載的遊戲。(按 Enter 繼續) ")
                                continue

                            valid = set(str(i) for i in range(1, len(items)+1))
                            idx = ask_choice("請輸入要下載的遊戲編號：", valid)
                            name, info = items[int(idx)-1]

                            print(f"\n正在向伺服器請求 {name} 最新版本安裝包...")
                            resp = await send_req_auth({
                                "kind": "download_game",
                                "token": token,
                                "name": name
                            })
                            if not resp.get("ok"):
                                print("✗ 無法下載：", resp.get("error"))
                                input("\n(按 Enter 繼續) ")
                                continue

                            version = resp["version"]
                            zip_b64 = resp["zip_b64"]
                            data = base64.b64decode(zip_b64.encode("utf-8"))

                            base_dir = DOWNLOADS_ROOT / player / name
                            if base_dir.exists():
                                for sub in base_dir.iterdir():
                                    if sub.is_dir():
                                        shutil.rmtree(sub, ignore_errors=True)

                            dest = base_dir / version
                            dest.mkdir(parents=True, exist_ok=True)
                            safe_extract_zip(data, dest)

                            print(f"✓ 已下載 {name}@{version} 到 {dest}")
                            print("  之前的舊版本已自動清除。")
                            input("\n(按 Enter 繼續) ")

                        else:
                            break

                elif choice == "2":
                    # ---------- 大廳 ----------
                    while True:
                        clear_screen()
                        print("=== 大廳 ===")
                        print(f"(Lobby Server: {LOBBY_HOST}:{LOBBY_PORT})")
                        print("1) 建立房間")
                        print("2) 查看房間列表")
                        print("3) 加入房間（輸入房間 ID）")
                        print("4) 返回")
                        c2 = ask_choice("選擇 (1-4): ", set("1234"))

                        if c2 == "1":
                            games = await fetch_playable_games(token)
                            items = print_game_menu(games)
                            if not items:
                                input("\n(按 Enter 繼續) ")
                                continue

                            valid = set(str(i) for i in range(1, len(items) + 1))
                            idx = ask_choice("請輸入欲遊玩的遊戲編號：", valid)
                            name, info = items[int(idx) - 1]

                            latest_ver = info.get("latest")
                            if not latest_ver:
                                print("❌ 此遊戲目前沒有可用版本。")
                                input("\n(按 Enter 繼續) ")
                                continue

                            if not has_local_game_version(player, name, latest_ver):
                                print("❌ 你目前尚未下載這款遊戲的最新版。")
                                print("   請先到『商城』→『下載 / 更新遊戲』下載後，再建立房間。")
                                input("\n(按 Enter 繼續) ")
                                continue

                            resp = await send_req_auth({
                                "kind": "create_room",
                                "token": token,
                                "game": name,
                                "version": latest_ver,
                            })
                            if resp.get("ok"):
                                room_id = resp.get("room_id")
                                print(f"✓ 房間建立成功：{room_id}")
                                await asyncio.sleep(1)
                                await room_interface(token, player, room_id, resp)
                            else:
                                print(f"✗ {resp.get('error')}")
                                input("\n(按 Enter 繼續) ")

                        elif c2 == "2":
                            rooms = await fetch_rooms(token)
                            print_room_menu(rooms)
                            input("\n(按 Enter 繼續) ")

                        elif c2 == "3":
                            rooms = await fetch_rooms(token)
                            items = print_room_menu(rooms)
                            if not items:
                                input("\n目前沒有房間可以加入。(按 Enter 繼續) ")
                                continue

                            print()
                            rid = input("請輸入要加入的房間 ID如：tetris-xxxx（或 Enter 返回）：").strip()
                            if not rid:
                                continue

                            r = rooms.get(rid)
                            if not r:
                                print("❌ 房間不存在")
                                input("\n(按 Enter 繼續) ")
                                continue

                            game_name = r["game"]
                            room_ver  = r["version"]

                            games = await fetch_playable_games(token)
                            ginfo = games.get(game_name)
                            if not ginfo:
                                print("⚠ 此遊戲目前已下架或不可下載，無法加入新房間。")
                                input("\n(按 Enter 繼續) ")
                                continue

                            latest_ver = ginfo.get("latest")
                            if room_ver != latest_ver:
                                print(f"⚠ 此房間使用舊版本 {room_ver}，目前最新版本為 {latest_ver}。")
                                print("   請先到『商城』下載 / 更新到最新版本。")
                                input("\n(按 Enter 繼續) ")
                                continue

                            if not has_local_game_version(player, game_name, latest_ver):
                                print("❌ 你目前尚未下載此遊戲的最新版。")
                                print("   請先到『商城』下載 / 更新遊戲。")
                                input("\n(按 Enter 繼續) ")
                                continue

                            join = await send_req_auth({"kind": "join_room", "token": token, "room_id": rid})
                            if not join.get("ok"):
                                print(f"✗ {join.get('error')}")
                                input("\n(按 Enter 繼續) ")
                                continue

                            print(f"✓ 成功加入房間：{rid}")
                            await asyncio.sleep(1)
                            await room_interface(token, player, rid, join)

                        else:
                            break

                elif choice == "3":
                    clear_screen()
                    print("=== 我的紀錄 → 評分與評論 ===")
                    print(f"(Lobby Server: {LOBBY_HOST}:{LOBBY_PORT})")

                    games = await fetch_playable_games(token)
                    items = print_game_menu(games)
                    if not items:
                        input("\n目前沒有可評分的遊戲\n(按 Enter 返回) ")
                        continue

                    valid = set(str(i) for i in range(1, len(items)+1))
                    idx = ask_choice("請選擇要評分的遊戲編號：", valid)
                    name, info = items[int(idx) - 1]
                    display_name = info.get("display_name", name)

                    print(f"\n選擇遊戲：{display_name} ({name})")
                    rating = input("評分 (1-5): ").strip()
                    text = input("短評 (可留空): ").strip()

                    try:
                        rating_int = int(rating)
                    except:
                        print("評分需為數字 1-5")
                        input("\n(按 Enter 繼續) ")
                        continue

                    resp = await send_req_auth({
                        "kind": "rate_game",
                        "token": token,
                        "name": name,
                        "rating": rating_int,
                        "text": text
                    })

                    if resp.get("ok"):
                        print("✓ 評論已送出")
                        avg = resp.get("avg_rating")
                        cnt = resp.get("count")
                        if avg is not None:
                            print(f"目前平均分數：{avg}（{cnt} 則評論）")
                    else:
                        print("✗ 無法送出評論：", resp.get("error"))

                    input("\n(按 Enter 繼續) ")

                elif choice == "4":
                    if token is not None:
                        try:
                            await send_req({"kind": "logout", "token": token})
                        except Exception:
                            pass
                    token = None
                    player = None
                    print("已登出，返回登入選單。")
                    input("\n(按 Enter 繼續) ")
                    break

                elif choice == "5":
                    if token is not None:
                        try:
                            await send_req({"kind": "logout", "token": token})
                        except Exception:
                            pass
                    print("再見～")
                    return

            except AuthExpired:
                print("\n⚠ 你的登入已失效或被登出，請重新登入。")
                token = None
                player = None
                input("(按 Enter 返回登入介面) ")
                break

def main():
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
