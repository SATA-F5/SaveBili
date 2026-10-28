import re
import os
import time
import json
import asyncio
import tempfile
import aiohttp
import sys
import traceback
import zipfile
import tarfile
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

from .plugin_api import PluginAPI

sys.stdout.reconfigure(encoding='utf-8')

gk_art = r"""//       ________ __    _____    __                  __   _
//      / ____/ //_/   / ___/   / /_   __  __   ____/ /  (_)  ____
//     / / __/ ,<      \__ \   / __/  / / / /  / __  /  / /  / __ \
//    / /_/ / /| |    ___/ /  / /_   / /_/ /  / /_/ /  / /  / /_/ /
//    \____/_/ |_|   /____/   \__/   \__,_/   \__,_/  /_/   \____/
"""
print(gk_art)


@register("saveany_bilibili_downloader", "Shou_Lu",
          "使用 saveany 解析并下载 B 站内容，支持视频/番剧/专栏/收藏夹/合集/动态，多 Cookie 管理",
          "1.2.0")
class SaveAnyBilibiliDownloader(Star):

    # B 站短链域名（分享链接常见形式：https://b23.tv/xxxx）
    SHORT_URL_HOSTS = {
        "b23.tv",
        "bili2233.cn",
        "bili22.cn",
        "bili23.cn",
        "bili33.cn",
    }
    # 匹配 B 站链接（允许不带 http/https 前缀），只匹配 ASCII URL 字符，
    # 避免把链接后面的中文分享文案一起吞掉。
    BILI_URL_PATTERN = re.compile(
        r"(?<![A-Za-z0-9\-._/])(?:https?://)?(?:[A-Za-z0-9-]+\.)*"
        r"(?:bilibili\.com|b23\.tv|bili2233\.cn|bili22\.cn|bili23\.cn|bili33\.cn)"
        r"/[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*",
        re.I,
    )
    URL_TRAILING_CHARS = " \t\r\n\"'`.,;:!?，。；：！？、）)]}】》」』”’…"

    FFMPEG_MIRROR_CANDIDATES = [
        "https://raw.ihtw.moe/",
        "https://gh.zwy.one/",
        "https://gh.llkk.cc/",
        "https://ghfast.top/",
        "https://gh.h233.eu.org/",
        "https://gh-proxy.com/",
        "https://ghproxy.net/",
        "https://gh.xxooo.cf/",
        "https://ghfile.geekertao.top/",
        "https://ghproxy.cxkpro.top/",
        "https://git.yylx.win/",
        "https://cdn.crashmc.com/",
        "https://githubproxy.cc/",
    ]

    FFMPEG_CONNECT_TIMEOUT = 8
    FFMPEG_READ_TIMEOUT = 30
    FFMPEG_TOTAL_TIMEOUT = 90
    # 正常压缩包远大于该值；低于它说明拿到的是错误页或截断响应
    FFMPEG_MIN_ARCHIVE_SIZE = 1024 * 1024

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.base_dir = Path(__file__).parent
        self.data_dir = self._get_data_dir(context)

        # 健壮地创建数据目录，任何异常都退回临时目录
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            if not self.data_dir.is_dir():
                raise NotADirectoryError(f"{self.data_dir} 不是目录")
        except Exception as e:
            logger.error(f"[路径] 创建数据目录失败: {e}")
            fallback = Path(tempfile.gettempdir()) / "saveany_bilibili_downloader"
            try:
                fallback.mkdir(parents=True, exist_ok=True)
            except Exception:
                fallback = Path.home() / ".saveany_bilibili_downloader"
                fallback.mkdir(parents=True, exist_ok=True)
            logger.warning(f"[路径] 已回退到: {fallback}")
            self.data_dir = fallback

        logger.info(f"[路径] 插件数据目录: {self.data_dir}")

        # 默认落到插件数据目录下，便于数据迁移与审计（不再依赖进程工作目录）
        self.download_dir = self._resolve_download_dir(None)
        self.max_size_mb = 2000.0
        self.quality = 80
        self.ffmpeg_path = None
        self.ffmpeg_status = {"status": "idle", "message": "尚未检测 FFmpeg"}
        self.ffmpeg_downloading = False

        if config:
            self.download_dir = self._resolve_download_dir(config.get("download_dir"))
            self.max_size_mb = float(config.get("max_size_mb", self.max_size_mb))
            self.quality = int(config.get("quality", self.quality))

        self._load_plugin_config()
        self.cookies = []
        self.bilibili_cookie = ""
        self._load_cookies()

        self.session = None
        self.plugin_api = PluginAPI(self)
        self._tasks = set()
        self._recent_requests = {}

    # ============ 核心修复：路径探测 ============
    def _get_data_dir(self, context: Context) -> Path:
        """多候选路径探测，避免 ASTRBOT_DATA_DIR 拼接错误。"""
        PLUGIN_DIR_NAME = "saveany_bilibili_downloader"

        # 1. 优先用 context 提供的官方方法
        for attr in ["get_plugin_data_dir", "get_data_dir"]:
            if hasattr(context, attr):
                try:
                    dir_path = getattr(context, attr)()
                    if dir_path:
                        p = Path(dir_path)
                        # 如果返回的就是插件专属目录，直接用
                        if p.name == PLUGIN_DIR_NAME:
                            return p
                        return p / PLUGIN_DIR_NAME
                except Exception as e:
                    logger.warning(f"[路径] context.{attr}() 调用失败: {e}")

        # 2. 遍历环境变量候选
        env_candidates = []
        for env_key in ("ASTRBOT_DATA_DIR", "ASTRBOT_ROOT", "ASTRBOT_HOME"):
            env_val = os.environ.get(env_key)
            if not env_val:
                continue
            base = Path(env_val)
            # 兼容：环境变量可能指向 AstrBot 根目录、data 目录、.astrbot 目录
            env_candidates.extend([
                base / "plugin_data" / PLUGIN_DIR_NAME,
                base / "data" / "plugin_data" / PLUGIN_DIR_NAME,
                base / ".astrbot" / "plugin_data" / PLUGIN_DIR_NAME,
                base / ".astrbot" / "data" / "plugin_data" / PLUGIN_DIR_NAME,
            ])

        # 3. 兜底候选：相对当前工作目录 & 用户主目录
        env_candidates.extend([
            Path.cwd() / "data" / "plugin_data" / PLUGIN_DIR_NAME,
            Path.cwd() / "plugin_data" / PLUGIN_DIR_NAME,
            Path.home() / ".astrbot" / "data" / "plugin_data" / PLUGIN_DIR_NAME,
            Path.home() / "AstrBot" / "data" / "plugin_data" / PLUGIN_DIR_NAME,
        ])

        # 依次尝试：能创建或已存在且是目录就返回
        first_error = None
        for cand in env_candidates:
            try:
                if cand.exists():
                    if cand.is_dir():
                        return cand
                    else:
                        # 存在但是文件 → 报错继续试下一个
                        logger.warning(f"[路径] {cand} 存在但不是目录，跳过")
                        continue
                # 不存在：尝试递归创建（先测父目录是否能创建）
                cand.mkdir(parents=True, exist_ok=True)
                if cand.is_dir():
                    return cand
            except Exception as e:
                if first_error is None:
                    first_error = e
                continue

        # 4. 全部失败 → 抛出，让 __init__ 的 fallback 接住
        raise RuntimeError(f"无法确定插件数据目录，最后的错误: {first_error}")

    def _resolve_download_dir(self, value) -> str:
        """把配置里的下载目录解析成绝对路径。

        - 留空 → 插件数据目录下的 downloads
        - 相对路径 → 统一挂到插件数据目录下，避免跟随进程工作目录漂移
        - 绝对路径 / ~ 开头 → 原样展开
        """
        raw = str(value or "").strip()
        if not raw:
            return str(self.data_dir / "downloads")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.data_dir / path
        return str(path)

    def _load_plugin_config(self):
        cfg_file = self.data_dir / "plugin_config.json"
        if cfg_file.exists():
            try:
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
                self.download_dir = self._resolve_download_dir(data.get("download_dir"))
                self.max_size_mb = float(data.get("max_size_mb", self.max_size_mb))
                self.quality = int(data.get("quality", self.quality))
            except Exception as e:
                logger.error(f"加载插件本地配置失败: {e}")

    def _save_plugin_config(self):
        cfg_file = self.data_dir / "plugin_config.json"
        # 数据目录内的路径按相对形式保存，整体迁移 data 目录后仍可用
        try:
            stored_dir = str(Path(self.download_dir).relative_to(self.data_dir))
        except Exception:
            stored_dir = self.download_dir
        data = {
            "download_dir": stored_dir,
            "max_size_mb": self.max_size_mb,
            "quality": self.quality,
        }
        cfg_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_cookies(self):
        cookie_file = self.data_dir / "cookies.json"
        if cookie_file.exists():
            try:
                self.cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
            except Exception:
                self.cookies = []
        self._select_best_cookie()

    def _save_cookies(self):
        cookie_file = self.data_dir / "cookies.json"
        cookie_file.write_text(json.dumps(self.cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        self._select_best_cookie()

    def _select_best_cookie(self):
        enabled = [c for c in self.cookies if c.get("enabled", True)]
        enabled.sort(key=lambda x: x.get("priority", 100))
        self.bilibili_cookie = enabled[0]["cookie"] if enabled else ""

    def _check_ffmpeg_local(self):
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            self.ffmpeg_path = system_ffmpeg
            self.ffmpeg_status = {"status": "ready", "message": "已检测到系统自带的 FFmpeg"}
            return

        bin_dir = self.data_dir / "bin"
        try:
            bin_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        ffmpeg_exe = bin_dir / "ffmpeg.exe" if os.name == 'nt' else bin_dir / "ffmpeg"
        if ffmpeg_exe.exists():
            self.ffmpeg_path = str(ffmpeg_exe)
            self.ffmpeg_status = {"status": "ready", "message": "已检测到插件目录下的 FFmpeg"}
            if os.name != 'nt':
                try:
                    os.chmod(self.ffmpeg_path, 0o755)
                except Exception:
                    pass
        else:
            self.ffmpeg_status = {"status": "idle", "message": "未检测到 FFmpeg，可点击右侧按钮一键下载"}

    async def initialize(self):
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=15)
        self.session = aiohttp.ClientSession(timeout=timeout)
        try:
            os.makedirs(self.download_dir, exist_ok=True)
        except Exception as e:
            logger.warning(f"[初始化] 创建下载目录失败: {e}")
        self.plugin_api.register(self.context)
        logger.info(f"B站下载插件已启动 (1.2.0) 下载目录: {self.download_dir}")
        self._check_ffmpeg_local()

    async def trigger_ffmpeg_download(self):
        if self.ffmpeg_status["status"] == "downloading":
            return
        self.ffmpeg_status = {"status": "downloading", "message": "正在下载 FFmpeg，请耐心等待..."}

        async def download_task():
            try:
                bin_dir = self.data_dir / "bin"
                bin_dir.mkdir(parents=True, exist_ok=True)

                if os.name == 'nt':
                    github_url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
                    temp_file = self.data_dir / "ffmpeg_temp.zip"
                    target_name = "ffmpeg.exe"
                    extract_func = self._extract_zip
                else:
                    github_url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
                    temp_file = self.data_dir / "ffmpeg_temp.tar.xz"
                    target_name = "ffmpeg"
                    extract_func = self._extract_tar

                # 官方源优先，失败后再走镜像；镜像仅作为网络受限时的备用通道
                sources = [""] + list(self.FFMPEG_MIRROR_CANDIDATES)
                total_sources = len(sources)
                last_error = None

                for idx, mirror in enumerate(sources, start=1):
                    full_url = github_url if not mirror else mirror + github_url
                    source_label = mirror or "GitHub 官方源"
                    self.ffmpeg_status = {
                        "status": "downloading",
                        "message": f"[{idx}/{total_sources}] 正在通过 {source_label} 下载..."
                    }
                    logger.info(f"[FFmpeg] [{idx}/{total_sources}] 尝试来源: {source_label}")
                    start_time = time.time()
                    try:
                        await asyncio.wait_for(
                            self._download_file_async(full_url, temp_file),
                            timeout=self.FFMPEG_TOTAL_TIMEOUT,
                        )
                        # 拒绝空包 / 错误页 / 被截断的响应，避免把垃圾数据当二进制执行
                        size = temp_file.stat().st_size if temp_file.exists() else 0
                        if size < self.FFMPEG_MIN_ARCHIVE_SIZE:
                            raise Exception(f"下载内容异常（仅 {size} 字节）")
                        elapsed = time.time() - start_time
                        logger.info(f"[FFmpeg] {source_label} 下载成功 ({elapsed:.1f}s, {size / 1048576:.1f} MB)")
                        break
                    except asyncio.TimeoutError:
                        elapsed = time.time() - start_time
                        last_error = f"超时 ({elapsed:.1f}s)"
                        logger.warning(f"[FFmpeg] {source_label} 超时，切换下一个")
                    except Exception as e:
                        last_error = str(e)
                        logger.warning(f"[FFmpeg] {source_label} 失败: {e}")
                    if temp_file.exists():
                        try:
                            temp_file.unlink()
                        except Exception:
                            pass
                else:
                    raise Exception(f"所有来源均失败: {last_error}")

                # 解压前后各校验一次：压缩包结构完整 + 二进制可正常执行
                target_path = bin_dir / target_name
                await asyncio.to_thread(extract_func, temp_file, bin_dir, target_name)
                if os.name != 'nt':
                    try:
                        os.chmod(target_path, 0o755)
                    except Exception:
                        pass
                if not target_path.exists():
                    raise Exception("解压完成但未找到可执行文件")
                if not await self._verify_ffmpeg_binary(str(target_path)):
                    try:
                        target_path.unlink()
                    except Exception:
                        pass
                    raise Exception("FFmpeg 校验失败（文件无法执行或内容不完整）")
                self._check_ffmpeg_local()
                if self.ffmpeg_status["status"] != "ready":
                    raise Exception("FFmpeg 校验通过但未能加载")
                logger.info("[FFmpeg] 二进制校验通过")
            except Exception as e:
                logger.error(f"FFmpeg 下载失败: {e}")
                self.ffmpeg_status = {"status": "error", "message": f"下载失败: {str(e)}"}
                for leftover in (self.data_dir / "ffmpeg_temp.zip", self.data_dir / "ffmpeg_temp.tar.xz"):
                    try:
                        if leftover.exists():
                            leftover.unlink()
                    except Exception:
                        pass

        asyncio.create_task(download_task())

    async def _download_file_async(self, url, dest):
        timeout = aiohttp.ClientTimeout(total=None, connect=self.FFMPEG_CONNECT_TIMEOUT, sock_read=self.FFMPEG_READ_TIMEOUT)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                with open(dest, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        f.write(chunk)

    def _extract_zip(self, temp_file, dest_dir, target_name):
        with zipfile.ZipFile(temp_file, 'r') as z:
            # CRC 校验，避免使用下载过程中损坏或被替换的压缩包
            if z.testzip() is not None:
                raise RuntimeError("ZIP 压缩包 CRC 校验失败")
            for filename in z.namelist():
                # 只取 basename，不按归档内路径落盘，杜绝路径穿越
                if os.path.basename(filename) == target_name:
                    with z.open(filename) as source, open(dest_dir / target_name, 'wb') as target:
                        shutil.copyfileobj(source, target)
                    break
            else:
                raise RuntimeError(f"压缩包中未找到 {target_name}")
        try:
            os.remove(temp_file)
        except Exception:
            pass

    def _extract_tar(self, temp_file, dest_dir, target_name):
        with tarfile.open(temp_file, "r:xz") as tar:
            for member in tar.getmembers():
                # 用 extractfile 读取流后写到固定目标名，不调用 tar.extract，
                # 因而归档内的路径/符号链接既不会被创建也不会影响目标位置
                if member.isfile() and os.path.basename(member.name) == target_name:
                    source = tar.extractfile(member)
                    if source is None:
                        raise RuntimeError(f"无法读取归档成员 {member.name}")
                    with source, open(dest_dir / target_name, 'wb') as target:
                        shutil.copyfileobj(source, target)
                    break
            else:
                raise RuntimeError(f"压缩包中未找到 {target_name}")
        try:
            os.remove(temp_file)
        except Exception:
            pass

    async def _verify_ffmpeg_binary(self, ffmpeg_path: Optional[str]) -> bool:
        """实际执行一次 `ffmpeg -version`，确认文件是可用且完整的 ffmpeg。"""
        if not ffmpeg_path:
            return False
        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    ffmpeg_path, "-version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=15,
            )
            stdout, _ = await proc.communicate()
            return proc.returncode == 0 and b"ffmpeg version" in (stdout or b"").lower()
        except Exception as e:
            logger.warning(f"[FFmpeg] 校验执行失败: {e}")
            return False

    async def _merge_with_ffmpeg(self, video_path: Path, audio_path: Path, output_path: Path):
        ffmpeg = self.find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("未找到 FFmpeg。请在 WebUI 点击按钮下载，或将 ffmpeg.exe 放到程序同目录。")
        cmd = [ffmpeg, "-y", "-i", str(video_path), "-i", str(audio_path), "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", "-movflags", "+faststart", str(output_path)]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="ignore")[-800:]
            raise RuntimeError(f"FFmpeg 合并失败：{err}")

    def find_ffmpeg(self) -> Optional[str]:
        return self.ffmpeg_path

    @filter.command("saveany")
    async def saveany(self, event: AstrMessageEvent, url: str):
        if not url:
            await event.send(event.plain_result("请提供一个 B 站链接或 BV 号。"))
            return
        await self.download_and_send(event, url)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        # 使用 AstrBot 内置唤醒判断：只有 @ 机器人、带唤醒词或私聊消息才会处理，
        # 不再对群聊中未 @ 机器人的普通 B 站链接自动下载。
        if not event.is_at_or_wake_command:
            return
        message = event.message_str.strip()
        # /saveany 交给指令处理器；唤醒前缀被框架剥离后消息可能以 "saveany" 开头
        if re.match(r"^/?saveany(?:\s|$)", message):
            return
        url_or_bvid = self.extract_bvid_or_url(message)
        if not url_or_bvid:
            return
        event.stop_event()
        await self.download_and_send(event, url_or_bvid)

    def extract_bvid_or_url(self, text: str) -> Optional[str]:
        match = self.BILI_URL_PATTERN.search(text)
        if match:
            url = match.group(0).rstrip(self.URL_TRAILING_CHARS)
            if url.lower().startswith(("http://", "https://")):
                return url
            return f"https://{url}"
        m = re.search(r'BV[0-9A-Za-z]{10}', text)
        if m:
            return m.group(0)
        m = re.search(r'av(\d+)', text, re.I)
        if m:
            return f"av{m.group(1)}"
        return None

    @classmethod
    def _is_short_url(cls, url: str) -> bool:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return False
        return host in cls.SHORT_URL_HOSTS

    async def _resolve_once(self, url: str, headers: dict) -> Optional[str]:
        """请求短链并返回重定向后的最终地址，HEAD 不可用时回退 GET。"""
        session = self.session
        created = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15, connect=8, sock_read=10)
            )
            created = True
        try:
            for method in ("HEAD", "GET"):
                async def _probe():
                    async with session.request(
                        method, url, allow_redirects=True, headers=headers
                    ) as resp:
                        return str(resp.url)

                try:
                    final_url = await asyncio.wait_for(_probe(), timeout=10.0)
                except Exception as e:
                    logger.debug(f"[短链] {method} 请求失败 {url}: {e}")
                    continue
                if final_url and final_url != url:
                    return final_url
        finally:
            if created:
                await session.close()
        return None

    async def resolve_short_url(self, url: str) -> Optional[str]:
        """把 b23.tv 等短链解析成真实的 B 站链接；非短链原样返回。"""
        if not self._is_short_url(url):
            return url

        headers = self._bili_headers()
        current = url
        for _ in range(3):
            final_url = await self._resolve_once(current, headers)
            if not final_url or final_url == current:
                break
            current = final_url
            if not self._is_short_url(current):
                logger.info(f"[短链] {url} -> {current}")
                return current

        # 网络解析失败时，b23.tv/BVxxx、b23.tv/avxxx 这类短链本身已带视频 ID，直接兜底
        m = re.search(r'/(BV[0-9A-Za-z]{10})', url)
        if m:
            logger.warning(f"[短链] 跳转解析失败，按路径提取视频 ID: {m.group(1)}")
            return m.group(1)
        m = re.search(r'/(av\d+)', url, re.I)
        if m:
            logger.warning(f"[短链] 跳转解析失败，按路径提取视频 ID: {m.group(1)}")
            return m.group(1).lower()

        logger.warning(f"[短链] 解析失败，未获取到跳转地址: {url}")
        return None

    def detect_input_type(self, text: str) -> dict:
        text = text.strip()
        if not text:
            return {"type": "unknown", "id": ""}
        if re.match(r'^(BV[0-9A-Za-z]{10})$', text):
            return {"type": "video", "id": text}
        if re.match(r'^av(\d+)$', text, re.I):
            return {"type": "video", "id": text}
        if re.match(r'^(cv\d+)$', text, re.I):
            return {"type": "article", "id": text}
        if re.match(r'^(ep|ss)(\d+)$', text, re.I):
            return {"type": "bangumi", "id": text}
        if re.match(r'^(\d+)$', text) and len(text) > 10:
            return {"type": "opus", "id": text}
        try:
            parsed = urlparse(text)
            path = parsed.path
            query = parse_qs(parsed.query)
        except Exception:
            return {"type": "unknown", "id": ""}
        for pattern, t in [(r'/bangumi/play/(ep|ss)(\d+)', "bangumi"), (r'/video/(BV[0-9A-Za-z]{10}|av\d+)', "video"), (r'/read/(cv\d+)', "article"), (r'/opus/(\d+)', "opus")]:
            m = re.search(pattern, path)
            if m:
                return {"type": t, "id": m.group(1) if t != 'bangumi' else f"{m.group(1)}{m.group(2)}"}
        if 'collectiondetail' in path and 'sid' in query:
            return {"type": "collection", "id": query['sid'][0], "mid": query.get('mid', [''])[0]}
        if 'favlist' in path and 'fid' in query:
            return {"type": "favlist", "id": query['fid'][0], "mid": query.get('mid', [''])[0]}
        m = re.search(r'BV[0-9A-Za-z]{10}', text)
        return {"type": "video", "id": m.group(0)} if m else {"type": "unknown", "id": ""}

    def _bili_headers(self, referer: str = "https://www.bilibili.com/") -> dict:
        h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Referer": referer}
        if self.bilibili_cookie:
            h["Cookie"] = self.bilibili_cookie
        return h

    def _format_desc(self, desc: str, max_len: int = 120) -> str:
        desc = re.sub(r'\s+', ' ', (desc or "").strip())
        return (desc[:max_len] + "...") if len(desc) > max_len else (desc or "（无简介）")

    def build_video_notify(self, info: dict) -> str:
        stat = info.get("stat", {}) or {}
        return f"正在下载：\n{info.get('title', '未知')}\n\nUP主：{info.get('owner', '未知')}\n简介：{self._format_desc(info.get('desc'))}\n目前播放量：{stat.get('view', 0):,}"

    def build_bangumi_notify(self, info: dict, ep_title: str, index: str) -> str:
        return f"正在下载番剧：\n{info.get('title', '未知')}\n集数：{index} / 共 {len(info.get('episodes', []))} 集\n\n本集标题：{ep_title or '（无标题）'}\n简介：{self._format_desc(info.get('desc'))}"

    async def _fetch_cover_to_temp(self, url: str) -> str:
        if not url: return ""
        url = "https:" + url if url.startswith("//") else url
        try:
            suffix = ".jpg"
            for ext in (".png", ".gif", ".webp", ".jpeg"):
                if ext in url.lower(): suffix = ext; break
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            async def fetch():
                async with self.session.get(url, headers=self._bili_headers()) as resp:
                    if resp.status == 200:
                        tmp.write(await resp.read())
                        return True
                    return False
            success = await asyncio.wait_for(fetch(), timeout=10.0)
            tmp.close()
            return tmp.name if success else ""
        except Exception as e:
            logger.warning(f"封面下载失败: {e}")
            return ""

    async def download_and_send(self, event: AstrMessageEvent, url_or_bvid: str):
        # /saveany 命令和 @ 触发都会走到这里，统一先处理短链
        url_or_bvid = await self.resolve_short_url(url_or_bvid)
        if not url_or_bvid:
            await event.send(
                event.plain_result("短链解析失败，请检查链接是否有效或稍后重试。")
            )
            return

        info = self.detect_input_type(url_or_bvid)
        if info["type"] == "unknown":
            await event.send(event.plain_result("无法识别的链接类型。"))
            return

        session_id = getattr(event, "unified_msg_origin", None) or getattr(event, "session_id", "default")
        key = (str(session_id), url_or_bvid.strip())
        now = time.time()
        last = self._recent_requests.get(key, 0)
        if now - last < 10:
            logger.warning(f"检测到 10 秒内重复请求，已忽略: {url_or_bvid}")
            return
        self._recent_requests[key] = now

        type_name = {"video": "视频", "bangumi": "番剧", "article": "专栏", "opus": "动态", "favlist": "收藏夹", "collection": "合集"}.get(info["type"], "内容")
        await event.send(event.plain_result(f"已接收您的{type_name}请求，正在后台解析和下载，请稍候..."))

        task = asyncio.create_task(self._background_download(event, info))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _background_download(self, event: AstrMessageEvent, info: dict):
        try:
            t = info["type"]
            logger.info(f"后台任务开始处理: type={t}, id={info.get('id')}")
            handlers = {
                "video": self._handle_video, "bangumi": self._handle_bangumi,
                "article": self._handle_article, "opus": self._handle_opus,
                "favlist": lambda e, i: self._handle_list(e, "favlist", i),
                "collection": lambda e, i: self._handle_list(e, "collection", i, info.get("mid", ""))
            }
            await handlers[t](event, info["id"])
        except Exception as e:
            logger.error(f"后台下载任务出错: {traceback.format_exc()}")
            await event.send(event.plain_result(f"下载过程中发生未捕获错误: {str(e)}"))

    async def _handle_video(self, event, bvid):
        info = await self.get_bilibili_video_info(bvid)
        if not info:
            await event.send(event.plain_result("获取视频信息失败，请查看控制台日志。"))
            return

        notify_text = self.build_video_notify(info)
        cover_path = ""
        if info.get("pic"):
            cover_path = await self._fetch_cover_to_temp(info["pic"])

        if cover_path:
            await event.send(event.chain_result([
                Comp.Image.fromFileSystem(path=cover_path),
                Comp.Plain(notify_text),
            ]))
        else:
            await event.send(event.plain_result(notify_text))

        url = await self.get_download_url(bvid, info.get("aid"), info.get("cid"))
        if not url:
            await event.send(event.plain_result("获取下载地址失败，请查看控制台日志。"))
            return
        path = await self.download_file(url, info["title"], None)
        if path and os.path.exists(path):
            await event.send(event.chain_result([Comp.Video.fromFileSystem(path=path)]))
        else:
            await event.send(event.plain_result("下载失败，可能文件过大或网络问题。"))

    async def _handle_bangumi(self, event, ep_or_ss):
        bangumi = await self.get_bangumi_info(ep_or_ss)
        if not bangumi:
            await event.send(event.plain_result("番剧信息获取失败，请查看控制台日志。"))
            return
        episodes = bangumi.get("episodes", [])
        if ep_or_ss.startswith("ep"):
            episodes = [e for e in episodes if e.get("ep_id") == int(ep_or_ss[2:])]
        if not episodes:
            await event.send(event.plain_result("未找到可下载的剧集。"))
            return
        if ep_or_ss.startswith("ss") and len(episodes) > 1:
            await event.send(event.plain_result(f"检测到整季共 {len(episodes)} 集，仅下载第 1 集。"))
            episodes = episodes[:1]

        for ep in episodes:
            notify_text = self.build_bangumi_notify(bangumi, ep.get('title'), ep.get('index'))
            cover_path = ""
            if bangumi.get("cover"):
                cover_path = await self._fetch_cover_to_temp(bangumi["cover"])

            if cover_path:
                await event.send(event.chain_result([
                    Comp.Image.fromFileSystem(path=cover_path),
                    Comp.Plain(notify_text),
                ]))
            else:
                await event.send(event.plain_result(notify_text))

            streams = await self.get_bangumi_playurl(ep["ep_id"], ep["cid"])
            if not streams or not streams.get("video_url"):
                await event.send(event.plain_result(f"第 {ep.get('index')} 集解析失败，请查看控制台输出。"))
                continue

            title = f"{bangumi['title']} - {ep.get('index')} {ep.get('title')}".strip()
            path = await self.download_file(streams["video_url"], title, streams.get("audio_url"))
            if path and os.path.exists(path):
                await event.send(event.chain_result([Comp.Video.fromFileSystem(path=path)]))
            else:
                await event.send(event.plain_result(f"第 {ep.get('index')} 集下载失败。"))

    async def get_bilibili_video_info(self, bvid: str) -> dict:
        api_url = f"https://api.bilibili.com/x/web-interface/view?{'aid' if bvid.lower().startswith('av') else 'bvid'}={bvid[2:] if bvid.lower().startswith('av') else bvid}"
        try:
            async def fetch():
                async with self.session.get(api_url, headers=self._bili_headers()) as resp:
                    return resp.status, await resp.text()
            status, raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            if data.get("code") != 0:
                logger.error(f"视频 API 返回错误: {data.get('message')}")
                return None
            vd = data.get("data") or data.get("result")
            if not vd:
                logger.error(f"视频 API 缺少 data 字段")
                return None
            return {"bvid": vd.get("bvid"), "aid": vd.get("aid"), "cid": vd.get("cid"), "title": vd.get("title"), "desc": vd.get("desc"), "pic": vd.get("pic"), "owner": vd.get("owner", {}).get("name"), "stat": vd.get("stat", {})}
        except Exception as e:
            logger.error(f"视频解析异常: {traceback.format_exc()}")
            return None

    async def get_bangumi_info(self, ep_or_ss: str) -> dict:
        url = f"https://api.bilibili.com/pgc/view/web/season?{'ep_id' if ep_or_ss.startswith('ep') else 'season_id'}={ep_or_ss[2:]}"
        referer = f"https://www.bilibili.com/bangumi/play/{ep_or_ss}"
        headers = self._bili_headers(referer=referer)
        try:
            async def fetch():
                async with self.session.get(url, headers=headers) as resp:
                    return resp.status, await resp.text()
            status, raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            if data.get("code") != 0:
                logger.error(f"番剧 API 返回错误: code={data.get('code')}, message={data.get('message')}")
                return None
            d = data.get("data") or data.get("result")
            if not d:
                logger.error(f"番剧 API 缺少 data 字段")
                return None
            raw_eps = d.get("episodes") or []
            for section in d.get("section", []) or []:
                raw_eps.extend(section.get("episodes") or [])
            episodes = []
            for ep in raw_eps:
                episodes.append({
                    "ep_id": ep.get("id"), "cid": ep.get("cid"), "aid": ep.get("aid", 0),
                    "title": ep.get("share_copy") or ep.get("long_title") or ep.get("title", ""),
                    "index": ep.get("title", ""),
                })
            return {"title": d.get("title", ""), "cover": d.get("cover", ""), "desc": d.get("evaluate", ""), "episodes": episodes}
        except Exception as e:
            logger.error(f"番剧解析异常: {traceback.format_exc()}")
            return None

    async def get_bangumi_playurl(self, ep_id: int, cid: int) -> dict:
        url = f"https://api.bilibili.com/pgc/player/web/playurl?ep_id={ep_id}&cid={cid}&qn={self.quality}&fnval=4048&fourk=1&otype=json"
        headers = self._bili_headers(referer=f"https://www.bilibili.com/bangumi/play/ep{ep_id}")
        try:
            async def fetch():
                async with self.session.get(url, headers=headers) as resp:
                    return resp.status, await resp.text()
            status, raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            if data.get("code") != 0:
                logger.error(f"番剧 playurl 返回错误: code={data.get('code')}, message={data.get('message')}")
                return None
            d = data.get("data") or data.get("result")
            if not d:
                logger.error(f"番剧 playurl 缺少 data 或 result 字段")
                return None

            result = {"video_url": None, "audio_url": None}
            if d.get("dash"):
                videos = d["dash"].get("video", [])
                audios = d["dash"].get("audio", [])
                if videos:
                    videos.sort(key=lambda v: v.get("id", 0), reverse=True)
                    result["video_url"] = videos[0].get("baseUrl")
                if audios:
                    audios.sort(key=lambda a: a.get("bandwidth", 0), reverse=True)
                    result["audio_url"] = audios[0].get("baseUrl")
                return result if result["video_url"] else None
            if d.get("durl"):
                result["video_url"] = d["durl"][0].get("url")
                return result
            return None
        except Exception as e:
            logger.error(f"番剧 playurl 请求异常: {traceback.format_exc()}")
            return None

    async def get_download_url(self, bvid: str, aid: int, cid: int) -> str:
        url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={self.quality}&otype=json&platform=html5&fnver=0&fnval=1"
        try:
            async def fetch():
                async with self.session.get(url, headers=self._bili_headers()) as resp:
                    return await resp.text()
            raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            if data.get("code") == 0:
                d = data.get("data") or data.get("result")
                if d and d.get("durl"):
                    return d["durl"][0]["url"]
        except Exception: pass
        for api in ["https://api.injahow.cn/bparse/", "https://jx.jsonplayer.com/player/"]:
            try:
                async def fetch_parse():
                    async with self.session.get(f"{api}?bv={bvid}&q={self.quality}") as resp:
                        return await resp.text()
                raw_text = await asyncio.wait_for(fetch_parse(), timeout=10.0)
                data = json.loads(raw_text)
                if data.get("url"): return data["url"]
                if data.get("data", {}).get("url"): return data["data"]["url"]
            except Exception: continue
        return None

    async def download_file(self, download_url: str, title: str, audio_url: str = None) -> str:
        safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:100]
        timestamp = int(time.time())

        if not audio_url:
            file_path = os.path.join(self.download_dir, f"{safe_title}_{timestamp}.mp4")
            ok = await self._download_single_file(download_url, file_path)
            return file_path if ok else None

        video_temp = os.path.join(self.download_dir, f".{safe_title}_{timestamp}_v.mp4")
        audio_temp = os.path.join(self.download_dir, f".{safe_title}_{timestamp}_a.mp4")
        final_path = os.path.join(self.download_dir, f"{safe_title}_{timestamp}.mp4")

        try:
            logger.info(f"开始下载视频流: {title}")
            if not await self._download_single_file(download_url, video_temp):
                return None
            logger.info(f"开始下载音频流: {title}")
            if not await self._download_single_file(audio_url, audio_temp):
                return None
            logger.info(f"开始合并音视频: {title}")
            if not self.ffmpeg_path:
                logger.error("未配置 FFmpeg")
                return None
            cmd = [self.ffmpeg_path, "-y", "-i", video_temp, "-i", audio_temp, "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", final_path]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.error(f"FFmpeg 合并失败: {stderr.decode('utf-8', errors='ignore')[-500:]}")
                return None
            logger.info(f"音视频合并成功: {final_path}")
            return final_path
        except Exception as e:
            logger.error(f"合并过程异常: {traceback.format_exc()}")
            return None
        finally:
            for temp_file in [video_temp, audio_temp]:
                if os.path.exists(temp_file):
                    try: os.remove(temp_file)
                    except: pass

    async def _download_single_file(self, url: str, file_path: str) -> bool:
        try:
            timeout = aiohttp.ClientTimeout(total=1800, connect=15, sock_read=30)
            async with aiohttp.ClientSession(timeout=timeout) as download_session:
                async with download_session.get(url, headers=self._bili_headers()) as resp:
                    if resp.status != 200:
                        logger.error(f"文件下载 HTTP 错误: {resp.status}")
                        return False
                    with open(file_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            f.write(chunk)
            return True
        except Exception as e:
            logger.error(f"文件下载异常: {traceback.format_exc()}")
            return False

    async def _handle_article(self, event, cv_id):
        article_id = cv_id[2:] if cv_id.lower().startswith("cv") else cv_id
        url = f"https://api.bilibili.com/x/article/view?id={article_id}"
        try:
            async def fetch():
                async with self.session.get(url, headers=self._bili_headers()) as resp:
                    return await resp.text()
            raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            d = data.get("data") or data.get("result")
            if data.get("code") != 0 or not d:
                logger.error(f"专栏 API 错误: code={data.get('code')}")
                await event.send(event.plain_result("专栏获取失败，请查看控制台日志。"))
                return
            await event.send(event.plain_result(f"正在获取专栏：\n{d.get('title')}\n作者：{d.get('author', {}).get('name')}\n摘要：{self._format_desc(d.get('summary'))}"))
            text = self._html_to_text(d.get("content", ""))
            txt_path = os.path.join(self.download_dir, f"{re.sub(r'[\\\\/*?:\"<>|]', '_', d.get('title', 'article'))[:80]}.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(f"{d.get('title')}\n作者：{d.get('author', {}).get('name')}\n链接：https://www.bilibili.com/read/cv{article_id}\n\n{text}")
            await event.send(event.plain_result(f"专栏正文已保存至：{txt_path}"))
        except Exception as e:
            logger.error(f"专栏处理异常: {traceback.format_exc()}")
            await event.send(event.plain_result("专栏处理失败，请查看控制台日志。"))

    async def _handle_opus(self, event, dynamic_id):
        url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?timezone_offset=-480&id={dynamic_id}"
        try:
            async def fetch():
                async with self.session.get(url, headers=self._bili_headers()) as resp:
                    return await resp.text()
            raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            item = data.get("data", {}).get("item") if data.get("code") == 0 else None
            if not item:
                logger.error(f"动态 API 错误: raw={raw_text}")
                await event.send(event.plain_result("动态获取失败，请查看控制台日志。"))
                return
            major = item.get("modules", {}).get("module_dynamic", {}).get("major") or {}
            archive = major.get("archive")
            if archive and archive.get("bvid"):
                await event.send(event.plain_result("检测到视频动态，正在下载视频..."))
                await self._handle_video(event, archive["bvid"]); return
            text = item.get("modules", {}).get("module_dynamic", {}).get("desc", {}).get("text", "")
            await event.send(event.plain_result(f"动态内容：\n{text or '（无文字）'}"))
            for it in (major.get("draw", {}) or {}).get("items", []):
                img = await self._fetch_cover_to_temp(it.get("src"))
                if img: await event.send(event.chain_result([Comp.Image.fromFileSystem(path=img)]))
        except Exception as e:
            logger.error(f"动态处理异常: {traceback.format_exc()}")
            await event.send(event.plain_result("动态处理失败，请查看控制台日志。"))

    async def _handle_list(self, event, kind: str, list_id: str, mid: str = ""):
        if kind == "favlist":
            url = f"https://api.bilibili.com/x/v3/fav/resource/list?media_id={list_id}&pn=1&ps=20&platform=web"
        else:
            if not mid: await event.send(event.plain_result("缺少 mid 参数。")); return
            url = f"https://api.bilibili.com/x/polymer/web-space/seasons_archives_list?mid={mid}&season_id={list_id}&page_num=1&page_size=30"
        try:
            async def fetch():
                async with self.session.get(url, headers=self._bili_headers()) as resp:
                    return await resp.text()
            raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            d = data.get("data") or data.get("result")
            if data.get("code") != 0 or not d:
                logger.error(f"列表 API 错误: raw={raw_text}")
                await event.send(event.plain_result("列表解析失败，请查看控制台日志。"))
                return
            items = [m for m in (d.get("medias") or d.get("archives") or []) if m.get("bvid")]
            await event.send(event.plain_result(f"共获取到 {len(items)} 个视频。\n如需下载，请单独发送 BV 号。"))
        except Exception as e:
            logger.error(f"列表处理异常: {traceback.format_exc()}")
            await event.send(event.plain_result("列表处理失败，请查看控制台日志。"))

    def _html_to_text(self, html: str) -> str:
        if not html: return ""
        html = re.sub(r'<[^>]+>', '', re.sub(r'<img[^>]*>', '[图片]', re.sub(r'</h[1-6]>', '\n\n', re.sub(r'<br\s*/?>', '\n', re.sub(r'</p>', '\n\n', html, flags=re.I), flags=re.I), flags=re.I), flags=re.I))
        return re.sub(r'\n{3,}', '\n\n', html.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")).strip()

    async def terminate(self):
        if self.session and not self.session.closed: await self.session.close()
        logger.info("B站下载插件已停止")
