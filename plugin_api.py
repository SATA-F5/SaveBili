import asyncio
import base64
import io
import json
import time
from pathlib import Path

import qrcode
from quart import jsonify, request

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_bili_downloader"

class PluginAPI:
    def __init__(self, plugin):
        self.plugin = plugin
        self.current_poll_task = None

    def register(self, context):
        routes = [
            ("/api/qrcode/generate", "handle_qrcode_generate", ["GET"]),
            ("/api/login/password", "handle_password_login", ["POST"]),
            ("/api/cookies", "handle_get_cookies", ["GET"]),
            ("/api/cookies/add", "handle_add_cookie", ["POST"]),
            ("/api/cookies/update", "handle_update_cookie", ["POST"]),
            ("/api/cookies/delete", "handle_delete_cookie", ["POST"]),
            ("/api/cookies/clear", "handle_clear_cookies", ["POST"]),
            ("/api/config", "handle_get_config", ["GET"]),
            ("/api/config/update", "handle_update_config", ["POST"]),
        ]
        for route, handler_name, methods in routes:
            handler = getattr(self, handler_name)
            context.register_web_api(
                f"/{PLUGIN_NAME}{route}",
                handler,
                methods,
                f"Bili Manager: {handler_name}",
            )

    # ---------- 扫码登录 ----------
    async def handle_qrcode_generate(self):
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                    headers={"User-Agent": "Mozilla/5.0"},
                ) as resp:
                    data = await resp.json()
                    if data.get("code") != 0:
                        return jsonify({"success": False, "error": data.get("message", "生成二维码失败")})
                    qrcode_key = data["data"]["qrcode_key"]
                    url = data["data"]["url"]

                    img = qrcode.make(url)
                    buf = io.BytesIO()
                    img.save(buf, format='PNG')
                    buf.seek(0)
                    img_base64 = base64.b64encode(buf.read()).decode('ascii')
                    img_data_url = f"data:image/png;base64,{img_base64}"

                    if self.current_poll_task and not self.current_poll_task.done():
                        self.current_poll_task.cancel()

                    self.current_poll_task = asyncio.create_task(self._poll_qrcode(qrcode_key))

                    return jsonify({
                        "success": True,
                        "qrcode_key": qrcode_key,
                        "image_data_url": img_data_url
                    })
        except Exception as e:
            logger.error(f"生成二维码异常: {e}")
            return jsonify({"success": False, "error": str(e)})

    async def _poll_qrcode(self, qrcode_key: str):
        import aiohttp
        deadline = time.time() + 180
        while time.time() < deadline:
            await asyncio.sleep(3)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}",
                        headers={"User-Agent": "Mozilla/5.0"},
                    ) as resp:
                        data = await resp.json()
                        code = data.get("data", {}).get("code")
                        if code == 0:
                            cookie_str = self._extract_cookie_from_response(data, resp)
                            if cookie_str:
                                await self._add_cookie_from_scan(cookie_str)
                                logger.info("B 站扫码登录成功，Cookie 已保存")
                            return
                        elif code == 86038:
                            logger.info("二维码已失效")
                            return
            except asyncio.CancelledError:
                logger.info("旧二维码轮询任务已取消")
                return
            except Exception as e:
                logger.error(f"轮询登录状态异常: {e}")

    def _extract_cookie_from_response(self, data, resp) -> str:
        set_cookies = resp.headers.getall('Set-Cookie', [])
        if set_cookies:
            cookies = []
            for c in set_cookies:
                parts = c.split(';')
                if parts:
                    cookies.append(parts[0].strip())
            if cookies:
                return "; ".join(cookies)
        url = data.get("data", {}).get("url", "")
        if "SESSDATA" in url:
            from urllib.parse import urlparse, parse_qs
            query = parse_qs(urlparse(url).query)
            sessdata = query.get("SESSDATA", [""])[0]
            bili_jct = query.get("bili_jct", [""])[0]
            dedeuserid = query.get("DedeUserID", [""])[0]
            return f"SESSDATA={sessdata}; bili_jct={bili_jct}; DedeUserID={dedeuserid}"
        return ""

    async def _add_cookie_from_scan(self, cookie_str: str):
        new_cookie = {
            "cookie": cookie_str,
            "remark": "扫码登录",
            "priority": 10,
            "enabled": True
        }
        self.plugin.cookies.append(new_cookie)
        self.plugin._save_cookies()

    # ---------- 账密登录（占位） ----------
    async def handle_password_login(self):
        data = await request.get_json()
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()
        if not username or not password:
            return jsonify({"success": False, "error": "用户名和密码不能为空"})
        return jsonify({
            "success": False,
            "error": "账密登录暂不可用，请使用扫码登录或手动添加 Cookie"
        })

    # ---------- Cookie 管理 ----------
    async def handle_get_cookies(self):
        return jsonify({"success": True, "cookies": self.plugin.cookies})

    async def handle_add_cookie(self):
        data = await request.get_json()
        cookie_str = str(data.get("cookie", "")).strip()
        remark = str(data.get("remark", "")).strip()
        priority = int(data.get("priority", 100))
        enabled = bool(data.get("enabled", True))

        if not cookie_str:
            return jsonify({"success": False, "error": "Cookie 不能为空"})

        # 如果用户输入的是纯 SESSDATA 值（不包含 '='），自动添加前缀
        if "=" not in cookie_str:
            cookie_str = f"SESSDATA={cookie_str}"

        self.plugin.cookies.append({
            "cookie": cookie_str,
            "remark": remark or "手动添加",
            "priority": priority,
            "enabled": enabled
        })
        self.plugin._save_cookies()
        return jsonify({"success": True})

    async def handle_update_cookie(self):
        data = await request.get_json()
        index = data.get("index")
        if index is None or index < 0 or index >= len(self.plugin.cookies):
            return jsonify({"success": False, "error": "无效的索引"})
        updates = {}
        if "cookie" in data:
            updates["cookie"] = str(data["cookie"]).strip()
        if "remark" in data:
            updates["remark"] = str(data["remark"]).strip()
        if "priority" in data:
            updates["priority"] = int(data["priority"])
        if "enabled" in data:
            updates["enabled"] = bool(data["enabled"])
        self.plugin.cookies[index].update(updates)
        self.plugin._save_cookies()
        return jsonify({"success": True})

    async def handle_delete_cookie(self):
        data = await request.get_json()
        index = data.get("index")
        if index is None or index < 0 or index >= len(self.plugin.cookies):
            return jsonify({"success": False, "error": "无效的索引"})
        del self.plugin.cookies[index]
        self.plugin._save_cookies()
        return jsonify({"success": True})

    async def handle_clear_cookies(self):
        self.plugin.cookies = []
        self.plugin._save_cookies()
        return jsonify({"success": True})

    # ---------- 配置管理 ----------
    async def handle_get_config(self):
        return jsonify({
            "success": True,
            "config": {
                "download_dir": self.plugin.download_dir,
                "max_size_mb": self.plugin.max_size_mb,
                "bot_qq": self.plugin.bot_qq,
                "quality": self.plugin.quality,
            }
        })

    async def handle_update_config(self):
        data = await request.get_json()
        if not data:
            return jsonify({"success": False, "error": "无数据"})
        if "download_dir" in data:
            self.plugin.download_dir = str(data["download_dir"]).strip()
        if "max_size_mb" in data:
            self.plugin.max_size_mb = float(data["max_size_mb"])
        if "bot_qq" in data:
            self.plugin.bot_qq = str(data["bot_qq"]).strip()
        if "quality" in data:
            self.plugin.quality = int(data["quality"])
        self.plugin._save_plugin_config()
        import os
        os.makedirs(self.plugin.download_dir, exist_ok=True)
        return jsonify({"success": True})