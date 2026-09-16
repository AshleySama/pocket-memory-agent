from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from base64 import b64encode
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pocket_memory.model_download import ModelDownloadService


APP_FOLDER_NAME = "PocketMemory"
MIN_FREE_SPACE_BYTES = 3_200 * 1024 * 1024


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def default_install_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_FOLDER_NAME
    return Path.home() / "AppData" / "Local" / APP_FOLDER_NAME


def installer_icon_data_uri() -> str:
    """Load the included application mark without requiring web access."""
    icon = resource_path("payload/_internal/frontend/brand/pocket-memory-main-logo.png")
    try:
        return "data:image/png;base64," + b64encode(icon.read_bytes()).decode("ascii")
    except OSError:
        return ""


def choose_windows_folder(initial_directory: Path) -> str | None:
    """Open the supported WinForms folder picker on a dedicated STA thread.

    PyWebView's Windows folder picker reflects into a private WinForms API and
    silently returns ``None`` on newer Windows/.NET combinations. The public
    FolderBrowserDialog is less visually elaborate, but is supported and keeps
    the installer independent from Tk and PowerShell.
    """
    selected: dict[str, str | None] = {"path": None}
    failure: list[Exception] = []

    def show_dialog() -> None:
        try:
            import clr

            clr.AddReference("System.Windows.Forms")
            from System.Threading import ApartmentState, Thread, ThreadStart
            from System.Windows.Forms import DialogResult, FolderBrowserDialog

            dialog = FolderBrowserDialog()
            dialog.Description = "选择 Pocket Memory 的安装位置"
            dialog.SelectedPath = str(initial_directory)
            dialog.ShowNewFolderButton = True
            if dialog.ShowDialog() == DialogResult.OK:
                selected["path"] = str(dialog.SelectedPath)
        except Exception as exc:
            failure.append(exc)

    # FolderBrowserDialog requires an STA apartment. JS API calls are serviced
    # from worker threads, so create a dedicated native UI thread explicitly.
    import clr

    clr.AddReference("System.Threading")
    from System.Threading import ApartmentState, Thread, ThreadStart

    thread = Thread(ThreadStart(show_dialog))
    thread.SetApartmentState(ApartmentState.STA)
    thread.Start()
    thread.Join()
    if failure:
        raise RuntimeError(f"Windows 目录选择器启动失败：{failure[0]}") from failure[0]
    return selected["path"]


def copy_payload(source: Path, destination: Path, on_progress=None) -> None:
    """Install app files while retaining user data and a resumable Qwen download."""
    files = [path for path in source.rglob("*") if path.is_file()]
    total = len(files) or 1
    for index, path in enumerate(files, 1):
        relative = path.relative_to(source)
        if relative.parts[0] == "data" or relative.parts[:2] == ("models", "Qwen3-4B"):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        if on_progress:
            on_progress(index, total, relative)


def available_disk_space(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def create_desktop_shortcut(target: Path, working_directory: Path) -> Path:
    """Create a normal Windows shortcut without adding another launcher script."""
    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    shortcut = desktop / "Pocket Memory.lnk"
    script = (
        "$shell = New-Object -ComObject WScript.Shell; "
        "$link = $shell.CreateShortcut($args[0]); "
        "$link.TargetPath = $args[1]; "
        "$link.WorkingDirectory = $args[2]; "
        "$link.IconLocation = $args[1]; "
        "$link.Save()"
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script,
         str(shortcut), str(target), str(working_directory)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return shortcut


def format_size(value: int) -> str:
    return f"{value / 1024 / 1024 / 1024:.2f} GB"


class InstallerApi:
    def __init__(self) -> None:
        self.payload = resource_path("payload")
        self.destination = default_install_dir()
        self.window = None
        self.model_download: ModelDownloadService | None = None
        self._lock = threading.RLock()
        self._state = "ready"
        self._detail = "选择保存位置后，即可开始使用。"
        self._progress = 0.0
        self._error = ""
        self._shortcut_warning = ""

    def status(self) -> dict:
        with self._lock:
            payload = {
                "state": self._state,
                "detail": self._detail,
                "progress": round(self._progress, 1),
                "error": self._error,
                "shortcut_warning": self._shortcut_warning,
            }
        if self.model_download is not None:
            payload["model"] = self.model_download.status()
        return payload

    def default_destination(self) -> str:
        return str(self.destination)

    def choose_destination(self) -> str | None:
        if self.window is None:
            return None
        selected = choose_windows_folder(self.destination)
        if selected:
            return str(Path(selected).resolve())
        return None

    def start(self, destination_text: str, create_shortcut: bool, overwrite: bool = False) -> dict:
        destination = Path(str(destination_text or "")).expanduser()
        if not destination.name:
            return {"ok": False, "error": "请选择有效的保存位置。"}
        if not self.payload.is_dir():
            return {"ok": False, "error": "应用资源缺失，请重新下载此程序。"}
        if destination.exists() and any(destination.iterdir()) and not overwrite:
            return {"ok": False, "needs_confirm": True, "error": "目标目录已有文件。更新将保留笔记和已下载模型。"}
        with self._lock:
            if self._state in {"copying", "downloading", "verifying"}:
                return {"ok": False, "error": "准备工作正在进行。"}
            self.destination = destination
            self._state = "copying"
            self._detail = "正在准备 Pocket Memory。"
            self._progress = 0
            self._error = ""
            self._shortcut_warning = ""
        threading.Thread(
            target=self._install_in_background,
            args=(create_shortcut,),
            name="pocket-memory-installer",
            daemon=True,
        ).start()
        return {"ok": True}

    def cancel(self) -> None:
        if self.model_download and self.model_download.status()["can_cancel"]:
            self.model_download.cancel()
        if self.window is not None:
            self.window.destroy()

    def launch(self) -> dict:
        if self._state != "complete":
            return {"ok": False, "error": "准备尚未完成。"}
        executable = self.destination / "PocketMemory.exe"
        if not executable.is_file():
            return {"ok": False, "error": "PocketMemory.exe 不存在，应用文件可能已损坏。"}
        os.startfile(executable)  # type: ignore[attr-defined]
        if self.window is not None:
            self.window.destroy()
        return {"ok": True}

    def _install_in_background(self, create_shortcut: bool) -> None:
        try:
            # Delay the downloader import until the user actually starts work.
            # This keeps first paint independent of networking dependencies.
            from pocket_memory.model_download import ModelDownloadService

            self.destination.mkdir(parents=True, exist_ok=True)

            def report(index: int, total: int, relative: Path) -> None:
                with self._lock:
                    self._progress = index / total * 18
                    self._detail = f"正在准备：{relative.name}"

            copy_payload(self.payload, self.destination, report)
            self.model_download = ModelDownloadService(self.destination / "models")
            if not self.model_download.status()["installed"] and available_disk_space(self.destination) < MIN_FREE_SPACE_BYTES:
                raise OSError("保存位置所在磁盘可用空间不足。请至少保留 3.2 GB 后重新开始。")
            with self._lock:
                self._state = "downloading"
                self._detail = "正在下载 Qwen 4B 智能模型。"
            self.model_download.start()
            while True:
                status = self.model_download.status()
                state = status["state"]
                total = max(int(status["total_bytes"] or 0), 1)
                downloaded = int(status["downloaded_bytes"] or 0)
                with self._lock:
                    if state == "downloading":
                        self._state = "downloading"
                        self._progress = 18 + downloaded / total * 76
                        self._detail = f"已下载 {format_size(downloaded)} / {format_size(total)}，可安全中断后继续。"
                    elif state == "verifying":
                        self._state = "verifying"
                        self._progress = 95
                        self._detail = "下载完成，正在校验模型完整性。"
                if state == "installed":
                    break
                if state in {"failed", "paused"}:
                    raise OSError(status.get("error") or "模型下载未完成，请检查网络后重试。")
                threading.Event().wait(0.45)
            if create_shortcut:
                try:
                    create_desktop_shortcut(self.destination / "PocketMemory.exe", self.destination)
                except (OSError, subprocess.SubprocessError) as exc:
                    self._shortcut_warning = f"桌面快捷方式未能创建：{exc}"
            with self._lock:
                self._state = "complete"
                self._progress = 100
                self._detail = "准备完成，点击“打开 Pocket Memory”继续。"
        except Exception as exc:
            with self._lock:
                self._state = "failed"
                self._error = str(exc)
                self._detail = "准备尚未完成。"


INSTALLER_HTML = """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><style>
:root{--teal:#078b84;--teal-dark:#056e69;--ink:#111722;--muted:#6c737d;--line:#d9dde0;--soft:#f7faf9}*{box-sizing:border-box}html,body{height:100%}body{margin:0;background:#fff;color:var(--ink);font-family:'Microsoft YaHei UI','Microsoft YaHei',system-ui,sans-serif}.installer{min-height:100%;display:flex;flex-direction:column}.brand{display:flex;align-items:center;gap:46px;padding:42px 80px 35px;border-bottom:1px solid var(--line)}.brand-mark{width:132px;height:132px;flex:0 0 132px;border-radius:27px;background:#078b84 url('__APP_ICON__') center/cover no-repeat;box-shadow:inset 0 0 0 1px #ffffff33}.brand-copy h1{margin:0;font-size:66px;line-height:1.08;letter-spacing:0;font-weight:800}.brand-copy p{margin:16px 0 0;color:var(--muted);font-size:29px;letter-spacing:0}.workflow{padding:0 54px;flex:1}.step{display:grid;grid-template-columns:100px minmax(0,1fr) auto;align-items:center;column-gap:26px;min-height:122px;border-bottom:1px solid var(--line)}.step-download{min-height:182px}.step-number{display:grid;place-items:center;width:68px;height:68px;border-radius:50%;background:radial-gradient(circle at 65% 35%,#10a99e,#047670);color:#fff;font-size:39px;font-weight:700;box-shadow:0 7px 16px #05746f26}.step-title{font-size:35px;font-weight:700;letter-spacing:0}.step-complete{display:flex;align-items:center;gap:16px;color:var(--teal);font-size:27px;font-weight:600;white-space:nowrap}.checkmark{display:grid;place-items:center;width:44px;height:44px;border:4px solid var(--teal);border-radius:50%;font-size:29px;line-height:1}.download-main{min-width:0}.download-progress{display:flex;align-items:center;gap:42px;margin-top:22px}.progress{height:19px;flex:1;border-radius:999px;background:#e4e6e8;overflow:hidden;box-shadow:inset 0 1px 2px #00000011}.bar{height:100%;width:0;background:linear-gradient(90deg,#07958d,#057c76);border-radius:inherit;transition:width .25s ease}.percent{min-width:88px;color:var(--teal);font-size:36px;font-weight:700;text-align:right}.download-detail{margin:15px 0 0 6px;color:var(--muted);font-size:26px}.shortcut-wrap{display:flex;align-items:center;gap:16px;color:#202833;font-size:27px;white-space:nowrap}.shortcut-wrap input{appearance:none;width:39px;height:39px;border:3px solid var(--teal);border-radius:6px;background:#fff;display:grid;place-content:center;cursor:pointer}.shortcut-wrap input:checked::after{content:'✓';color:var(--teal);font-size:31px;font-weight:800;line-height:1}.location{display:grid;grid-template-columns:135px minmax(0,1fr) 180px;gap:26px;align-items:center;padding:34px 26px 40px}.location label{font-size:29px;font-weight:600}.location input{width:100%;height:66px;padding:0 23px;border:1px solid #c6ccd0;border-radius:8px;background:#fff;color:#17202a;font:26px 'Microsoft YaHei UI','Microsoft YaHei',sans-serif;outline:none}.location input:focus{border-color:var(--teal);box-shadow:0 0 0 3px #078b8420}.button{height:66px;border:2px solid var(--teal);border-radius:8px;background:#fff;color:var(--teal);font:26px 'Microsoft YaHei UI','Microsoft YaHei',sans-serif;cursor:pointer;transition:transform .16s ease,box-shadow .16s ease,background .16s ease}.button:hover{transform:translateY(-1px);box-shadow:0 7px 16px #057c7620}.button:focus-visible{outline:3px solid #5acbc3;outline-offset:2px}.footer{min-height:148px;display:flex;align-items:center;justify-content:space-between;gap:24px;padding:24px 54px;border-top:1px solid var(--line)}.privacy{display:flex;align-items:center;gap:28px;color:var(--muted);font-size:27px}.shield{display:grid;place-items:center;width:54px;height:64px;border:4px solid var(--teal);border-radius:48% 48% 52% 52% / 40% 40% 62% 62%;color:var(--teal);font-size:34px;font-weight:700}.primary{min-width:350px;border-color:var(--teal);background:linear-gradient(135deg,#06968e,#047d77);color:#fff;font-size:34px;font-weight:700;box-shadow:0 10px 22px #057c7630}.primary:disabled{cursor:wait;transform:none;box-shadow:none;opacity:.7}.message{min-height:26px;margin:-22px 0 0 126px;color:var(--muted);font-size:18px}.message.error{color:#ba3929}.message.warning{color:#94620d}.hidden{display:none!important}@media(max-width:1050px){.brand{padding:28px 40px;gap:26px}.brand-mark{width:92px;height:92px;flex-basis:92px}.brand-copy h1{font-size:46px}.brand-copy p{font-size:21px}.workflow{padding:0 34px}.step{grid-template-columns:75px minmax(0,1fr) auto;column-gap:18px}.step-title{font-size:27px}.location{grid-template-columns:110px minmax(0,1fr) 150px;padding-left:0;padding-right:0}.location label,.button{font-size:21px}.footer{padding:22px 34px}.privacy{font-size:21px}.primary{min-width:260px;font-size:25px}.download-detail{font-size:20px}}@media(max-width:760px){.brand-copy h1{font-size:36px}.brand-copy p{font-size:17px}.step{grid-template-columns:60px minmax(0,1fr);padding:18px 0}.step-number{width:50px;height:50px;font-size:28px}.step-title{font-size:22px}.step-complete,.shortcut-wrap{grid-column:2;font-size:18px}.location{grid-template-columns:1fr}.location input{font-size:18px}.footer{align-items:flex-start;flex-direction:column}.primary{width:100%}.message{margin:0}.download-progress{gap:12px}.percent{font-size:24px;min-width:60px}}
</style></head><body><main class='installer'><section class='brand'><div class='brand-mark' role='img' aria-label='Pocket Memory'></div><div class='brand-copy'><h1>Pocket Memory</h1><p>本地 AI 知识库，您的专属记忆助手</p></div></section><section class='workflow'><section class='step'><div class='step-number'>1</div><div class='step-title'>安装应用与本地检索组件</div><div class='step-complete'><span class='checkmark'>✓</span><span>已完成</span></div></section><section class='step step-download'><div class='step-number'>2</div><div class='download-main'><div class='step-title'>下载智能模型 Qwen3-4B</div><div class='download-progress'><div class='progress'><div id='bar' class='bar'></div></div><div id='percent' class='percent'>0%</div></div><p id='download-detail' class='download-detail'>0.00 GB / 2.50 GB，支持断点续传与校验</p></div></section><section class='step'><div class='step-number'>3</div><div class='step-title'>创建桌面快捷方式</div><label class='shortcut-wrap'><input id='shortcut' type='checkbox' checked><span>创建桌面快捷方式</span></label></section><section class='location'><label for='destination'>安装位置</label><input id='destination' aria-label='安装位置'><button class='button' id='choose' type='button'>更改位置</button></section><p id='message' class='message' aria-live='polite'></p></section><footer class='footer'><div class='privacy'><span class='shield'>✓</span><span>笔记与文件不会上传到网络</span></div><button class='button primary' id='install' type='button'>安装并下载模型</button><button class='button primary hidden' id='launch' type='button'>打开 Pocket Memory</button></footer></main><script>
const $=id=>document.getElementById(id);let bridgeReady=false;let polling=false;const getApi=()=>window.pywebview&&window.pywebview.api;const message=(text,kind='')=>{$('message').textContent=text||'';$('message').className='message '+kind};const formatSize=bytes=>((Number(bytes||0)/1024/1024/1024).toFixed(2)+' GB');function render(s){const model=s.model||{};const modelProgress=model.total_bytes?Math.min(100,(Number(model.downloaded_bytes||0)/Number(model.total_bytes))*100):0;const progress=['downloading','verifying','complete'].includes(s.state)?(s.state==='complete'?100:modelProgress):0;$('bar').style.width=progress.toFixed(1)+'%';$('percent').textContent=Math.round(progress)+'%';$('download-detail').textContent=`${formatSize(model.downloaded_bytes)} / ${formatSize(model.total_bytes||2497281120)}，支持断点续传与校验`;const busy=['copying','downloading','verifying'].includes(s.state);$('install').disabled=busy;$('install').textContent=s.state==='failed'?'重新安装并下载模型':'安装并下载模型';$('install').classList.toggle('hidden',s.state==='complete');$('launch').classList.toggle('hidden',s.state!=='complete');if(s.error)message(s.error,'error');else if(s.shortcut_warning)message(s.shortcut_warning,'warning');else if(busy)message(s.detail||'');else message('')};async function waitForBridge(timeout=3500){const started=Date.now();while(!getApi()&&Date.now()-started<timeout){await new Promise(resolve=>setTimeout(resolve,60))}return getApi()}async function refresh(){const api=getApi();if(!api)return;try{render(await api.status())}catch(error){message('安装器暂时无法读取状态，请稍后重试。','error')}}async function chooseDestination(){const api=await waitForBridge();if(!api){message('安装器仍在初始化，请稍候后再选择位置。','error');return}try{const selected=await api.choose_destination();if(selected)$('destination').value=selected}catch(error){const detail=error&&error.message?error.message:String(error);message(`无法打开目录选择窗口：${detail}。你也可以直接在安装位置中输入完整路径。`,'error')}}async function startInstall(){const api=await waitForBridge();if(!api){message('安装器仍在初始化，请稍候后再开始。','error');return}try{let result=await api.start($('destination').value,$('shortcut').checked,false);if(result.needs_confirm&&confirm(result.error))result=await api.start($('destination').value,$('shortcut').checked,true);if(!result.ok&&!result.needs_confirm)message(result.error,'error');await refresh()}catch(error){message('无法启动安装，请重试。','error')}}async function launch(){const api=await waitForBridge();if(!api){message('安装器仍在初始化，请稍候后再打开。','error');return}const result=await api.launch();if(!result.ok)message(result.error,'error')}function initialize(){if(bridgeReady||!getApi())return;bridgeReady=true;getApi().default_destination().then(path=>{$('destination').value=path;refresh()}).catch(()=>message('无法读取默认安装位置。','error'));if(!polling){polling=true;setInterval(refresh,450)}}function bootstrap(){initialize();if(!bridgeReady)setTimeout(bootstrap,60)}$('choose').addEventListener('click',chooseDestination);$('install').addEventListener('click',startInstall);$('launch').addEventListener('click',launch);window.addEventListener('pywebviewready',initialize);bootstrap();
</script></body></html>"""


def main() -> None:
    try:
        import webview
    except Exception as exc:
        # A packaged installer should include this dependency. Avoid a Python
        # traceback if a security product or a damaged download removes it.
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            0,
            f"程序组件无法启动：{exc}\n\n请重新下载程序，或联系管理员检查安全软件隔离记录。",
            "Pocket Memory 随手记",
            0x10,
        )
        return

    api = InstallerApi()
    window = webview.create_window(
        "安装 Pocket Memory",
        html=INSTALLER_HTML.replace("__APP_ICON__", installer_icon_data_uri()),
        js_api=api,
        width=1540,
        height=920,
        min_size=(1080, 720),
    )
    api.window = window
    webview.start()


if __name__ == "__main__":
    main()
