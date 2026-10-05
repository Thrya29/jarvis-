//! JARVIS floating overlay.
//!
//! A small see-through, always-on-top, click-through window in the top-right corner
//! that shows one page from the local JARVIS service (`/ui/overlay.html`). The page is
//! a remote origin to Tauri, so it gets no IPC/native access at all; everything native
//! (hotkeys, click-through, placement) happens here.
//!
//! Usage: `jarvis-overlay --url http://127.0.0.1:8765/ui/overlay.html#token=... [--parent-pid N]`
//!
//! Hotkeys: Ctrl+Alt+O toggles "interactive" (clickable) mode, Ctrl+Alt+H hides/shows.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::atomic::{AtomicBool, Ordering};

use tauri::{
    AppHandle, Manager, PhysicalPosition, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

const LABEL: &str = "overlay";
const WIDTH: f64 = 400.0;
const HEIGHT: f64 = 250.0;
/// Logical pixels from the right edge and from the top (clear of the title-bar area).
const MARGIN_RIGHT: f64 = 24.0;
const MARGIN_TOP: f64 = 64.0;

static INTERACTIVE: AtomicBool = AtomicBool::new(false);

struct Args {
    url: Url,
    parent_pid: Option<u32>,
}

/// Parse and validate the command line: the overlay only ever loads the local service.
fn parse_args(args: impl Iterator<Item = String>) -> Result<Args, String> {
    let mut url = None;
    let mut parent_pid = None;
    let mut it = args.skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--url" => url = it.next(),
            "--parent-pid" => parent_pid = it.next().and_then(|p| p.parse::<u32>().ok()),
            other => return Err(format!("unknown argument {other:?}")),
        }
    }
    let url = Url::parse(&url.ok_or("missing --url")?).map_err(|e| e.to_string())?;
    let local = matches!(url.host_str(), Some("127.0.0.1") | Some("localhost"));
    if url.scheme() != "http" || !local || url.port().is_none() {
        return Err("the overlay only loads the local JARVIS service".into());
    }
    Ok(Args { url, parent_pid })
}

fn shortcuts() -> (Shortcut, Shortcut) {
    let mods = Some(Modifiers::CONTROL | Modifiers::ALT);
    (
        Shortcut::new(mods, Code::KeyO),
        Shortcut::new(mods, Code::KeyH),
    )
}

/// Top-right of the primary monitor.
fn place(win: &WebviewWindow) -> tauri::Result<()> {
    if let Some(monitor) = win.primary_monitor()? {
        let scale = monitor.scale_factor();
        let origin = monitor.position();
        let size = monitor.size();
        let x = origin.x + size.width as i32 - ((WIDTH + MARGIN_RIGHT) * scale) as i32;
        let y = origin.y + (MARGIN_TOP * scale) as i32;
        win.set_position(PhysicalPosition::new(x, y))?;
    }
    Ok(())
}

/// Click-through by default; interactive mode accepts the mouse (to approve, open JARVIS).
fn set_interactive(app: &AppHandle, on: bool) {
    INTERACTIVE.store(on, Ordering::SeqCst);
    if let Some(win) = app.get_webview_window(LABEL) {
        let _ = win.set_ignore_cursor_events(!on);
        if on {
            let _ = win.show();
            let _ = win.set_focus();
        }
        let script = format!(
            "window.dispatchEvent(new CustomEvent('jarvis-overlay', {{ detail: {{ interactive: {on} }} }}))"
        );
        let _ = win.eval(&script);
    }
}

fn toggle_visible(app: &AppHandle) {
    if let Some(win) = app.get_webview_window(LABEL) {
        if win.is_visible().unwrap_or(true) {
            set_interactive(app, false);
            let _ = win.hide();
        } else {
            let _ = win.show();
        }
    }
}

/// Exit when JARVIS (the parent process) exits, however it exits.
#[cfg(windows)]
fn exit_with_parent(pid: u32, app: AppHandle) {
    use windows_sys::Win32::Foundation::CloseHandle;
    use windows_sys::Win32::System::Threading::{
        OpenProcess, WaitForSingleObject, INFINITE, PROCESS_SYNCHRONIZE,
    };
    std::thread::spawn(move || {
        // SAFETY: plain Win32 calls on a handle we own; a null handle means the parent
        // is already gone (or inaccessible), in which case we exit straight away.
        unsafe {
            let handle = OpenProcess(PROCESS_SYNCHRONIZE, 0, pid);
            if !handle.is_null() {
                WaitForSingleObject(handle, INFINITE);
                CloseHandle(handle);
            }
        }
        app.exit(0);
    });
}

#[cfg(not(windows))]
fn exit_with_parent(_pid: u32, _app: AppHandle) {}

fn main() {
    let args = match parse_args(std::env::args()) {
        Ok(args) => args,
        Err(err) => {
            eprintln!("jarvis-overlay: {err}");
            std::process::exit(2);
        }
    };

    tauri::Builder::default()
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    if event.state() != ShortcutState::Pressed {
                        return;
                    }
                    let (interact, hide) = shortcuts();
                    if *shortcut == interact {
                        set_interactive(app, !INTERACTIVE.load(Ordering::SeqCst));
                    } else if *shortcut == hide {
                        toggle_visible(app);
                    }
                })
                .build(),
        )
        .setup(move |app| {
            let win = WebviewWindowBuilder::new(app, LABEL, WebviewUrl::External(args.url.clone()))
                .title("JARVIS overlay")
                .inner_size(WIDTH, HEIGHT)
                .resizable(false)
                .decorations(false)
                .transparent(true)
                .shadow(false)
                .always_on_top(true)
                .skip_taskbar(true)
                .focused(false)
                .visible(false)
                .build()?;
            place(&win)?;
            win.set_ignore_cursor_events(true)?;
            win.show()?;

            let (interact, hide) = shortcuts();
            app.global_shortcut().register(interact)?;
            app.global_shortcut().register(hide)?;

            if let Some(pid) = args.parent_pid {
                exit_with_parent(pid, app.handle().clone());
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("JARVIS overlay failed to start");
}

#[cfg(test)]
mod tests {
    use super::parse_args;

    fn parse(list: &[&str]) -> Result<String, String> {
        let argv = std::iter::once("jarvis-overlay")
            .chain(list.iter().copied())
            .map(String::from);
        parse_args(argv).map(|a| a.url.to_string())
    }

    #[test]
    fn accepts_local_service_only() {
        assert!(parse(&["--url", "http://127.0.0.1:8765/ui/overlay.html#token=x"]).is_ok());
        assert!(parse(&["--url", "http://localhost:8765/ui/overlay.html"]).is_ok());
        assert!(parse(&["--url", "https://example.com/"]).is_err());
        assert!(parse(&["--url", "http://127.0.0.1.evil.com:80/"]).is_err());
        assert!(parse(&["--url", "file:///C:/x.html"]).is_err());
        assert!(parse(&["--url", "http://127.0.0.1/no-port"]).is_err());
        assert!(parse(&[]).is_err());
        assert!(parse(&["--bogus"]).is_err());
    }

    #[test]
    fn parent_pid_is_optional() {
        let argv = [
            "jarvis-overlay",
            "--url",
            "http://127.0.0.1:1/",
            "--parent-pid",
            "42",
        ];
        let args = parse_args(argv.iter().map(|s| s.to_string())).unwrap();
        assert_eq!(args.parent_pid, Some(42));
    }
}
