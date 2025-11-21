# xpra/xpra/client/gtk3/window/im_window_ext.py
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk
from xpra.log import Logger
from xpra.client.gtk3.window.stub_window import GtkStubWindow
from xpra.client.gtk3.window.keyboard import KeyboardWindow  # 导入KeyboardWindow
from typing import Dict, Any
import time
from typing import Callable, Optional, Tuple

import requests
import os

typedict = Dict[str, Any]
log = Logger("client", "window", "im")

RIM_SERVER=os.getenv("RIM_SERVER")

class IMEnhancedWindow(GtkStubWindow):
    """无冲突方案：复用KeyboardWindow回调，不新增信号绑定"""
    def init_window(self, client, metadata: typedict, client_props: typedict) -> None:
        log.info("===== IM基础类初始化（复用KeyboardWindow回调） =====")
        self._client = client
        self._window = client
        self._first_key_captured = False
        self.im_context = Gtk.IMMulticontext()
        # 3. 绑定输入法完整信号集（本地程序默认绑定，缺一不可）
        self.im_context.connect("commit", self._on_im_commit)
        self.im_context.connect("preedit-start", self._on_im_preedit_start)
        self.im_context.connect("preedit-end", self._on_im_preedit_end)
        self.im_context.connect("preedit-changed", self._on_im_preedit_changed)
        self.im_context.connect("retrieve-surrounding", self._on_im_retrieve_surrounding)
        self.im_context.connect("delete-surrounding", self._on_im_delete_surrounding)
        self.im_setup = False

    def _on_im_retrieve_surrounding(self, im_context):
        #log.info(f"on im retrieve surrounding")
        # TODO: set surronding
        pass
    def _on_im_delete_surrounding(self, im_context, offfset, n_chars):
        log.info(f"on im delete surrounding OFFSET:{offset} N_CHARS:{n_chars}")
        # TODO: set surronding
        pass

    def _handle_im_events(self, _win, event) -> bool:
        # return False
        self.im_context.focus_in()

        def _update_spot_location(x: Optional[int], y: Optional[int], error: Optional[Exception]) -> None:
            if error:
                print(f"❌ 错误：{str(error)}")
            else:
                """GtkEntry 同款光标位置通知：即使不绘制，也要告诉输入法"""
                if not self.has_focus or not self.get_window():
                    return

                # 构造默认光标矩形（输入法只需要存在，不需要精准位置）
                cursor_rect = Gdk.Rectangle()
                cursor_rect.x = x
                cursor_rect.y = y
                cursor_rect.width = 1
                cursor_rect.height = 20
                self.im_context.set_cursor_location(cursor_rect)

                #print(f"✅ 坐标更新：x={x}, y={y}")

        if not self.im_setup:
            self.im_context.set_client_window(_win.get_window())
            self.im_setup = True
            xid = self._metadata.get("id")
            subscribe_spots(url = f"{RIM_SERVER}/im/cursor_events?xid={xid}", callback= _update_spot_location)

        if self.im_context.filter_keypress(event):
            return True
        return False

    def _on_im_preedit_start(self, im_context):
        log.info(f"predit start, win:{self._metadata.get('id')}")
        pass

    def _on_im_preedit_end(self, im_context):
        log.info(f"predit end, win:{self._metadata.get('id')}")
        pass

    def _on_im_preedit_changed(self, im_context):
        tstr = im_context.get_preedit_string()
        log.info(f"predit change to: {tstr}")


    def _on_im_commit(self, im_context, text):
        """IM输入完成，发送到服务器"""
        requests.get(
            url =f"{RIM_SERVER}/im/commit",
            params = {
                "text": text,
                "xid": self._metadata.get("xid"),
        })

        #log.info(f"IM提交文本：{text}")
        # if self._client and hasattr(self._window, 'wid') and self._window.wid:
        #     self._client.send_text_input(self._window.wid, text)


from threading import Thread
import threading

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
        print(f"[后台线程] 开始订阅坐标，连接地址：{url}")
        print("[后台线程] 按 Ctrl+C 或调用线程 stop() 退出\n")

        running = True
        while running:
            response: Optional[requests.Response] = None
            try:
                # 无主动超时，仅被动响应网络异常
                response = requests.get(
                    url,
                    stream=True,
                    headers={"Connection": "keep-alive"},
                )
                response.raise_for_status()
                print("[后台线程] ✅ 连接成功，持续接收坐标更新...")

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
                err = requests.exceptions.RequestException(f"❌ 网络连接异常: {str(e)}")
                callback(None, None, err)
                if running:
                    print("[后台线程] 5 秒后尝试重连...")
                    time.sleep(5)
            except KeyboardInterrupt:
                break
            except Exception as e:
                err = Exception(f"❌ 未知错误: {str(e)}")
                callback(None, None, err)
                if running:
                    print("[后台线程] 5 秒后尝试重连...")
                    time.sleep(5)
            finally:
                if response is not None:
                    response.close()
                    print("[后台线程] 🔌 已关闭当前连接")

        print("[后台线程] 📤 订阅已停止")

    # 创建并启动后台线程（守护线程，主线程退出时自动结束）
    thread = Thread(target=_background_task, daemon=True)
    thread.start()
    return thread
