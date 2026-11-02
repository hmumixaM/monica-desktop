use reqwest::blocking::Client;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    thread,
    time::{Duration, Instant, SystemTime},
};
use tauri::{
    image::Image,
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::TrayIconBuilder,
    AppHandle, Manager, RunEvent, WindowEvent,
};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

struct RuntimeState {
    child: Mutex<Option<Child>>,
    data_root: PathBuf,
    logs_root: PathBuf,
    bundle_root: PathBuf,
    error: Mutex<Option<String>>,
    ui_health: Mutex<UiHealth>,
    quitting: AtomicBool,
    allow_exit: AtomicBool,
}

struct UiHealth {
    last_poll: Instant,
}

impl UiHealth {
    fn should_reload(&mut self, now: Instant, active: bool) -> bool {
        // Hidden/unfocused WebKit pages may throttle timers. Give them a full
        // grace period on return so a healthy unsaved form isn't discarded.
        if !active {
            self.last_poll = now;
            return false;
        }
        if now.duration_since(self.last_poll) > Duration::from_secs(30) {
            self.last_poll = now;
            return true;
        }
        false
    }
}

#[derive(Deserialize)]
struct ControlFile {
    port: u16,
    token: String,
    pid: u32,
}

struct TrayItems {
    status: MenuItem<tauri::Wry>,
    pause: MenuItem<tauri::Wry>,
    resume: MenuItem<tauri::Wry>,
    open: MenuItem<tauri::Wry>,
}

fn record_error(app: &AppHandle, message: String) {
    let state = app.state::<RuntimeState>();
    if let Ok(mut file) = log_file(&state.logs_root.join("desktop.log")) {
        let _ = writeln!(file, "{:?}: {}", SystemTime::now(), message);
    }
    if let Ok(mut error) = state.error.lock() {
        *error = Some(message);
    };
}

fn log_file(path: &Path) -> Result<File, String> {
    let mut options = OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options.open(path).map_err(|error| error.to_string())
}

fn show_settings(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("settings") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn read_control(state: &RuntimeState) -> Result<ControlFile, String> {
    let path = state.data_root.join("run/control.json");
    let metadata =
        fs::symlink_metadata(&path).map_err(|_| "本地服务正在启动，请稍候。".to_owned())?;
    if !metadata.is_file() || metadata.len() > 8192 {
        return Err("本地服务控制文件无效。请重新启动 Monica。".into());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if metadata.mode() & 0o077 != 0 || metadata.uid() != unsafe { libc::geteuid() } {
            return Err("本地服务控制文件权限无效。请查看日志。".into());
        }
    }
    let data = fs::read(&path).map_err(|error| error.to_string())?;
    let control: ControlFile =
        serde_json::from_slice(&data).map_err(|_| "本地服务控制文件尚未就绪。".to_owned())?;
    if control.port == 0 || control.token.len() < 24 {
        return Err("本地服务连接信息无效。".into());
    }
    let child = state
        .child
        .lock()
        .map_err(|_| "本地服务状态暂时不可用。".to_owned())?;
    if child.as_ref().map(|child| child.id()) != Some(control.pid) {
        return Err("本地服务正在启动，请稍候。".into());
    }
    Ok(control)
}

fn request_control(
    app: &AppHandle,
    command: &str,
    payload: Option<Value>,
) -> Result<Value, String> {
    let state = app.state::<RuntimeState>();
    let control = read_control(&state)?;
    let timeout = if command == "shutdown" {
        245
    } else if command == "status" {
        5
    } else {
        180
    };
    let client = Client::builder()
        .no_proxy()
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(timeout))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|error| error.to_string())?;
    let url = format!("http://127.0.0.1:{}/control/{}", control.port, command);
    let request = if let Some(body) = payload {
        client.post(&url).json(&body)
    } else {
        client.get(&url)
    };
    let response = request
        .bearer_auth(&control.token)
        .send()
        .map_err(|_| "无法连接本地服务。请稍候重试，或查看日志。".to_owned())?;
    let status = response.status();
    let value: Value = response
        .json()
        .map_err(|_| "本地服务返回了无法读取的结果。".to_owned())?;
    if !status.is_success() || value.get("ok") == Some(&Value::Bool(false)) {
        let message = value
            .get("error")
            .or_else(|| value.get("detail"))
            .and_then(Value::as_str)
            .unwrap_or("操作未完成，请查看日志。");
        return Err(message.to_owned());
    }
    Ok(value)
}

fn spawn_runtime(app: &AppHandle) -> Result<(), String> {
    let state = app.state::<RuntimeState>();
    let python = state.bundle_root.join("python/bin/python3");
    if !python.is_file() {
        return Err("应用缺少本地运行组件。请安装完整的 Monica 应用包。".into());
    }
    let log = log_file(&state.logs_root.join("runtime.log"))?;
    let stderr = log.try_clone().map_err(|error| error.to_string())?;
    let paths = [
        state.bundle_root.join("python/bin"),
        state.bundle_root.join("node/bin"),
        state.bundle_root.join("postgres/bin"),
        PathBuf::from("/usr/bin"),
        PathBuf::from("/bin"),
        PathBuf::from("/usr/sbin"),
        PathBuf::from("/sbin"),
    ];
    let mut command = Command::new(&python);
    command.env_clear();
    for name in ["HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL"] {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }
    command
        .env(
            "PATH",
            std::env::join_paths(paths).map_err(|error| error.to_string())?,
        )
        .env("PYTHONHOME", state.bundle_root.join("python"))
        .env("PYTHONPATH", state.bundle_root.join("desktop"))
        .env("PYTHONNOUSERSITE", "1")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONUNBUFFERED", "1")
        .arg("-m")
        .arg("monica_desktop")
        .arg("serve")
        .arg("--bundle-root")
        .arg(&state.bundle_root)
        .arg("--data-root")
        .arg(&state.data_root)
        .arg("--parent-pid")
        .arg(std::process::id().to_string())
        .current_dir(&state.data_root)
        .stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(stderr));
    let child = command
        .spawn()
        .map_err(|error| format!("无法启动本地服务：{}", error))?;
    *state
        .child
        .lock()
        .map_err(|_| "无法保存本地服务状态。".to_owned())? = Some(child);
    *state
        .error
        .lock()
        .map_err(|_| "无法保存本地服务状态。".to_owned())? = None;
    Ok(())
}

fn runtime_status(app: &AppHandle) -> Result<Value, String> {
    let state = app.state::<RuntimeState>();
    let mut value = match request_control(app, "status", None) {
        Ok(value) => value,
        Err(connection_error) => {
            let error = state.error.lock().ok().and_then(|error| error.clone());
            if let Some(error) = error {
                json!({"state":"error","error":error,"services":[],"sources":[]})
            } else {
                return Err(connection_error);
            }
        }
    };
    if state.quitting.load(Ordering::SeqCst) {
        value["state"] = json!("stopping");
    }
    if let Some(config) = value.get_mut("config").and_then(Value::as_object_mut) {
        config.insert(
            "autostart".into(),
            json!(app.autolaunch().is_enabled().unwrap_or(false)),
        );
    }
    Ok(value)
}

#[tauri::command]
async fn get_status(app: AppHandle) -> Result<Value, String> {
    if let Ok(mut health) = app.state::<RuntimeState>().ui_health.lock() {
        health.last_poll = Instant::now();
    }
    tauri::async_runtime::spawn_blocking(move || runtime_status(&app))
        .await
        .map_err(|error| error.to_string())?
}

#[tauri::command]
async fn control_command(app: AppHandle, command: String, payload: Value) -> Result<Value, String> {
    const ALLOWED: &[&str] = &[
        "save_config",
        "start",
        "pause",
        "resume",
        "stop",
        "connect_wechat",
        "test_ai",
        "source_login",
        "collect",
        "create_account",
    ];
    if !ALLOWED.contains(&command.as_str()) || !payload.is_object() {
        return Err("不支持的操作。".into());
    }
    tauri::async_runtime::spawn_blocking(move || request_control(&app, &command, Some(payload)))
        .await
        .map_err(|error| error.to_string())?
}

fn open_web(app: &AppHandle) -> Result<(), String> {
    let status = request_control(app, "status", None)?;
    let address = status
        .get("web_url")
        .and_then(Value::as_str)
        .ok_or("Web 界面尚未就绪，请稍候。")?;
    let url = url::Url::parse(address).map_err(|_| "本地 Web 地址无效。")?;
    if url.scheme() != "http"
        || !matches!(url.host_str(), Some("127.0.0.1") | Some("localhost"))
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_none()
    {
        return Err("本地 Web 地址无效。".into());
    }
    app.opener()
        .open_url(url.as_str(), None::<&str>)
        .map_err(|error| error.to_string())
}

#[tauri::command]
async fn open_monica(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || open_web(&app))
        .await
        .map_err(|error| error.to_string())?
}

#[tauri::command]
fn open_logs(app: AppHandle) -> Result<(), String> {
    let path = app.state::<RuntimeState>().logs_root.clone();
    app.opener()
        .open_path(path.to_string_lossy(), None::<&str>)
        .map_err(|error| error.to_string())
}

#[tauri::command]
async fn choose_directory(app: AppHandle) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .file()
            .set_title("选择微信数据目录")
            .blocking_pick_folder()
            .map(|file| {
                file.into_path()
                    .map(|path| path.to_string_lossy().into_owned())
                    .map_err(|error| error.to_string())
            })
            .transpose()
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
async fn choose_file(app: AppHandle) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .file()
            .set_title("选择 Chatlog 配置文件")
            .add_filter("JSON 配置文件", &["json"])
            .blocking_pick_file()
            .map(|file| {
                file.into_path()
                    .map(|path| path.to_string_lossy().into_owned())
                    .map_err(|error| error.to_string())
            })
            .transpose()
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
fn set_autostart(app: AppHandle, enabled: bool) -> Result<(), String> {
    if enabled {
        app.autolaunch().enable()
    } else {
        app.autolaunch().disable()
    }
    .map_err(|error| error.to_string())
}

fn begin_quit(app: &AppHandle) {
    if app
        .state::<RuntimeState>()
        .quitting
        .swap(true, Ordering::SeqCst)
    {
        return;
    }
    if let Some(items) = app.try_state::<TrayItems>() {
        let _ = items.status.set_text("正在保存进度并退出…");
        let _ = items.pause.set_enabled(false);
        let _ = items.resume.set_enabled(false);
    }
    let handle = app.clone();
    thread::spawn(move || {
        let started = Instant::now();
        if let Err(error) = request_control(&handle, "shutdown", Some(json!({}))) {
            record_error(&handle, format!("退出请求：{}", error));
        }
        loop {
            let state = handle.state::<RuntimeState>();
            let finished = state
                .child
                .lock()
                .ok()
                .and_then(|mut child| {
                    child
                        .as_mut()
                        .map(|child| child.try_wait().ok().flatten().is_some())
                })
                .unwrap_or(true);
            if finished {
                break;
            }
            if started.elapsed() >= Duration::from_secs(250) {
                record_error(
                    &handle,
                    "本地服务未能在等待时间内退出，已发送终止请求。".into(),
                );
                #[cfg(unix)]
                if let Ok(child) = state.child.lock() {
                    if let Some(child) = child.as_ref() {
                        unsafe {
                            libc::kill(child.id() as i32, libc::SIGTERM);
                        }
                    }
                }
                break;
            }
            thread::sleep(Duration::from_millis(250));
        }
        handle
            .state::<RuntimeState>()
            .allow_exit
            .store(true, Ordering::SeqCst);
        handle.exit(0);
    });
}

#[tauri::command]
fn quit_app(app: AppHandle) {
    begin_quit(&app);
}

fn tray_icon() -> Image<'static> {
    let mut rgba = vec![0_u8; 22 * 22 * 4];
    // A quiet, monochrome M that works with macOS's menu bar template tinting.
    for y in 4..18_usize {
        for x in 3..19_usize {
            let stroke = (3..6).contains(&x)
                || (16..19).contains(&x)
                || ((6..11).contains(&x) && (y as i32 - x as i32 + 2).abs() <= 1)
                || ((11..16).contains(&x) && (y as i32 + x as i32 - 20).abs() <= 1);
            if stroke {
                rgba[(y * 22 + x) * 4 + 3] = 255;
            }
        }
    }
    Image::new_owned(rgba, 22, 22)
}

fn install_tray(app: &AppHandle) -> tauri::Result<()> {
    let status = MenuItem::with_id(app, "status", "正在启动…", false, None::<&str>)?;
    let open = MenuItem::with_id(app, "open", "打开 Monica", true, None::<&str>)?;
    let settings = MenuItem::with_id(app, "settings", "设置…", true, None::<&str>)?;
    let pause = MenuItem::with_id(app, "pause", "暂停后台任务", true, None::<&str>)?;
    let resume = MenuItem::with_id(app, "resume", "恢复后台任务", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出 Monica", true, None::<&str>)?;
    let separator = PredefinedMenuItem::separator(app)?;
    let separator2 = PredefinedMenuItem::separator(app)?;
    let menu = Menu::with_items(
        app,
        &[
            &status,
            &open,
            &settings,
            &separator,
            &pause,
            &resume,
            &separator2,
            &quit,
        ],
    )?;
    app.manage(TrayItems {
        status,
        pause,
        resume,
        open,
    });
    TrayIconBuilder::with_id("monica")
        .icon(tray_icon())
        .icon_as_template(true)
        .tooltip("Monica · 在本机运行")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "settings" => show_settings(app),
            "quit" => begin_quit(app),
            "open" | "pause" | "resume" => {
                let app = app.clone();
                let id = event.id.as_ref().to_owned();
                thread::spawn(move || {
                    let result = if id == "open" {
                        open_web(&app).map(|_| Value::Null)
                    } else {
                        request_control(&app, &id, Some(json!({})))
                    };
                    if let Err(error) = result {
                        record_error(&app, error);
                        show_settings(&app);
                    }
                });
            }
            _ => {}
        })
        .build(app)?;
    Ok(())
}

fn monitor_runtime(app: AppHandle, autostart: bool) {
    thread::spawn(move || {
        if let Err(error) = spawn_runtime(&app) {
            record_error(&app, error);
            show_settings(&app);
            return;
        }
        let mut last_tick = SystemTime::now();
        let mut initialized = false;
        let mut restart_count = 0;
        loop {
            thread::sleep(Duration::from_secs(3));
            let state = app.state::<RuntimeState>();
            if state.quitting.load(Ordering::SeqCst) {
                return;
            }
            if let Some(window) = app.get_webview_window("settings") {
                let active =
                    window.is_visible().unwrap_or(false) && window.is_focused().unwrap_or(false);
                let recover = state
                    .ui_health
                    .lock()
                    .map(|mut health| health.should_reload(Instant::now(), active))
                    .unwrap_or(false);
                if recover {
                    // Native reload works even after WebKit's content process
                    // has died; evaluating JavaScript in that process doesn't.
                    let _ = window.reload();
                }
            }
            let now = SystemTime::now();
            if now.duration_since(last_tick).unwrap_or_default() > Duration::from_secs(45) {
                let _ = request_control(&app, "wake", Some(json!({})));
            }
            last_tick = now;
            let exited = state.child.lock().ok().and_then(|mut child| {
                child
                    .as_mut()
                    .and_then(|child| child.try_wait().ok().flatten())
            });
            if let Some(exit) = exited {
                record_error(&app, format!("本地服务意外退出（{}）。", exit));
                show_settings(&app);
                if restart_count >= 3 {
                    return;
                }
                restart_count += 1;
                thread::sleep(Duration::from_secs(3 * restart_count));
                if state.quitting.load(Ordering::SeqCst) {
                    return;
                }
                if let Err(error) = spawn_runtime(&app) {
                    record_error(&app, error);
                    return;
                }
                continue;
            }
            if let Ok(status) = request_control(&app, "status", None) {
                let state_name = status
                    .get("state")
                    .and_then(Value::as_str)
                    .unwrap_or("starting");
                if let Some(items) = app.try_state::<TrayItems>() {
                    let title = match state_name {
                        "running" | "ready" => "运行中",
                        "paused" => "后台任务已暂停",
                        "pausing" => "正在暂停…",
                        "stopped" => "本地服务已停止",
                        "stopping" => "正在停止…",
                        "error" => "需要处理 · 打开设置查看",
                        _ => "正在启动…",
                    };
                    let idle = status
                        .get("operation")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .is_empty();
                    let _ = items.status.set_text(title);
                    let _ = items.pause.set_enabled(state_name == "running" && idle);
                    let _ = items
                        .resume
                        .set_enabled(matches!(state_name, "paused" | "stopped") && idle);
                    let _ = items.open.set_enabled(
                        status
                            .get("web_url")
                            .and_then(Value::as_str)
                            .map(|url| !url.is_empty())
                            .unwrap_or(false),
                    );
                }
                if !initialized {
                    initialized = true;
                    if autostart && status.get("configured") == Some(&Value::Bool(false)) {
                        show_settings(&app);
                    }
                }
            }
        }
    });
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_settings(app)
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .macos_launcher(tauri_plugin_autostart::MacosLauncher::LaunchAgent)
                .arg("--autostart")
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            get_status,
            control_command,
            open_monica,
            open_logs,
            choose_directory,
            choose_file,
            set_autostart,
            quit_app
        ])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(|app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);
            // Foundation resolves the application container when App Sandbox is
            // enabled. $HOME can still point to the user's unrestricted home.
            #[cfg(target_os = "macos")]
            let home = PathBuf::from(objc2_foundation::NSHomeDirectory().to_string());
            #[cfg(not(target_os = "macos"))]
            let home = app.path().home_dir()?;
            let data_root = home.join("Library/Application Support/Monica");
            let logs_root = home.join("Library/Logs/Monica");
            fs::create_dir_all(&data_root)?;
            fs::create_dir_all(&logs_root)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(&data_root, fs::Permissions::from_mode(0o700))?;
                fs::set_permissions(&logs_root, fs::Permissions::from_mode(0o700))?;
            }
            let bundle_root = if cfg!(debug_assertions) {
                std::env::var_os("MONICA_DESKTOP_RUNTIME_ROOT")
                    .map(PathBuf::from)
                    .unwrap_or(app.path().resource_dir()?.join("runtime"))
            } else {
                app.path().resource_dir()?.join("runtime")
            };
            app.manage(RuntimeState {
                child: Mutex::new(None),
                data_root,
                logs_root,
                bundle_root,
                error: Mutex::new(None),
                ui_health: Mutex::new(UiHealth {
                    last_poll: Instant::now(),
                }),
                quitting: AtomicBool::new(false),
                allow_exit: AtomicBool::new(false),
            });
            install_tray(app.handle())?;
            let autostart = std::env::args().any(|arg| arg == "--autostart");
            if !autostart {
                show_settings(app.handle());
            }
            monitor_runtime(app.handle().clone(), autostart);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("could not start Monica desktop");
    app.run(|app, event| {
        #[cfg(target_os = "macos")]
        if let RunEvent::Reopen { .. } = &event {
            show_settings(app);
        }
        if let RunEvent::ExitRequested { api, .. } = event {
            if !app
                .state::<RuntimeState>()
                .allow_exit
                .load(Ordering::SeqCst)
            {
                api.prevent_exit();
                begin_quit(app);
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn foreground_renderer_without_status_poll_is_reloaded_with_backoff() {
        let start = Instant::now();
        let mut health = UiHealth { last_poll: start };
        assert!(!health.should_reload(start + Duration::from_secs(29), true));
        assert!(health.should_reload(start + Duration::from_secs(31), true));
        assert!(!health.should_reload(start + Duration::from_secs(32), true));
    }

    #[test]
    fn hidden_or_unfocused_view_gets_grace_after_sleep_to_preserve_edits() {
        let start = Instant::now();
        let mut health = UiHealth { last_poll: start };
        assert!(!health.should_reload(start + Duration::from_secs(3600), false));
        assert!(!health.should_reload(start + Duration::from_secs(3603), true));
        // The normal frontend poll resumes during the grace period.
        health.last_poll = start + Duration::from_secs(3604);
        assert!(!health.should_reload(start + Duration::from_secs(3630), true));
    }
}
