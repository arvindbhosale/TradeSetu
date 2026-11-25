// TradeSetu app: a native window over the desk, for desktop and Android.
//
// Desktop, local mode (default): starts the bundled desk backend on 127.0.0.1 and shows it.
// Server mode (the only mode on Android): shows a desk you host yourself (Dokploy, VPS).

use std::{
    fs,
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    sync::Mutex,
    time::Duration,
};
#[cfg(desktop)]
use std::{
    io::{Read, Seek, SeekFrom},
    net::TcpListener,
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicU32, Ordering},
    time::Instant,
};

use serde::{Deserialize, Serialize};
#[cfg(desktop)]
use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    webview::NewWindowResponse,
    RunEvent,
};
use tauri::{webview::DownloadEvent, AppHandle, Manager, Url, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;

#[cfg(desktop)]
const BACKEND_START_TIMEOUT: Duration = Duration::from_secs(120);

/// Phones have no Python to run the desk, so they only open a hosted one.
const LOCAL_SUPPORTED: bool = cfg!(desktop);

/// On phones, `target="_blank"` links do nothing in the WebView: send them through normal
/// navigation, where external sites (the Kite login) are handed to the system browser.
#[cfg(mobile)]
const MOBILE_LINK_SCRIPT: &str = r#"
document.addEventListener("click", function (e) {
  var a = e.target && e.target.closest ? e.target.closest("a[target=_blank]") : null;
  if (a && a.href && !a.hasAttribute("download")) { e.preventDefault(); location.href = a.href; }
}, true);
window.open = function (u) { if (u) location.href = new URL(u, location.href).href; return null; };
window.__deskNativeApp = "mobile";
"#;

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
struct Config {
    /// "local" runs the bundled backend; "server" opens `server_url`.
    mode: String,
    server_url: String,
}

impl Default for Config {
    fn default() -> Self {
        Config { mode: if LOCAL_SUPPORTED { "local" } else { "server" }.into(), server_url: String::new() }
    }
}

#[cfg(desktop)]
#[derive(Default)]
struct Backend {
    child: Option<Child>,
    port: u16,
}

#[derive(Default)]
struct AppState {
    #[cfg(desktop)]
    backend: Mutex<Backend>,
    /// Origin the desk is shown from (local backend or server), for link handling.
    desk_origin: Mutex<Option<String>>,
    last_download: Mutex<Option<PathBuf>>,
}

fn config_path(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_config_dir().map_err(|e| e.to_string())?;
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir.join("desktop.json"))
}

fn read_config(app: &AppHandle) -> Config {
    let mut config: Config = config_path(app)
        .ok()
        .and_then(|p| fs::read_to_string(p).ok())
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();
    if !LOCAL_SUPPORTED {
        config.mode = "server".into();
    }
    config
}

fn log_path(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_log_dir().map_err(|e| e.to_string())?;
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir.join("backend.log"))
}

fn origin_of(url: &Url) -> String {
    url.origin().ascii_serialization()
}

fn normalise_server_url(raw: &str) -> Result<String, String> {
    let raw = raw.trim();
    let url = Url::parse(raw).map_err(|_| "Enter a full address, for example https://desk.example.com".to_string())?;
    if url.scheme() != "https" && url.scheme() != "http" {
        return Err("The address must start with https:// or http://".into());
    }
    if url.host_str().is_none() {
        return Err("The address has no host name".into());
    }
    if !LOCAL_SUPPORTED && url.scheme() != "https" {
        // Android blocks plain http in release apps.
        return Err("On a phone the address must start with https://".into());
    }
    Ok(url.to_string())
}

#[cfg(desktop)]
fn backend_executable(app: &AppHandle) -> Result<PathBuf, String> {
    let name = if cfg!(windows) { "desk-backend.exe" } else { "desk-backend" };
    let dir = app.path().resource_dir().map_err(|e| e.to_string())?;
    let exe = dir.join("backend").join(name);
    if !exe.is_file() {
        return Err(format!("The desk backend is missing from this install ({}). Reinstall the app.", exe.display()));
    }
    Ok(exe)
}

/// The local backend's usual port. A fixed one keeps the Kite Connect redirect URL
/// (http://127.0.0.1:47800/broker/callback) the same at every start; any free port when it is taken.
#[cfg(desktop)]
const LOCAL_PORT: u16 = 47800;

#[cfg(desktop)]
fn free_port() -> Result<u16, String> {
    let listener = TcpListener::bind(("127.0.0.1", LOCAL_PORT))
        .or_else(|_| TcpListener::bind("127.0.0.1:0"))
        .map_err(|e| e.to_string())?;
    Ok(listener.local_addr().map_err(|e| e.to_string())?.port())
}

#[cfg(desktop)]
fn log_tail(path: &Path, bytes: u64) -> String {
    let Ok(mut file) = fs::File::open(path) else { return String::new() };
    let len = file.metadata().map(|m| m.len()).unwrap_or(0);
    let _ = file.seek(SeekFrom::Start(len.saturating_sub(bytes)));
    let mut text = String::new();
    let _ = file.read_to_string(&mut text);
    text
}

#[cfg(desktop)]
fn spawn_backend(app: &AppHandle, port: u16) -> Result<Child, String> {
    let exe = backend_executable(app)?;
    let log = log_path(app)?;
    let out = fs::File::create(&log).map_err(|e| format!("Cannot write {}: {e}", log.display()))?;
    let err = out.try_clone().map_err(|e| e.to_string())?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(meta) = fs::metadata(&exe) {
            if meta.permissions().mode() & 0o111 == 0 {
                let mut perms = meta.permissions();
                perms.set_mode(0o755);
                let _ = fs::set_permissions(&exe, perms);
            }
        }
    }

    let mut cmd = Command::new(&exe);
    cmd.arg("--port")
        .arg(port.to_string())
        .current_dir(exe.parent().unwrap_or(Path::new(".")))
        // The backend quits when this pipe closes, so it never outlives the app, even after a crash.
        .stdin(Stdio::piped())
        .stdout(Stdio::from(out))
        .stderr(Stdio::from(err))
        .env("PYTHONUNBUFFERED", "1")
        .env("TRADINGAGENTS_NO_UPDATE_CHECK", "1");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd.spawn().map_err(|e| format!("Could not start the desk backend: {e}"))
}

#[cfg(desktop)]
/// Starts the local backend if it is not running, and waits until it accepts connections.
fn ensure_local_backend(app: &AppHandle) -> Result<String, String> {
    let state = app.state::<AppState>();
    let port = {
        let mut backend = state.backend.lock().unwrap();
        let alive = match backend.child.as_mut() {
            Some(child) => matches!(child.try_wait(), Ok(None)),
            None => false,
        };
        if !alive {
            let port = free_port()?;
            backend.child = Some(spawn_backend(app, port)?);
            backend.port = port;
        }
        backend.port
    };

    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let started = Instant::now();
    loop {
        if TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok() {
            return Ok(format!("http://127.0.0.1:{port}/"));
        }
        {
            let mut backend = state.backend.lock().unwrap();
            if let Some(child) = backend.child.as_mut() {
                if let Ok(Some(status)) = child.try_wait() {
                    backend.child = None;
                    let tail = log_path(app).map(|p| log_tail(&p, 4000)).unwrap_or_default();
                    return Err(format!("The desk backend stopped ({status}).\n\n{tail}"));
                }
            }
        }
        if started.elapsed() > BACKEND_START_TIMEOUT {
            let tail = log_path(app).map(|p| log_tail(&p, 4000)).unwrap_or_default();
            return Err(format!("The desk backend did not start within {}s.\n\n{tail}", BACKEND_START_TIMEOUT.as_secs()));
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

#[cfg(desktop)]
fn stop_backend(app: &AppHandle) {
    let state = app.state::<AppState>();
    let mut backend = state.backend.lock().unwrap();
    if let Some(mut child) = backend.child.take() {
        drop(child.stdin.take()); // asks it to shut down cleanly
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if let Ok(Some(_)) = child.try_wait() {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

#[tauri::command]
fn get_config(app: AppHandle) -> Config {
    read_config(&app)
}

#[tauri::command]
fn save_config(app: AppHandle, mode: String, server_url: String) -> Result<Config, String> {
    let config = match mode.as_str() {
        "local" => Config { mode, server_url: server_url.trim().to_string() },
        "server" => Config { mode, server_url: normalise_server_url(&server_url)? },
        _ => return Err("Unknown mode".into()),
    };
    let text = serde_json::to_string_pretty(&config).map_err(|e| e.to_string())?;
    fs::write(config_path(&app)?, text).map_err(|e| e.to_string())?;
    Ok(config)
}

/// Returns the address to show: the server's, or the local backend's once it is up.
#[tauri::command]
async fn start(app: AppHandle) -> Result<String, String> {
    let config = read_config(&app);
    let url = if config.mode == "server" || !LOCAL_SUPPORTED {
        #[cfg(desktop)]
        stop_backend(&app);
        let url = normalise_server_url(&config.server_url)?;
        let probe = url.clone();
        tauri::async_runtime::spawn_blocking(move || server_reachable(&probe))
            .await
            .map_err(|e| e.to_string())??;
        url
    } else {
        #[cfg(desktop)]
        {
            let handle = app.clone();
            tauri::async_runtime::spawn_blocking(move || ensure_local_backend(&handle))
                .await
                .map_err(|e| e.to_string())??
        }
        #[cfg(mobile)]
        unreachable!()
    };
    let parsed = Url::parse(&url).map_err(|e| e.to_string())?;
    *app.state::<AppState>().desk_origin.lock().unwrap() = Some(origin_of(&parsed));
    Ok(url)
}

/// Fails fast with a readable message instead of a blank page when the server is down or the address is wrong.
fn server_reachable(url: &str) -> Result<(), String> {
    use std::net::ToSocketAddrs;
    let parsed = Url::parse(url).map_err(|e| e.to_string())?;
    let host = parsed.host_str().unwrap_or_default().to_string();
    let port = parsed.port_or_known_default().unwrap_or(443);
    let addrs: Vec<SocketAddr> = (host.as_str(), port)
        .to_socket_addrs()
        .map_err(|e| format!("Cannot find {host}: {e}. Check the address and your internet connection."))?
        .collect();
    for addr in &addrs {
        if TcpStream::connect_timeout(addr, Duration::from_secs(8)).is_ok() {
            return Ok(());
        }
    }
    Err(format!("Cannot reach {host}:{port}. Is the server running?"))
}

#[tauri::command]
fn log_file(app: AppHandle) -> Result<String, String> {
    Ok(log_path(&app)?.display().to_string())
}

#[tauri::command]
fn open_log(app: AppHandle) -> Result<(), String> {
    let path = log_path(&app)?;
    reveal(&app, path)
}

/// What the start screen offers: "desktop" (local or server) or "mobile" (server only).
#[tauri::command]
fn platform() -> &'static str {
    if LOCAL_SUPPORTED { "desktop" } else { "mobile" }
}

fn reveal(app: &AppHandle, path: PathBuf) -> Result<(), String> {
    #[cfg(desktop)]
    return app.opener().reveal_item_in_dir(path).map_err(|e| e.to_string());
    #[cfg(mobile)]
    {
        let _ = (app, path);
        Ok(())
    }
}

/// The app's own pages (the start screen), which differ by platform.
fn is_app_url(url: &Url) -> bool {
    matches!(url.scheme(), "tauri" | "asset")
        || (url.scheme() == "http" && url.host_str() == Some("tauri.localhost"))
        || (url.scheme() == "https" && url.host_str() == Some("tauri.localhost"))
}

fn is_desk_url(app: &AppHandle, url: &Url) -> bool {
    let origin = app.state::<AppState>().desk_origin.lock().unwrap().clone();
    origin.is_some_and(|o| o == origin_of(url))
}

fn open_external(app: &AppHandle, url: &Url) {
    if matches!(url.scheme(), "http" | "https" | "mailto") {
        let _ = app.opener().open_url(url.as_str(), None::<&str>);
    }
}

fn unique_download_path(dir: &Path, name: &str) -> PathBuf {
    let candidate = dir.join(name);
    if !candidate.exists() {
        return candidate;
    }
    let (stem, ext) = match name.rsplit_once('.') {
        Some((s, e)) if !s.is_empty() => (s.to_string(), format!(".{e}")),
        _ => (name.to_string(), String::new()),
    };
    (1..1000)
        .map(|i| dir.join(format!("{stem} ({i}){ext}")))
        .find(|p| !p.exists())
        .unwrap_or(candidate)
}

#[cfg(desktop)]
fn open_desk_window(app: &AppHandle, url: Url) {
    static NEXT: AtomicU32 = AtomicU32::new(1);
    let label = format!("page-{}", NEXT.fetch_add(1, Ordering::Relaxed));
    let handle = app.clone();
    let popup = app.clone();
    // The chart window (/charts, "Pop out" on the Charts tab) and any other desk page opened in a new window.
    let _ = WebviewWindowBuilder::new(app, label, WebviewUrl::External(url))
        .title(app.package_info().name.clone())
        .inner_size(1280.0, 820.0)
        .min_inner_size(480.0, 360.0)
        .on_navigation(move |u| navigation_allowed(&handle, u))
        .on_new_window(move |u, _features| {
            if is_desk_url(&popup, &u) {
                open_desk_window(&popup, u);
            } else {
                open_external(&popup, &u);
            }
            NewWindowResponse::Deny
        })
        .build();
}

fn navigation_allowed(app: &AppHandle, url: &Url) -> bool {
    // Phones: the WebView cannot save files, so the desk hands a file over at a one-off /dl/ link and the
    // phone's browser downloads it.
    if cfg!(mobile) && is_desk_url(app, url) && url.path().starts_with("/dl/") {
        open_external(app, url);
        return false;
    }
    if is_app_url(url) || is_desk_url(app, url) || url.scheme() == "about" || url.scheme() == "blob" || url.scheme() == "data" {
        return true;
    }
    open_external(app, url);
    false
}

fn build_main_window(app: &AppHandle) -> tauri::Result<()> {
    let nav = app.clone();
    let dl = app.clone();
    let builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .on_navigation(move |url| navigation_allowed(&nav, url));
    #[cfg(desktop)]
    let builder = {
        let popup = app.clone();
        builder
            .title(app.package_info().name.clone())
            .inner_size(1360.0, 880.0)
            .min_inner_size(1000.0, 680.0)
            .center()
            .on_new_window(move |url, _features| {
                if is_desk_url(&popup, &url) {
                    open_desk_window(&popup, url);
                } else {
                    open_external(&popup, &url);
                }
                NewWindowResponse::Deny
            })
    };
    #[cfg(mobile)]
    let builder = builder.initialization_script(MOBILE_LINK_SCRIPT);
    builder
        .on_download(move |_webview, event| {
            match event {
                DownloadEvent::Requested { destination, .. } => {
                    let name = destination
                        .file_name()
                        .map(|n| n.to_string_lossy().to_string())
                        .filter(|n| !n.is_empty())
                        .unwrap_or_else(|| "download".into());
                    if let Ok(dir) = dl.path().download_dir() {
                        let target = unique_download_path(&dir, &name);
                        *destination = target.clone();
                        *dl.state::<AppState>().last_download.lock().unwrap() = Some(target);
                    }
                }
                DownloadEvent::Finished { path, success, .. } => {
                    if success {
                        let saved = path.or_else(|| dl.state::<AppState>().last_download.lock().unwrap().clone());
                        if let Some(p) = saved {
                            let _ = reveal(&dl, p);
                        }
                    }
                }
                _ => {}
            }
            true
        })
        .build()?;
    Ok(())
}

#[cfg(desktop)]
fn build_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let menu = Menu::default(app)?;
    let connection = Submenu::with_items(
        app,
        "Desk",
        true,
        &[
            &MenuItem::with_id(app, "reload", "Reload", true, Some("CmdOrCtrl+R"))?,
            &MenuItem::with_id(app, "connection", "Connection settings…", true, None::<&str>)?,
            &PredefinedMenuItem::separator(app)?,
            &MenuItem::with_id(app, "log", "Show backend log", true, None::<&str>)?,
        ],
    )?;
    menu.append(&connection)?;
    Ok(menu)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default();
    #[cfg(desktop)]
    let builder = builder
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .menu(|app| build_menu(app))
        .on_menu_event(|app, event| {
            let Some(window) = app.get_webview_window("main") else { return };
            match event.id().as_ref() {
                "reload" => {
                    let _ = window.eval("location.reload()");
                }
                "connection" => {
                    // Back to the start screen, which shows the connection form.
                    if let Ok(mut url) = window.url() {
                        if !is_app_url(&url) {
                            url = app_start_url();
                        }
                        url.set_fragment(Some("settings"));
                        let _ = window.navigate(url);
                    }
                }
                "log" => {
                    if let Ok(path) = log_path(app) {
                        let _ = reveal(app, path);
                    }
                }
                _ => {}
            }
        });
    let app = builder
        .plugin(tauri_plugin_opener::init())
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![get_config, save_config, start, log_file, open_log, platform])
        .setup(|app| {
            build_main_window(app.handle())?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the app");
    app.run(|_app, _event| {
        #[cfg(desktop)]
        if let RunEvent::Exit = _event {
            stop_backend(_app);
        }
    });
}

#[cfg(desktop)]
fn app_start_url() -> Url {
    #[cfg(any(windows, target_os = "android"))]
    let base = "http://tauri.localhost/index.html";
    #[cfg(not(any(windows, target_os = "android")))]
    let base = "tauri://localhost/index.html";
    Url::parse(base).expect("static url")
}
