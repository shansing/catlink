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

typedict = Dict[str, Any]
log = Logger("client", "window", "im")

RIM_SERVER=os.getenv("RIM_SERVER")

WIN32: bool = sys.platform.startswith("win")

im_windows = set()
spot_location_seq = 0
pending_spot_location = None
spot_subscription_started = False


class IMEnhancedWindow(GtkStubWindow):
    """无冲突方案：复用KeyboardWindow回调，不新增信号绑定"""
    def init_window(self, client, metadata: typedict, client_props: typedict) -> None:
        self._client = client
        self.im_context = Gtk.IMMulticontext()
        # 3. 绑定输入法完整信号集（本地程序默认绑定，缺一不可）
        self.im_context.connect("commit", self._on_im_commit)
        self.im_context.connect("preedit-start", self._on_im_preedit_start)
        self.im_context.connect("preedit-end", self._on_im_preedit_end)
        self.im_context.connect("preedit-changed", self._on_im_preedit_changed)
        self.im_context.connect("retrieve-surrounding", self._on_im_retrieve_surrounding)
        self.im_context.connect("delete-surrounding", self._on_im_delete_surrounding)
        if WIN32:
            self.im_context.set_use_preedit(False)
        self.im_setup = False
        self._im_client_window = None
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
        log.info(f"on im delete surrounding OFFSET:{offset} N_CHARS:{n_chars}")
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

    def _apply_spot_location(self, spot_id: int, x: int, y: int) -> bool:
        has_focus = bool(self.is_active())
        has_window = bool(self.get_window())
        if log.is_debug_enabled():
            log(f"[IM-SPOT #{spot_id}] apply begin: raw=({x},{y}) wid={getattr(self, 'wid', None)} focus={has_focus} window={has_window} {self._focus_debug_state()}")
        if not has_focus or not has_window:
            if log.is_debug_enabled():
                log(f"[IM-SPOT #{spot_id}] apply skip: focus={has_focus} window={has_window}")
            return False

        if x == 0 and y == 0:
            if log.is_debug_enabled():
                log(f"[IM-SPOT #{spot_id}] apply skip: zero spot")
            return False

        window = self.get_window()
        origin_x, origin_y = window.get_origin()[-2:]
        _x = round(x * self._xscale - origin_x)
        _y = round(y * self._yscale - origin_y)
        # 构造默认光标矩形（输入法只需要存在，不需要精准位置）
        cursor_rect = Gdk.Rectangle()
        cursor_rect.x = _x
        cursor_rect.y = _y
        cursor_rect.width = 1
        cursor_rect.height = 20
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

    def _setup_im(self):
        global spot_subscription_started

        def _apply_spot_location() -> bool:
            global pending_spot_location
            if log.is_debug_enabled():
                for candidate in tuple(im_windows):
                    log(f"[IM-SPOT] candidate: wid={getattr(candidate, 'wid', None)} window={bool(candidate.get_window())} {candidate._focus_debug_state()}")
            for window in tuple(im_windows):
                if window.is_active() and window.get_window():
                    spot_location = pending_spot_location
                    pending_spot_location = None
                    if spot_location is None:
                        if log.is_debug_enabled():
                            log("[IM-SPOT] apply skip: no pending spot")
                        return False
                    spot_id, x, y = spot_location
                    return window._apply_spot_location(spot_id, x, y)
            if log.is_debug_enabled():
                log("[IM-SPOT] apply skip: no active window")
            return False

        def _update_spot_location(x: Optional[int], y: Optional[int], error: Optional[Exception]) -> None:
            global spot_location_seq, pending_spot_location

            if error:
                log.error("错误：%s", error)
            else:
                spot_location_seq += 1
                spot_id = spot_location_seq
                pending_spot_location = (spot_id, x, y)
                if log.is_debug_enabled():
                    log(f"[IM-SPOT #{spot_id}] receive: raw=({x},{y})")
                source_id = GLib.idle_add(_apply_spot_location, priority=GLib.PRIORITY_HIGH_IDLE-101)
                if log.is_debug_enabled():
                    log(f"[IM-SPOT #{spot_id}] scheduled idle: source={source_id}")

        if not self.im_setup:
            # not all Windows need IME, for example WeChat' sticker recommend windows (auto pop-up but not focused)
            # window = self.get_window()
            # if window and window is not self._im_client_window:
            #     self.im_context.set_client_window(window)
            #     self._im_client_window = window
            # self.im_context.focus_in()
            # if log.is_debug_enabled():
            #     log(f"[IM-SPOT] setup: wid={getattr(self, 'wid', None)} active={self.is_active()} client_window={window} {self._focus_debug_state()}")
            # self.im_context.reset()
            self.im_setup = True
            im_windows.add(self)
            self.connect("focus-in-event", self._on_im_focus_in)
            self.connect("focus-out-event", self._on_im_focus_out)
            xid = self._metadata.get("xid")
            if not spot_subscription_started:
                spot_subscription_started = True
                subscribe_spots(url = f"{RIM_SERVER}/im/cursor_events?xid={xid}", callback= _update_spot_location)

    def _on_im_focus_in(self, _window, _event) -> None:
        window = self.get_window()
        if window and window is not self._im_client_window:
            self.im_context.set_client_window(window)
            self._im_client_window = window
        self.im_context.focus_in()
        self.im_context.reset()
        if log.is_debug_enabled():
            log(f"[IM-SPOT] focus-in: wid={getattr(self, 'wid', None)} active={self.is_active()} client_window={window} {self._focus_debug_state()}")

    def _on_im_focus_out(self, _window, _event) -> None:
        self.im_context.focus_out()
        if log.is_debug_enabled():
            log(f"[IM-SPOT] focus-out: wid={getattr(self, 'wid', None)} active={self.is_active()} window={bool(self.get_window())}")

    def cleanup(self) -> None:
        im_windows.discard(self)
        self._im_client_window = None

    def _handle_im_events(self, _win, event) -> bool:
        return self.im_context.filter_keypress(event)

    def _on_im_preedit_start(self, im_context):
        #log.info(f"predit start, win:{self._metadata.get('id')}")
        pass

    def _on_im_preedit_end(self, im_context):
        #log.info(f"predit end, win:{self._metadata.get('id')}")
        pass

    def _on_im_preedit_changed(self, im_context):
        im_context.get_preedit_string()


    def _on_im_commit(self, im_context, text):
        """IM输入完成，发送到服务器"""
        requests.packages.urllib3.disable_warnings()
        requests.get(
            url =f"{RIM_SERVER}/im/commit",
            verify = False,
            params = {
                "text": text,
                "xid": self._metadata.get("xid"),
            })

        #log.info(f"IM提交文本：{text}")
        # if self._client and hasattr(self._window, 'wid') and self._window.wid:
        #     self._client.send_text_input(self._window.wid, text)


from threading import Thread

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
