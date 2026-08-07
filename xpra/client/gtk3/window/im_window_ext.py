# xpra/xpra/client/gtk3/window/im_window_ext.py
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib
from xpra.log import Logger
from xpra.client.gtk3.window.stub_window import GtkStubWindow
from typing import Dict, Any
import time
from typing import Callable, Optional, Tuple

import requests
import os
import sys
from threading import Thread

typedict = Dict[str, Any]
log = Logger("client", "window", "im")

RIM_SERVER=os.getenv("RIM_SERVER")
requests.packages.urllib3.disable_warnings()
im_session = requests.Session()

def parse_timeout_env(name: str, default: Tuple[float, float]) -> Tuple[float, float]:
    value = os.getenv(name)
    if not value:
        return default
    try:
        parts = [part.strip() for part in value.split(",", 1)]
        connect_timeout = float(parts[0])
        read_timeout = float(parts[1]) if len(parts) > 1 else connect_timeout
        return connect_timeout, read_timeout
    except Exception as e:
        log.warn("Warning: invalid %s=%r, using default %s: %s", name, value, default, e)
        return default

IM_PREEDIT_TIMEOUT = parse_timeout_env("IM_PREEDIT_TIMEOUT", (5, 5))
IM_COMMIT_TIMEOUT = parse_timeout_env("IM_COMMIT_TIMEOUT", (10, 10))

WIN32: bool = sys.platform.startswith("win")

im_windows = set()
current_im_window = None
spot_location_seq = 0
pending_spot_location = None
latest_spot_location = None
spot_subscription_started = False


class IMEnhancedWindow(GtkStubWindow):
    """无冲突方案：复用KeyboardWindow回调，不新增信号绑定"""
    def init_window(self, client, metadata: typedict, client_props: typedict) -> None:
        self._client = client
        self._catlink_im_preedit = bool(getattr(client, "catlink_im_preedit", False))
        self.im_context = Gtk.IMMulticontext()
        # 3. 绑定输入法完整信号集（本地程序默认绑定，缺一不可）
        self.im_context.connect("commit", self._on_im_commit)
        if self._catlink_im_preedit:
            self.im_context.connect("preedit-start", self._on_im_preedit_start)
            self.im_context.connect("preedit-end", self._on_im_preedit_end)
            self.im_context.connect("preedit-changed", self._on_im_preedit_changed)
        self.im_context.connect("retrieve-surrounding", self._on_im_retrieve_surrounding)
        self.im_context.connect("delete-surrounding", self._on_im_delete_surrounding)
        if not self._catlink_im_preedit:
            self.im_context.set_use_preedit(False)
        else:
            self.im_context.set_use_preedit(True)
        self.im_setup = False
        self._im_is_transient_utility_window = False
        self._im_transient_window = None
        self.when_realized("init-focus-im", self._setup_im)

    # def init_widget_events(self, widget) -> None:
    #     def press(_w, _event) -> bool:
    #         self.im_context.set_client_window(self.get_window())
    #         self.im_context.focus_in()
    #         if log.is_debug_enabled():
    #             log(f"[IM-SPOT] button-press: wid={getattr(self, 'wid', None)} active={self.is_active()} client_window={window} {self._focus_debug_state()}")
    #         return False
    #
    #     widget.connect("button-press-event", press)

    def _on_im_retrieve_surrounding(self, im_context):
        #log.info(f"on im retrieve surrounding")
        # TODO: set surronding
        pass
    def _on_im_delete_surrounding(self, im_context, offset, n_chars):
        log.info("on im delete surrounding OFFSET:%s N_CHARS:%s", offset, n_chars)
        # TODO: set surronding
        pass

    def _focus_debug_state(self) -> str:
        values = []
        for name in ("has_focus", "is_focus", "has_toplevel_focus", "is_active"):
            fn = getattr(self, name, None)
            if callable(fn):
                try:
                    value = fn()
                except Exception as e:
                    value = f"ERR:{e!r}"
            else:
                value = None
            values.append(f"{name}={value}")
        return " ".join(values)

    def _cache_transient_for_window(self) -> None:
        self._im_transient_window = None
        window_types = self._metadata.get("window-type", ())
        self._im_is_transient_utility_window = bool(self._metadata.get("transient-for")) and "UTILITY" in window_types
        if not self._im_is_transient_utility_window:
            return
        self._im_transient_window = self._client.find_window(self._metadata, "transient-for")
        if log.is_debug_enabled():
            log(f"[IM-SPOT] cache transient window: wid={getattr(self, 'wid', None)} transient_for={self._metadata.get('transient-for')} transient_window={self._im_transient_window}")

    def _apply_spot_location(self, spot_id: int, x: int, y: int) -> bool:
        try:
            has_focus = bool(self.is_active())
            window = self.get_window()
            has_window = bool(window)
            if log.is_debug_enabled():
                log(f"[IM-SPOT #{spot_id}] apply begin: raw=({x},{y}) wid={getattr(self, 'wid', None)} focus={has_focus} window={has_window} {self._focus_debug_state()}")
            if not has_focus or not has_window:
                if log.is_debug_enabled():
                    log(f"[IM-SPOT #{spot_id}] apply skip: focus={has_focus} window={has_window}")
                return False

            cursor_height = 20
            origin_x, origin_y = window.get_origin()[-2:]
            geometry = window.get_geometry()
            width = max(1, int(geometry.width))
            height = max(1, int(geometry.height))
            if x == 0 and y == 0:
                # workaround: 如果收到服务端 (0,0) 坐标，判断其是否在当前窗口内，是则重新应用，否则转换到窗口左上角应用（缓解显示到另一显示器左上角的情况）
                fallback_x = origin_x / self._xscale
                fallback_y = origin_y / self._yscale
                latest_in_window = False
                if latest_spot_location is not None:
                    latest_spot_id, latest_x, latest_y = latest_spot_location
                    latest_global_x = round(latest_x * self._xscale)
                    latest_global_y = round(latest_y * self._yscale)
                    latest_in_window = (
                            origin_x <= latest_global_x < origin_x + width
                            and origin_y <= latest_global_y < origin_y + height
                    )
                    if latest_in_window:
                        spot_id, x, y = latest_spot_id, latest_x, latest_y
                    else:
                        x, y = fallback_x, fallback_y
                    if log.is_debug_enabled():
                        log(f"[IM-SPOT #{spot_id}] zero spot replace: latest=({latest_x},{latest_y}) latest_global=({latest_global_x},{latest_global_y}) latest_in_window={latest_in_window} fallback=({fallback_x},{fallback_y}) using=({x},{y}) window_origin=({origin_x},{origin_y}) window_size=({width},{height})")
                else:
                    x, y = fallback_x, fallback_y
                    if log.is_debug_enabled():
                        log(f"[IM-SPOT #{spot_id}] zero spot replace: no latest spot fallback=({fallback_x},{fallback_y}) window_origin=({origin_x},{origin_y}) window_size=({width},{height})")

            scaled_global_x = round(x * self._xscale)
            scaled_global_y = round(y * self._yscale)
            origin_source = "self"
            _x = scaled_global_x - origin_x
            _y = scaled_global_y - origin_y
            if self._im_is_transient_utility_window:
                # workaround: 部分环境（目前发现 Windows）下部分子窗口也会获得输入焦点，服务端坐标还在父窗口，需要拟合到子窗口最近位置然后给出
                transient_window = self._im_transient_window
                transient_gdk_window = transient_window.get_window() if transient_window else None
                parent_origin = None
                if transient_gdk_window:
                    parent_origin = transient_gdk_window.get_origin()[-2:]
                nearest_global_x = min(max(scaled_global_x, origin_x), origin_x + width - 1)
                nearest_global_y = min(max(scaled_global_y, origin_y), origin_y + max(0, height - cursor_height))
                _x = nearest_global_x - origin_x
                _y = nearest_global_y - origin_y
                origin_source = "self-fit"
                if log.is_debug_enabled():
                    relative_to_parent = None
                    if parent_origin is not None:
                        relative_to_parent = (origin_x - parent_origin[0], origin_y - parent_origin[1])
                    log(f"[IM-SPOT #{spot_id}] transient utility fit: wid={getattr(self, 'wid', None)} transient_for={self._metadata.get('transient-for')} transient_window={transient_window} transient_gdk_window={transient_gdk_window} raw=({x},{y}) scaled_global=({scaled_global_x},{scaled_global_y}) parent_origin={parent_origin} transient_origin=({origin_x},{origin_y}) transient_size=({width},{height}) transient_in_parent={relative_to_parent} nearest_global=({nearest_global_x},{nearest_global_y}) local=({_x},{_y}) origin_source={origin_source}")
            if log.is_debug_enabled():
                try:
                    position = self.get_position()
                except Exception as e:
                    position = f"ERR:{e!r}"
                try:
                    root_origin = window.get_root_origin()
                except Exception as e:
                    root_origin = f"ERR:{e!r}"
                try:
                    geometry = window.get_geometry()
                except Exception as e:
                    geometry = f"ERR:{e!r}"
                log(f"[IM-SPOT #{spot_id}] geometry values: origin_source={origin_source} origin=({origin_x},{origin_y}) get_position={position} get_root_origin={root_origin} get_geometry={geometry}")
            if WIN32:
                if WIN32:
                    _x -= origin_x
                    _y -= origin_y
                if log.is_debug_enabled():
                    log(f"[IM-SPOT #{spot_id}] win32 monitor adjust")
            # 构造默认光标矩形（输入法只需要存在，不需要精准位置）
            cursor_rect = Gdk.Rectangle()
            cursor_rect.x = _x
            cursor_rect.y = _y
            cursor_rect.width = 1
            cursor_rect.height = cursor_height
            if log.is_debug_enabled():
                log(f"[IM-SPOT #{spot_id}] apply before set_cursor_location: raw=({x},{y}) wid={getattr(self, 'wid', None)} scale=({self._xscale},{self._yscale}) origin=({origin_x},{origin_y}) local=({_x},{_y}) client_window={window}")
            try:
                self.im_context.set_cursor_location(cursor_rect)
            except Exception as e:
                log.warn("Warning: failed to set IM cursor location: %s", e)
                return False
            if log.is_debug_enabled():
                log(f"[IM-SPOT #{spot_id}] apply after set_cursor_location")
            return False
        except Exception as e:
            log.warn("[IM-SPOT #%s] apply failed: %s", spot_id, e, exc_info=True)
            return False

    def _setup_im(self):
        global spot_subscription_started

        def _apply_spot_location() -> bool:
            global pending_spot_location
            if log.is_debug_enabled():
                for candidate in tuple(im_windows):
                    log(f"[IM-SPOT] candidate: wid={getattr(candidate, 'wid', None)} window={bool(candidate.get_window())} {candidate._focus_debug_state()}")

            window = current_im_window
            if window is None:
                if log.is_debug_enabled():
                    log("[IM-SPOT] apply skip: no current IM window")
                return False
            if not window.get_window():
                if log.is_debug_enabled():
                    log(f"[IM-SPOT] apply skip: current window unrealized wid={getattr(window, 'wid', None)}")
                return False

            spot_location = pending_spot_location
            pending_spot_location = None
            if spot_location is None:
                if log.is_debug_enabled():
                    log("[IM-SPOT] apply skip: no pending spot")
                return False
            spot_id, x, y = spot_location
            if log.is_debug_enabled():
                log(f"[IM-SPOT] current window: wid={getattr(window, 'wid', None)}")
            return window._apply_spot_location(spot_id, x, y)

        def _update_spot_location(x: Optional[int], y: Optional[int], error: Optional[Exception]) -> None:
            global spot_location_seq, pending_spot_location, latest_spot_location

            if error:
                log.error("错误：%s", error)
            else:
                spot_location_seq += 1
                spot_id = spot_location_seq
                pending_spot_location = (spot_id, x, y)
                if x != 0 or y != 0:
                    latest_spot_location = pending_spot_location
                if log.is_debug_enabled():
                    log(f"[IM-SPOT #{spot_id}] receive: raw=({x},{y})")
                source_id = GLib.idle_add(_apply_spot_location, priority=GLib.PRIORITY_HIGH_IDLE-101)
                if log.is_debug_enabled():
                    log(f"[IM-SPOT #{spot_id}] scheduled idle: source={source_id}")

        if not self.im_setup:
            self.im_setup = True
            self._cache_transient_for_window()
            im_windows.add(self)
            # workaround: Windows need this set_use_preedit(False),
            #   otherwise may not work sometimes with pop-up windows
            if not self._catlink_im_preedit:
                self.im_context.set_use_preedit(False)
            else:
                self.im_context.set_use_preedit(True)
            self.connect("focus-in-event", self._on_im_focus_in)
            self.connect("focus-out-event", self._on_im_focus_out)
            xid = self._metadata.get("xid")
            if not spot_subscription_started:
                spot_subscription_started = True
                subscribe_spots(url = f"{RIM_SERVER}/im/cursor_events?xid={xid}", callback= _update_spot_location)

    def _on_im_focus_in(self, _window, _event) -> None:
        global current_im_window
        try:
            window = self.get_window()
            current_im_window = self
            if window:
                self.im_context.set_client_window(window)
            elif log.is_debug_enabled():
                log(f"[IM-SPOT] focus-in set_client_window skip: no client window wid={getattr(self, 'wid', None)}")
            self.im_context.focus_in()
            if latest_spot_location is not None:
                spot_id, x, y = latest_spot_location
                source_id = GLib.idle_add(self._apply_spot_location, spot_id, x, y, priority=GLib.PRIORITY_HIGH_IDLE-101)
                if log.is_debug_enabled():
                    log(f"[IM-SPOT #{spot_id}] focus-in scheduled latest spot: wid={getattr(self, 'wid', None)} source={source_id} raw=({x},{y})")
            # 还是通通不 reset，这样在微信推荐表情弹窗来回切换不会丢失输入
            # if not self._im_is_transient_utility_window:
            #     self.im_context.reset()
            # elif log.is_debug_enabled():
            #     log(f"[IM-SPOT] focus-in im_context.reset skip")
            if log.is_debug_enabled():
                log(f"[IM-SPOT] focus-in: wid={getattr(self, 'wid', None)} active={self.is_active()} client_window={window} {self._focus_debug_state()}")
                if self._im_is_transient_utility_window:
                    log(f"[IM-SPOT] focus-in transient utility: wid={getattr(self, 'wid', None)} transient_for={self._metadata.get('transient-for')} window_type={self._metadata.get('window-type')} active={self.is_active()} client_window={window} {self._focus_debug_state()}")
        except Exception as e:
            log.warn("[IM-SPOT] _on_im_focus_in failed", e, exc_info=True)
            return False

    def _on_im_focus_out(self, _window, _event) -> None:
        self.im_context.focus_out()
        if log.is_debug_enabled():
            log(f"[IM-SPOT] focus-out: wid={getattr(self, 'wid', None)} active={self.is_active()} window={bool(self.get_window())}")

    def cleanup(self) -> None:
        global current_im_window
        if current_im_window is self:
            current_im_window = None
        im_windows.discard(self)
        self._im_transient_window = None

    def _handle_im_events(self, _win, event) -> bool:
        return self.im_context.filter_keypress(event)

    def _on_im_preedit_start(self, im_context):
        #log.info(f"predit start, win:{self._metadata.get('id')}")
        pass

    def _on_im_preedit_end(self, im_context):
        self._send_im_preedit("", 0, False)

    def _on_im_preedit_changed(self, im_context):
        text, _attrs, cursor_pos = im_context.get_preedit_string()
        text = text or ""
        if text and not cursor_pos:
            cursor_pos = len(text)
        self._send_im_preedit(text, cursor_pos or 0, bool(text))

    def _send_im_preedit(self, text: str, cursor_pos: int, visible: bool) -> None:
        if not RIM_SERVER:
            return
        try:
            response = im_session.get(
                url=f"{RIM_SERVER}/im/preedit",
                verify=False,
                timeout=IM_PREEDIT_TIMEOUT,
                params={
                    "text": text,
                    "cursor": max(0, int(cursor_pos)),
                    "visible": "1" if visible else "0",
                    "xid": self._metadata.get("xid"),
                },
            )
            response.raise_for_status()
        except requests.exceptions.Timeout as e:
            log.warn("Warning: timed out sending IM preedit: %s", e)
        except Exception as e:
            log.warn("Warning: failed to send IM preedit: %s", e)

    def _on_im_commit(self, im_context, text):
        """IM输入完成，发送到服务器"""
        try:
            im_session.get(
                url =f"{RIM_SERVER}/im/commit",
                verify = False,
                timeout=IM_COMMIT_TIMEOUT,
                params = {
                    "text": text,
                    "xid": self._metadata.get("xid"),
                })
        except requests.exceptions.Timeout as e:
            log.warn("Warning: timed out sending IM commit: %s", e)
        except Exception as e:
            log.warn("Warning: failed to send IM commit: %s", e)

        #log.info(f"IM提交文本：{text}")
        # if self._client and hasattr(self._window, 'wid') and self._window.wid:
        #     self._client.send_text_input(self._window.wid, text)


def subscribe_spots(url: str, callback: Callable[[Optional[int], Optional[int], Optional[Exception]], None]) -> Thread:
    """
    非阻塞订阅坐标更新（后台线程实现）
    函数调用后立即返回，不阻塞主线程
    :param url: 完整订阅地址（含 xid）
    :param callback: 回调函数 (x, y, error)
    :return: 后台线程对象（可通过 thread.stop() 尝试停止，或 thread.join() 等待结束）
    """
    def parse_coordinate(line: str) -> Tuple[Optional[int], Optional[int], Optional[Exception]]:
        """内部坐标解析函数"""
        line = line.strip()
        if not line:
            return None, None, None
        try:
            x, y = map(int, line.split(','))
            return x, y, None
        except ValueError as e:
            return None, None, ValueError(f"坐标格式无效: {line} (错误: {str(e)})")

    def _background_task() -> None:
        """后台线程执行的核心逻辑"""
        requests.packages.urllib3.disable_warnings()
        running = True
        while running:
            response: Optional[requests.Response] = None
            try:
                # 无主动超时，仅被动响应网络异常
                response = requests.get(
                    url,
                    verify = False,
                    stream=True,
                    headers={"Connection": "keep-alive"},
                )
                response.raise_for_status()

                # 逐行读取（阻塞等待数据，直到连接断开）
                for line_bytes in response.iter_lines(chunk_size=1024, decode_unicode=False):
                    if not running:  # 检测是否需要停止
                        break
                    if not line_bytes:
                        continue

                    # 解码和解析
                    try:
                        line = line_bytes.decode("utf-8")
                        x, y, err = parse_coordinate(line)
                    except UnicodeDecodeError as e:
                        err = UnicodeDecodeError(f"编码解析失败: {str(e)}")
                        x, y = None, None
                    except Exception as e:
                        err = Exception(f"读取行失败: {str(e)}")
                        x, y = None, None

                    # 调用回调（回调函数会在后台线程执行，若需主线程处理可加队列）
                    if err is not None:
                        callback(None, None, err)
                    elif x is not None and y is not None:
                        callback(x, y, None)

            except requests.exceptions.RequestException as e:
                err = requests.exceptions.RequestException(f"网络连接异常: {str(e)}")
                callback(None, None, err)
                if running:
                    log.warn("后台线程 5 秒后尝试重连...")
                    time.sleep(5)
            except KeyboardInterrupt:
                break
            except Exception as e:
                err = Exception(f"未知错误: {str(e)}")
                callback(None, None, err)
                if running:
                    log.warn("后台线程 5 秒后尝试重连...")
                    time.sleep(5)
            finally:
                if response is not None:
                    response.close()
                    log.warn("后台线程已关闭当前连接")

        log.warn("后台线程订阅已停止")

    # 创建并启动后台线程（守护线程，主线程退出时自动结束）
    thread = Thread(target=_background_task, daemon=True)
    thread.start()
    return thread
