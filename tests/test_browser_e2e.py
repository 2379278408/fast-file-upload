from __future__ import annotations

import base64
import os
import re
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    CDPSession,
    Locator,
    Page,
    Request,
    expect,
    sync_playwright,
)


ROOT = Path(__file__).resolve().parents[1]
UPLOAD_TOKEN = "browser-e2e-token"
SERVER_START_TIMEOUT_SECONDS = 10.0
SERVER_STOP_TIMEOUT_SECONDS = 5.0
SERVER_START_ATTEMPTS = 5
EXPECTED_AUTH_CONSOLE_EXPLANATION = (
    "Chromium reports the expected unauthenticated GET /api/session 401 used to open "
    "the unlock dialog."
)
REQUIRED_CSP_DIRECTIVES = {
    "default-src": {"'self'"},
    "img-src": {"'self'", "data:"},
    "style-src": {"'self'"},
    "script-src": {"'self'"},
    "connect-src": {"'self'"},
    "base-uri": {"'self'"},
    "frame-ancestors": {"'none'"},
}
ROUTE_TITLES = {
    "transfer": "传输工作台",
    "files": "全部文件",
    "manage": "管理与设置",
}


@dataclass(slots=True)
class BrowserSession:
    page: Page
    context: BrowserContext
    base_url: str
    console_messages: list[str]
    page_errors: list[str]
    session_401_responses: list[str]
    allowed_console_messages: list[str]
    document_csp_headers: list[str]
    cdp_sessions: list[CDPSession]


class ServerStartError(RuntimeError):
    def __init__(self, message: str, *, address_in_use: bool = False) -> None:
        super().__init__(message)
        self.address_in_use = address_in_use


def _cleanup_cdp_network_session(cdp: CDPSession) -> None:
    try:
        cdp.send(
            "Network.emulateNetworkConditions",
            {
                "offline": False,
                "latency": 0,
                "downloadThroughput": -1,
                "uploadThroughput": -1,
                "connectionType": "none",
            },
        )
    except Exception:
        pass
    try:
        cdp.send("Network.disable")
    except Exception:
        pass
    try:
        cdp.detach()
    except Exception:
        pass


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_server(
    process: subprocess.Popen[bytes], base_url: str, log_path: Path
) -> None:
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    last_error = "server did not answer"
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            log = log_path.read_text(encoding="utf-8", errors="replace")
            normalized_log = log.lower()
            raise ServerStartError(
                f"browser E2E server exited with {return_code}:\n{log}",
                address_in_use=(
                    "eaddrinuse" in normalized_log
                    or "address already in use" in normalized_log
                    or "errno 98" in normalized_log
                ),
            )
        try:
            with urlopen(base_url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError as error:
            last_error = str(error)
        time.sleep(0.05)
    log = log_path.read_text(encoding="utf-8", errors="replace")
    raise ServerStartError(
        f"browser E2E server readiness timed out: {last_error}\n{log}"
    )


def _stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        process.wait(timeout=SERVER_STOP_TIMEOUT_SECONDS)
        return
    process.terminate()
    try:
        process.wait(timeout=SERVER_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=SERVER_STOP_TIMEOUT_SECONDS)


@contextmanager
def _run_live_server(
    tmp_path: Path,
    port_provider: Callable[[], int] = _unused_loopback_port,
) -> Iterator[str]:
    upload_dir = tmp_path / "uploads"
    database_path = tmp_path / "timeline.sqlite3"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "PYTHONUNBUFFERED": "1",
        "UPLOAD_TOKEN": UPLOAD_TOKEN,
        "UPLOAD_DIR": str(upload_dir),
        "DATABASE_PATH": str(database_path),
        "MAINTENANCE_INTERVAL_SECONDS": "3600",
    }
    last_error: ServerStartError | None = None
    for attempt in range(1, SERVER_START_ATTEMPTS + 1):
        port = port_provider()
        base_url = f"http://127.0.0.1:{port}"
        log_path = tmp_path / f"server-attempt-{attempt}.log"
        with log_path.open("wb") as server_log:
            process = subprocess.Popen(
                [
                    "python3",
                    "server.py",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--dir",
                    str(upload_dir),
                ],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=server_log,
                stderr=subprocess.STDOUT,
            )
            try:
                _wait_for_server(process, base_url, log_path)
            except ServerStartError as error:
                last_error = error
                _stop_server(process)
                if process.poll() is None:
                    raise ServerStartError(
                        "browser E2E failed process remained alive after cleanup"
                    ) from error
                if error.address_in_use and attempt < SERVER_START_ATTEMPTS:
                    continue
                raise
            except BaseException:
                _stop_server(process)
                raise
            try:
                yield base_url
                return
            finally:
                _stop_server(process)
    if last_error is not None:
        raise last_error
    raise ServerStartError("browser E2E server exhausted all startup attempts")


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[str]:
    try:
        with _run_live_server(tmp_path) as base_url:
            yield base_url
    except ServerStartError as error:
        pytest.fail(str(error))


@pytest.fixture(scope="module")
def chromium_browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def browser_session(
    chromium_browser: Browser, live_server: str
) -> Iterator[BrowserSession]:
    context = chromium_browser.new_context(
        base_url=live_server,
        viewport={"width": 1280, "height": 900},
    )
    page = context.new_page()
    console_messages: list[str] = []
    page_errors: list[str] = []
    session_401_responses: list[str] = []
    allowed_console_messages: list[str] = []
    document_csp_headers: list[str] = []
    cdp_sessions: list[CDPSession] = []
    page.on(
        "console",
        lambda message: console_messages.append(f"{message.type}: {message.text}"),
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on(
        "response",
        lambda response: (
            session_401_responses.append(response.url)
            if response.status == 401 and response.url.endswith("/api/session")
            else None
        ),
    )
    page.add_init_script(
        """
        window.__cspViolations = [];
        window.__scrollIntoViewCalls = [];
        window.addEventListener('securitypolicyviolation', event => {
          window.__cspViolations.push({
            blockedURI: event.blockedURI,
            effectiveDirective: event.effectiveDirective,
          });
        });
        const originalScrollIntoView = Element.prototype.scrollIntoView;
        Element.prototype.scrollIntoView = function(options) {
          window.__scrollIntoViewCalls.push(options || null);
          return originalScrollIntoView.call(this, options);
        };
        """
    )
    try:
        yield BrowserSession(
            page=page,
            context=context,
            base_url=live_server,
            console_messages=console_messages,
            page_errors=page_errors,
            session_401_responses=session_401_responses,
            allowed_console_messages=allowed_console_messages,
            document_csp_headers=document_csp_headers,
            cdp_sessions=cdp_sessions,
        )
    finally:
        for cdp in reversed(cdp_sessions):
            _cleanup_cdp_network_session(cdp)
        context.close()


def test_cdp_cleanup_attempts_disable_and_detach_when_network_restore_fails() -> None:
    class FakeCdpSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object | None]] = []

        def send(self, method: str, params: object | None = None) -> None:
            self.calls.append((method, params))
            if method == "Network.emulateNetworkConditions":
                raise RuntimeError("target already closing")

        def detach(self) -> None:
            self.calls.append(("detach", None))

    cdp = FakeCdpSession()
    _cleanup_cdp_network_session(cdp)  # type: ignore[arg-type]

    assert [method for method, _ in cdp.calls] == [
        "Network.emulateNetworkConditions",
        "Network.disable",
        "detach",
    ]


def _open_locked_application(session: BrowserSession) -> None:
    page = session.page
    response = page.goto("/", wait_until="domcontentloaded")
    assert response is not None
    csp_header = response.headers.get("content-security-policy")
    assert csp_header
    directives = {
        parts[0]: set(parts[1:])
        for directive in csp_header.split(";")
        if (parts := directive.strip().split())
    }
    for name, required_values in REQUIRED_CSP_DIRECTIVES.items():
        assert name in directives, csp_header
        assert required_values <= directives[name], csp_header
    session.document_csp_headers.append(csp_header)
    expect(page.get_by_role("dialog", name="访问验证")).to_be_visible(timeout=10_000)
    page.wait_for_timeout(50)
    expected_console = (
        "error: Failed to load resource: the server responded with a status of 401 "
        "(Unauthorized)"
    )
    assert session.session_401_responses == [f"{session.base_url}/api/session"]
    assert session.console_messages.count(expected_console) == 1
    session.console_messages.remove(expected_console)
    session.allowed_console_messages.append(EXPECTED_AUTH_CONSOLE_EXPLANATION)


def _unlock(page: Page) -> None:
    page.locator("#accessToken").fill(UPLOAD_TOKEN)
    page.locator("#deviceName").fill("Playwright Chromium")
    page.locator("#unlockSubmit").click()
    try:
        expect(page.locator("#sessionExpired")).to_be_hidden(timeout=10_000)
    except AssertionError as error:
        detail = page.locator(".unlock-error").text_content()
        raise AssertionError(f"unlock did not finish; dialog error: {detail!r}") from error
    expect(page.locator("#mainContent")).to_be_visible()


def _assert_browser_clean(session: BrowserSession) -> None:
    session.page.wait_for_timeout(100)
    csp_violations = session.page.evaluate("window.__cspViolations")
    assert session.console_messages == []
    assert session.page_errors == []
    assert csp_violations == []
    assert session.allowed_console_messages == [EXPECTED_AUTH_CONSOLE_EXPLANATION]
    assert len(session.document_csp_headers) == 1


def _assert_no_horizontal_overflow(page: Page) -> None:
    metrics = page.evaluate(
        """
        () => {
          const root = document.documentElement;
          const viewportWidth = root.clientWidth;
          const offenders = [...document.querySelectorAll('body *')]
            .map(element => {
              const rect = element.getBoundingClientRect();
              return {
                element: element.id ? `#${element.id}` : element.className || element.tagName,
                left: rect.left,
                right: rect.right,
                width: rect.width,
              };
            })
            .filter(item => item.right > viewportWidth + 1 || item.left < -1)
            .slice(0, 12);
          return {
            clientWidth: viewportWidth,
            scrollWidth: root.scrollWidth,
            offenders,
          };
        }
        """
    )
    assert metrics["scrollWidth"] <= metrics["clientWidth"], metrics


def _assert_visible_table_descendants_stay_within_cells(table: Locator) -> None:
    violations = table.locator("tbody tr").evaluate_all(
        """
        rows => rows.flatMap((row, rowIndex) => [...row.cells].flatMap((cell, cellIndex) => {
          if (getComputedStyle(cell).display === 'none') return [];
          const cellRect = cell.getBoundingClientRect();
          return [...cell.querySelectorAll('*')].flatMap(element => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            if (style.display === 'none' || rect.width === 0 || rect.height === 0) return [];
            if (rect.left >= cellRect.left - 1 && rect.right <= cellRect.right + 1) return [];
            return [{
              rowIndex,
              cellIndex,
              element: element.id || (typeof element.className === 'string' ? element.className : element.tagName),
              cellLeft: cellRect.left,
              cellRight: cellRect.right,
              elementLeft: rect.left,
              elementRight: rect.right,
            }];
          });
        }))
        """
    )
    assert violations == []


def create_test_files(
    tmp_path: Path, count: int, size_bytes: int
) -> list[Path]:
    paths: list[Path] = []
    block = b"transfer-e2e" * 1024
    for index in range(count):
        path = tmp_path / f"file-{index}.bin"
        with path.open("wb") as output:
            remaining = size_bytes
            while remaining:
                chunk = block[: min(len(block), remaining)]
                output.write(chunk)
                remaining -= len(chunk)
        paths.append(path)
    return paths


def hold_part_requests_until_released(page: Page) -> None:
    page.add_init_script(r"""
      (() => {
        const originalOpen = XMLHttpRequest.prototype.open;
        const originalSend = XMLHttpRequest.prototype.send;
        const held = [];
        let released = false;
        XMLHttpRequest.prototype.open = function(method, url, ...rest) {
          this.__uploadUrl = String(url);
          return originalOpen.call(this, method, url, ...rest);
        };
        XMLHttpRequest.prototype.send = function(body) {
          if (!released && this.__uploadUrl?.includes('/parts/')) {
            held.push({ request: this, body });
            return;
          }
          return originalSend.call(this, body);
        };
        window.__heldUploadPartCount = () => held.length;
        window.__releaseUploadParts = () => {
          released = true;
          const requests = held.splice(0);
          requests.forEach(entry => originalSend.call(entry.request, entry.body));
          return requests.length;
        };
      })();
    """)


@contextmanager
def track_part_request_concurrency(page: Page) -> Iterator[dict[str, int]]:
    state = {"active": 0, "peak": 0}

    def is_part_request(request: Request) -> bool:
        return "/api/uploads/" in request.url and "/parts/" in request.url

    def request_started(request: Request) -> None:
        if not is_part_request(request):
            return
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])

    def request_finished(request: Request) -> None:
        if is_part_request(request):
            state["active"] -= 1

    page.on("request", request_started)
    page.on("requestfinished", request_finished)
    page.on("requestfailed", request_finished)
    try:
        yield state
    finally:
        page.remove_listener("request", request_started)
        page.remove_listener("requestfinished", request_finished)
        page.remove_listener("requestfailed", request_finished)


def release_upload_creations_together(page: Page, count: int) -> None:
    page.add_init_script(
        f"""
        (() => {{
          const originalFetch = window.fetch.bind(window);
          const waiting = [];
          window.fetch = async (input, options = {{}}) => {{
            const response = await originalFetch(input, options);
            const url = new URL(typeof input === 'string' ? input : input.url, location.href);
            if (url.pathname === '/api/uploads' && options.method === 'POST') {{
              await new Promise(resolve => {{
                waiting.push(resolve);
                if (waiting.length === {count}) waiting.splice(0).forEach(release => release());
              }});
            }}
            return response;
          }};
        }})();
        """
    )


def _create_seed_session(session: BrowserSession, device_id: str) -> None:
    response = session.context.request.post(
        f"{session.base_url}/api/session",
        data={
            "access_token": UPLOAD_TOKEN,
            "device_id": device_id,
            "device_name": "Playwright seed",
        },
    )
    assert response.ok


def _wait_for_active_upload(
    context: BrowserContext, base_url: str, predicate: Callable[[dict[str, object]], bool]
) -> dict[str, object]:
    deadline = time.monotonic() + 30
    last_uploads: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        response = context.request.get(f"{base_url}/api/uploads/active")
        assert response.ok
        last_uploads = response.json()
        for upload in last_uploads:
            if predicate(upload):
                return upload
        time.sleep(0.05)
    raise AssertionError(f"active upload did not reach the expected state: {last_uploads!r}")


def _assert_route_state(
    page: Page, route: str, visible_nav: str, *, expect_heading_focus: bool = True
) -> None:
    title = ROUTE_TITLES[route]
    assert page.evaluate("location.hash") == f"#{route}"
    expect(page.locator(f'#{route}Page')).to_be_visible()
    for other_route in ROUTE_TITLES.keys() - {route}:
        expect(page.locator(f'#{other_route}Page')).to_be_hidden()
    expect(page).to_have_title(f"{title} · MonkeyCode")
    expect(page.locator("[data-route-title]")).to_have_text(title)
    if expect_heading_focus:
        expect(page.locator(f'[data-route-heading="{route}"]')).to_be_focused()

    hidden_nav = ".mobile-nav" if visible_nav == ".sidebar" else ".sidebar"
    expect(page.locator(f'{visible_nav} [data-route="{route}"]')).to_be_visible()
    expect(page.locator(f'{hidden_nav} [data-route="{route}"]')).to_be_hidden()
    for nav_selector in (".sidebar", ".mobile-nav"):
        for candidate_route in ROUTE_TITLES:
            button = page.locator(
                f'{nav_selector} [data-route="{candidate_route}"]'
            )
            state = button.evaluate(
                """
                element => ({
                  active: element.classList.contains('active'),
                  ariaCurrent: element.getAttribute('aria-current'),
                })
                """
            )
            assert state == {
                "active": candidate_route == route,
                "ariaCurrent": "page" if candidate_route == route else None,
            }


def _assert_batch_toolbar_state(toolbar: Locator, *, visible: bool) -> None:
    state = toolbar.evaluate(
        """
        element => ({
          visibleClass: element.classList.contains('visible'),
          visibility: getComputedStyle(element).visibility,
          pointerEvents: getComputedStyle(element).pointerEvents,
        })
        """
    )
    assert state == {
        "visibleClass": visible,
        "visibility": "visible" if visible else "hidden",
        "pointerEvents": "auto" if visible else "none",
    }


def test_live_server_retries_an_occupied_first_port(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        occupied_port = int(blocker.getsockname()[1])
        attempts = 0

        def port_provider() -> int:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return occupied_port
            return _unused_loopback_port()

        with _run_live_server(tmp_path, port_provider) as base_url:
            assert base_url != f"http://127.0.0.1:{occupied_port}"
            with urlopen(base_url, timeout=0.5) as response:
                assert response.status == 200

    first_attempt_log = (tmp_path / "server-attempt-1.log").read_text(
        encoding="utf-8", errors="replace"
    )
    normalized_log = first_attempt_log.lower()
    assert (
        "eaddrinuse" in normalized_log
        or "address already in use" in normalized_log
        or "errno 98" in normalized_log
    ), first_attempt_log
    assert 2 <= attempts <= SERVER_START_ATTEMPTS


def test_unlock_dialog_native_focus_and_inert_lifecycle(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    _open_locked_application(browser_session)

    dialog = page.get_by_role("dialog", name="访问验证")
    assert dialog.get_attribute("aria-modal") == "true"
    assert page.evaluate(
        """
        () => Array.from(document.body.children)
          .filter(element => element.id !== 'sessionExpired')
          .every(element => element.inert)
        """
    )

    token = page.locator("#accessToken")
    submit = page.locator("#unlockSubmit")
    expect(token).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(submit).to_be_focused()
    page.keyboard.press("Tab")
    expect(token).to_be_focused()

    _unlock(page)
    assert page.evaluate(
        "() => Array.from(document.body.children).every(element => !element.inert)"
    )
    expect(page.locator("#mainContent")).to_be_focused()
    _assert_browser_clean(browser_session)


def test_composer_enter_shift_enter_and_ime_composition(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    _open_locked_application(browser_session)
    _unlock(page)

    textarea = page.locator("#composerTextarea")
    messages = page.locator(".timeline-message")

    textarea.fill("保留换行")
    page.keyboard.press("Shift+Enter")
    assert textarea.input_value() == "保留换行\n"
    assert messages.count() == 0

    textarea.fill("输入法组合中")
    is_composing = textarea.evaluate(
        """
        element => {
          const event = new KeyboardEvent('keydown', {
            key: 'Enter',
            code: 'Enter',
            bubbles: true,
            cancelable: true,
            isComposing: true,
          });
          element.dispatchEvent(event);
          return event.isComposing;
        }
        """
    )
    assert is_composing is True
    page.wait_for_timeout(200)
    assert textarea.input_value() == "输入法组合中"
    assert messages.count() == 0

    textarea.fill("Playwright 真实文本")
    page.keyboard.press("Enter")
    expect(
        page.locator(".timeline-message-body").filter(
            has_text="Playwright 真实文本"
        )
    ).to_have_count(1)
    expect(textarea).to_have_value("")
    _assert_browser_clean(browser_session)


def test_transfer_long_content_and_new_message_notice_do_not_overlap(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    page.set_viewport_size({"width": 390, "height": 844})
    _open_locked_application(browser_session)
    _unlock(page)
    expect(page.locator("#connectionStatus")).to_have_text("已连接", timeout=10_000)

    last_seed_id = ""
    for index in range(12):
        response = browser_session.context.request.post(
            f"{browser_session.base_url}/api/messages",
            data={
                "body": "https://example.com/" + "long-segment-" * 30,
                "client_request_id": f"density-message-{index}",
            },
        )
        assert response.ok
        last_seed_id = response.json()["id"]
    expect(page.locator(f'[data-message-id="{last_seed_id}"]')).to_be_attached(
        timeout=10_000
    )

    timeline = page.locator("#timelineContainer")
    notice = page.locator("#newMessageButton")
    assert notice.is_hidden()
    assert timeline.evaluate(
        "node => getComputedStyle(node).paddingBottom"
    ) == "8px"
    assert page.locator("#timelinePanel").evaluate(
        "node => getComputedStyle(node).paddingBottom"
    ) != "64px"
    page.wait_for_function(
        """
        () => {
          const timeline = document.getElementById('timelineContainer');
          return timeline.scrollHeight - timeline.clientHeight > 48;
        }
        """
    )
    timeline.evaluate(
        """
        node => {
          node.scrollTop = 0;
          node.dispatchEvent(new Event('scroll'));
        }
        """
    )
    page.wait_for_function(
        """
        () => {
          const timeline = document.getElementById('timelineContainer');
          return timeline.scrollTop === 0 &&
            timeline.scrollHeight - timeline.clientHeight > 48;
        }
        """
    )
    response = browser_session.context.request.post(
        f"{browser_session.base_url}/api/messages",
        data={"body": "new message", "client_request_id": "density-new-message"},
    )
    assert response.ok
    new_message_id = response.json()["id"]
    expect(page.locator(f'[data-message-id="{new_message_id}"]')).to_be_attached(
        timeout=10_000
    )

    expect(notice).to_be_visible(timeout=10_000)
    layout = page.locator("#timelinePanel").evaluate(
        """
        panel => {
          const timeline = panel.querySelector('#timelineContainer');
          const notice = panel.querySelector('#newMessageButton');
          const timelineRect = timeline.getBoundingClientRect();
          const noticeRect = notice.getBoundingClientRect();
          const messageRects = Array.from(
            timeline.querySelectorAll('.timeline-message')
          ).map(message => message.getBoundingClientRect());
          const overlaps = messageRects.some(rect => {
            const visibleTop = Math.max(rect.top, timelineRect.top);
            const visibleBottom = Math.min(rect.bottom, timelineRect.bottom);
            return visibleTop < visibleBottom &&
              rect.left < noticeRect.right && rect.right > noticeRect.left &&
              visibleTop < noticeRect.bottom && visibleBottom > noticeRect.top;
          });
          return {
            overlaps,
            paddingBottom: Number.parseFloat(getComputedStyle(panel).paddingBottom),
            noticeHeight: noticeRect.height,
            noticePosition: getComputedStyle(notice).position,
            centerDelta: Math.abs(
              (noticeRect.left + noticeRect.right) / 2 -
              (timelineRect.left + timelineRect.right) / 2
            ),
          };
        }
        """
    )
    assert layout["overlaps"] is False
    assert layout["paddingBottom"] >= layout["noticeHeight"]
    assert layout["noticePosition"] == "absolute"
    assert layout["centerDelta"] <= 1
    link_layout = page.locator(".timeline-message-body").first.evaluate(
        """
        body => {
          const bodyRect = body.getBoundingClientRect();
          const link = body.querySelector('a');
          const linkRects = Array.from(link.getClientRects());
          return {
            bodyFits: body.scrollWidth <= body.clientWidth,
            overflowWrap: getComputedStyle(body).overflowWrap,
            linkLineCount: linkRects.length,
            linkFits: linkRects.every(rect =>
              rect.left >= bodyRect.left - 0.5 && rect.right <= bodyRect.right + 0.5
            ),
          };
        }
        """
    )
    assert link_layout["bodyFits"] is True
    assert link_layout["overflowWrap"] == "anywhere"
    assert link_layout["linkLineCount"] > 1
    assert link_layout["linkFits"] is True

    notice_before_hover = notice.bounding_box()
    assert notice_before_hover is not None
    notice.hover()
    page.wait_for_function(
        """
        beforeY => {
          const notice = document.getElementById('newMessageButton');
          return Math.abs(notice.getBoundingClientRect().top - (beforeY - 1)) < 0.1;
        }
        """,
        arg=notice_before_hover["y"],
    )
    notice_after_hover = notice.bounding_box()
    assert notice_after_hover is not None
    before_center_x = notice_before_hover["x"] + notice_before_hover["width"] / 2
    after_center_x = notice_after_hover["x"] + notice_after_hover["width"] / 2
    assert after_center_x == pytest.approx(before_center_x, abs=0.5)
    assert notice_after_hover["y"] == pytest.approx(
        notice_before_hover["y"] - 1, abs=0.2
    )

    notice.evaluate(
        "node => { node.textContent = `${'9'.repeat(80)} 条新消息`; }"
    )
    notice_box = notice.bounding_box()
    timeline_box = timeline.bounding_box()
    assert notice_box is not None and timeline_box is not None
    viewport_width = page.evaluate("document.documentElement.clientWidth")
    assert notice_box["x"] >= -0.5
    assert notice_box["x"] + notice_box["width"] <= viewport_width + 0.5
    assert notice_box["x"] + notice_box["width"] / 2 == pytest.approx(
        timeline_box["x"] + timeline_box["width"] / 2,
        abs=0.5,
    )
    _assert_no_horizontal_overflow(page)
    _assert_browser_clean(browser_session)


def test_transfer_short_viewport_uses_unbounded_timeline_and_document_flow(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    page.set_viewport_size({"width": 375, "height": 667})
    _open_locked_application(browser_session)
    _unlock(page)

    layout = page.locator("#transferPage").evaluate(
        """
        transferPage => {
          const workspace = transferPage.querySelector('.transfer-workspace');
          const timelinePanel = transferPage.querySelector('#timelinePanel');
          const timeline = transferPage.querySelector('#timelineContainer');
          const composer = transferPage.querySelector('#composerPanel');
          return {
            workspaceDisplay: getComputedStyle(workspace).display,
            workspaceMinHeight: getComputedStyle(workspace).minHeight,
            timelineMaxHeight: getComputedStyle(timeline).maxHeight,
            timelineOverflowY: getComputedStyle(timeline).overflowY,
            timelinePanelMinHeight: Number.parseFloat(
              getComputedStyle(timelinePanel).minHeight
            ),
            composerPosition: getComputedStyle(composer).position,
          };
        }
        """
    )
    assert layout == {
        "workspaceDisplay": "block",
        "workspaceMinHeight": "0px",
        "timelineMaxHeight": "none",
        "timelineOverflowY": "auto",
        "timelinePanelMinHeight": 320,
        "composerPosition": "static",
    }
    _assert_no_horizontal_overflow(page)
    _assert_browser_clean(browser_session)


def test_transfer_visible_upload_summary_stays_in_status_stack(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    page.set_viewport_size({"width": 390, "height": 844})
    _open_locked_application(browser_session)
    _unlock(page)

    page.locator("#uploadSummary").evaluate("element => { element.hidden = false; }")
    layout = page.locator("#transferPage").evaluate(
        """
        transferPage => {
          const workspace = transferPage.querySelector('.transfer-workspace');
          const stack = transferPage.querySelector('.transfer-status-stack');
          const status = transferPage.querySelector('.transfer-status-strip');
          const summary = transferPage.querySelector('#uploadSummary');
          const timeline = transferPage.querySelector('#timelinePanel');
          const stackRect = stack?.getBoundingClientRect();
          const statusRect = status.getBoundingClientRect();
          const summaryRect = summary.getBoundingClientRect();
          const timelineRect = timeline.getBoundingClientRect();
          return {
            summaryParentClass: summary.parentElement.className,
            stackDisplay: stack ? getComputedStyle(stack).display : null,
            workspaceDisplay: getComputedStyle(workspace).display,
            statusBeforeSummary: statusRect.bottom <= summaryRect.top,
            summaryBeforeTimeline: summaryRect.bottom <= timelineRect.top,
            timelineGap: stackRect ? timelineRect.top - stackRect.bottom : null,
            timelineHeight: timelineRect.height,
          };
        }
        """
    )
    assert layout["summaryParentClass"] == "transfer-status-stack"
    assert layout["stackDisplay"] in ("grid", "flex")
    assert layout["workspaceDisplay"] == "flex"
    assert layout["statusBeforeSummary"] is True
    assert layout["summaryBeforeTimeline"] is True
    assert 0 <= layout["timelineGap"] <= 16
    assert layout["timelineHeight"] >= 220
    _assert_no_horizontal_overflow(page)
    _assert_browser_clean(browser_session)


def test_tabs_skip_link_and_mobile_touch_targets(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    page.set_viewport_size({"width": 375, "height": 812})
    _open_locked_application(browser_session)

    unlock_box = page.locator("#unlockSubmit").bounding_box()
    assert unlock_box is not None
    assert unlock_box["width"] >= 44 and unlock_box["height"] >= 44
    _unlock(page)

    skip_link = page.locator("#skipLink")
    page.evaluate(
        """
        () => {
          const timeline = document.getElementById('mainContent');
          const nativeFocus = timeline.focus.bind(timeline);
          window.__skipLinkFocusCalls = 0;
          timeline.focus = (...args) => {
            window.__skipLinkFocusCalls += 1;
            return nativeFocus(...args);
          };
        }
        """
    )
    skip_link.focus()
    expect(skip_link).to_be_focused()
    expect(skip_link).to_be_visible()
    skip_box = skip_link.bounding_box()
    assert skip_box is not None and skip_box["width"] > 1 and skip_box["height"] > 1
    page.keyboard.press("Enter")
    expect(page.locator("#mainContent")).to_be_focused()
    assert page.evaluate("location.hash") == "#transfer"
    assert page.evaluate("window.__skipLinkFocusCalls") == 1

    skip_link.focus()
    skip_link.click()
    expect(page.locator("#mainContent")).to_be_focused()
    assert page.evaluate("location.hash") == "#transfer"
    assert page.evaluate("window.__skipLinkFocusCalls") == 2

    page.locator('.mobile-nav [data-route="files"]').click()
    expect(page.locator("#filesPage")).to_be_visible()
    grid = page.locator("#gridViewBtn")
    list_view = page.locator("#listViewBtn")
    grid.focus()
    page.keyboard.press("ArrowRight")
    expect(list_view).to_be_focused()
    expect(list_view).to_have_attribute("aria-selected", "true")
    page.keyboard.press("ArrowLeft")
    expect(grid).to_be_focused()
    expect(grid).to_have_attribute("aria-selected", "true")
    page.keyboard.press("End")
    expect(list_view).to_be_focused()
    expect(list_view).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Home")
    expect(grid).to_be_focused()
    expect(grid).to_have_attribute("aria-selected", "true")

    _assert_no_horizontal_overflow(page)
    major_button_boxes = page.locator(
        "button.btn-primary:visible, #gridViewBtn:visible, #listViewBtn:visible, "
        "#composerAttachBtn:visible, .mobile-nav button:visible"
    ).evaluate_all(
        """
        elements => elements.map(element => {
          const rect = element.getBoundingClientRect();
          return { id: element.id, text: element.textContent.trim(), width: rect.width, height: rect.height };
        })
        """
    )
    assert major_button_boxes
    assert all(
        box["width"] >= 44 and box["height"] >= 44
        for box in major_button_boxes
    ), major_button_boxes
    _assert_browser_clean(browser_session)


@pytest.mark.parametrize(
    ("viewport", "nav_selector"),
    [
        pytest.param(
            {"width": 1440, "height": 900}, ".sidebar", id="desktop-1440"
        ),
        pytest.param(
            {"width": 390, "height": 844}, ".mobile-nav", id="mobile-390x844"
        ),
        pytest.param(
            {"width": 390, "height": 568}, ".mobile-nav", id="mobile-390x568"
        ),
        pytest.param(
            {"width": 600, "height": 800}, ".mobile-nav", id="mobile-600x800"
        ),
    ],
)
def test_three_route_navigation_history_focus_and_viewport_safety(
    browser_session: BrowserSession,
    viewport: dict[str, int],
    nav_selector: str,
) -> None:
    page = browser_session.page
    page.set_viewport_size(viewport)
    if viewport == {"width": 390, "height": 568}:
        _create_seed_session(browser_session, "playwright-compact-seed")
        for index in range(18):
            response = browser_session.context.request.post(
                f"{browser_session.base_url}/api/messages",
                data={
                    "body": f"compact-seed-{index:02d}",
                    "client_request_id": f"compact-seed-{index:02d}",
                },
            )
            assert response.ok
        browser_session.context.clear_cookies()
    _open_locked_application(browser_session)
    _unlock(page)

    expect(page.locator("#transferPage")).to_be_visible()
    assert page.evaluate("location.hash") == "#transfer"
    _assert_no_horizontal_overflow(page)

    page.locator(f'{nav_selector} [data-route="transfer"]').click()
    _assert_route_state(page, "transfer", nav_selector)

    page.locator(f'{nav_selector} [data-route="files"]').click()
    _assert_route_state(page, "files", nav_selector)
    _assert_no_horizontal_overflow(page)

    assert page.locator("#batchToolbar").evaluate(
        "element => getComputedStyle(element).pointerEvents"
    ) == "none"
    page.locator(f'{nav_selector} [data-route="manage"]').click()
    _assert_route_state(page, "manage", nav_selector)
    _assert_no_horizontal_overflow(page)

    stable_control = page.locator("#themeToggle")
    expect(stable_control).to_be_visible()
    stable_control.focus()
    expect(stable_control).to_be_focused()
    page.go_back()
    _assert_route_state(
        page, "files", nav_selector, expect_heading_focus=False
    )
    expect(stable_control).to_be_focused()
    page.go_forward()
    _assert_route_state(
        page, "manage", nav_selector, expect_heading_focus=False
    )
    expect(stable_control).to_be_focused()

    page.locator(f'{nav_selector} [data-route="transfer"]').click()
    _assert_route_state(page, "transfer", nav_selector)
    _assert_no_horizontal_overflow(page)

    if nav_selector == ".mobile-nav":
        composer = page.locator("#composerPanel")
        mobile_nav = page.locator(".mobile-nav")
        timeline = page.locator("#timelineContainer")
        connection = page.locator("#connectionStatus")
        expect(timeline).to_be_visible()
        expect(composer).to_be_visible()
        expect(connection).to_be_visible()
        assert page.evaluate("window.scrollY") == 0
        timeline_metrics = timeline.evaluate(
            """
            element => {
              const rect = element.getBoundingClientRect();
              const style = getComputedStyle(element);
              return {
                top: rect.top,
                bottom: rect.bottom,
                height: rect.height,
                clientHeight: element.clientHeight,
                scrollHeight: element.scrollHeight,
                overflowY: style.overflowY,
              };
            }
            """
        )
        composer_box = composer.bounding_box()
        mobile_nav_box = mobile_nav.bounding_box()
        assert composer_box is not None and mobile_nav_box is not None
        composer_position = composer.evaluate(
            "element => getComputedStyle(element).position"
        )
        assert timeline_metrics["height"] >= 120
        assert timeline_metrics["overflowY"] == "auto"
        assert timeline_metrics["top"] >= 0
        assert timeline_metrics["bottom"] <= composer_box["y"] + 1, {
            "timeline": timeline_metrics,
            "composer": composer_box,
        }
        if viewport == {"width": 390, "height": 568}:
            compact_layout = page.locator("#transferPage").evaluate(
                """
                transferPage => {
                  const workspace = transferPage.querySelector('.transfer-workspace');
                  const panel = transferPage.querySelector('#timelinePanel');
                  const timeline = transferPage.querySelector('#timelineContainer');
                  const composer = transferPage.querySelector('#composerPanel');
                  return {
                    workspaceDisplay: getComputedStyle(workspace).display,
                    workspaceMinHeight: getComputedStyle(workspace).minHeight,
                    panelMinHeight: Number.parseFloat(getComputedStyle(panel).minHeight),
                    timelineMaxHeight: getComputedStyle(timeline).maxHeight,
                    composerPosition: getComputedStyle(composer).position,
                  };
                }
                """
            )
            assert compact_layout == {
                "workspaceDisplay": "block",
                "workspaceMinHeight": "0px",
                "panelMinHeight": 320,
                "timelineMaxHeight": "none",
                "composerPosition": "static",
            }
            assert timeline_metrics["scrollHeight"] > timeline_metrics["clientHeight"]
            assert composer_position == "static"
        else:
            assert composer_position == "fixed"
            assert composer_box["y"] >= 0
            assert composer_box["y"] + composer_box["height"] <= mobile_nav_box["y"]

    else:
        assert page.locator("#composerPanel").evaluate(
            "element => getComputedStyle(element).position"
        ) == "sticky"

    _assert_browser_clean(browser_session)


def test_empty_files_action_returns_to_transfer_and_opens_picker(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    _open_locked_application(browser_session)
    _unlock(page)

    page.locator('.sidebar [data-route="files"]').click()
    empty_action = page.locator("#emptyFilesAction")
    expect(empty_action).to_be_visible()
    with page.expect_file_chooser() as chooser_info:
        empty_action.click()

    assert chooser_info.value.is_multiple()
    expect(page.locator("#transferPage")).to_be_visible()
    expect(page.locator('[data-route-heading="transfer"]')).to_be_focused()
    assert page.evaluate("location.hash") == "#transfer"
    _assert_browser_clean(browser_session)


@pytest.mark.parametrize(
    "viewport",
    [
        pytest.param({"width": 1024, "height": 768}, id="desktop-1024x768"),
        pytest.param({"width": 390, "height": 844}, id="mobile-390x844"),
        pytest.param({"width": 375, "height": 667}, id="mobile-375x667"),
    ],
)
def test_files_tools_and_results_have_no_horizontal_overflow(
    browser_session: BrowserSession,
    viewport: dict[str, int],
) -> None:
    page = browser_session.page
    page.set_viewport_size(viewport)
    _create_seed_session(browser_session, f"files-density-{viewport['width']}")
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    files = (
        (f"task-3-{'unbrokenfilename' * 10}.txt", "text/plain", b"files density"),
        (f"task-3-{'secondlongfilename' * 10}.md", "text/markdown", b"# density"),
        (f"task-3-{'previewfilename' * 10}.png", "image/png", png),
    )
    for index, (name, mime_type, content) in enumerate(files):
        upload = browser_session.context.request.post(
            f"{browser_session.base_url}/api/upload",
            multipart={
                "client_request_id": f"files-density-{viewport['width']}-{index}",
                "file": {
                    "name": name,
                    "mimeType": mime_type,
                    "buffer": content,
                },
            },
        )
        assert upload.ok
    browser_session.context.clear_cookies()

    _open_locked_application(browser_session)
    _unlock(page)
    nav_selector = ".sidebar" if viewport["width"] > 720 else ".mobile-nav"
    page.locator(f'{nav_selector} [data-route="files"]').click()
    _assert_route_state(page, "files", nav_selector)
    page.locator("#librarySearch").fill("task-3-")
    page.locator("#filterToggleBtn").click()
    expect(page.locator("#filterGrid")).not_to_have_class(re.compile(r".*collapsed.*"))

    primary_box = page.locator(".library-primary-tools").bounding_box()
    secondary_box = page.locator(".library-secondary-tools").bounding_box()
    assert primary_box is not None and secondary_box is not None
    assert primary_box["y"] + primary_box["height"] <= secondary_box["y"]
    for selector in (
        "#librarySearch",
        "#filterToggleBtn",
        "#gridViewBtn",
        "#listViewBtn",
    ):
        box = page.locator(selector).bounding_box()
        assert box is not None
        assert box["width"] >= 43.99 and box["height"] >= 43.99, (selector, box)

    grid_cards = page.locator("#fileList.grid-mode .file-card")
    expect(grid_cards).to_have_count(3)
    grid_metrics = grid_cards.evaluate_all(
        """
        cards => cards.map(card => {
          const listRect = card.parentElement.getBoundingClientRect();
          const cardRect = card.getBoundingClientRect();
          const name = card.querySelector('.file-name');
          return {
            cardLeft: cardRect.left,
            cardRight: cardRect.right,
            cardTop: cardRect.top,
            listLeft: listRect.left,
            listRight: listRect.right,
            nameClientWidth: name.clientWidth,
            nameScrollWidth: name.scrollWidth,
            overflowWrap: getComputedStyle(name).overflowWrap,
          };
        })
        """
    )
    assert all(metric["cardLeft"] >= metric["listLeft"] - 1 for metric in grid_metrics)
    assert all(metric["cardRight"] <= metric["listRight"] + 1 for metric in grid_metrics)
    assert all(
        metric["nameScrollWidth"] <= metric["nameClientWidth"] + 1
        for metric in grid_metrics
    )
    assert all(metric["overflowWrap"] == "anywhere" for metric in grid_metrics)
    grid_columns = {round(metric["cardLeft"]) for metric in grid_metrics}
    if viewport["width"] > 720:
        assert len(grid_columns) == 3
    else:
        assert len(grid_columns) == 1

    preview_card = grid_cards.filter(has=page.locator(".media img"))
    expect(preview_card).to_have_count(1)
    preview_card.locator(".media").click()
    preview_dialog = page.locator("#previewModal")
    expect(preview_dialog).to_be_visible()
    expect(page.locator("#previewTitle")).to_contain_text("previewfilename")
    expect(page.locator("#previewImage")).to_be_visible()
    page.locator("#closePreviewBtn").click()
    expect(preview_dialog).to_be_hidden()

    card_selection = grid_cards.first.locator(".select-line")
    card_selection_box = card_selection.bounding_box()
    card_checkbox_box = card_selection.locator("input").bounding_box()
    assert card_selection_box is not None and card_checkbox_box is not None
    assert card_selection_box["width"] >= 43.99
    assert card_selection_box["height"] >= 43.99
    assert card_checkbox_box["width"] <= 24 and card_checkbox_box["height"] <= 24
    card_selection.click()
    expect(grid_cards.first.locator(".select-line input")).to_be_checked()
    card_selection.click()
    expect(grid_cards.first.locator(".select-line input")).not_to_be_checked()
    _assert_no_horizontal_overflow(page)

    page.locator("#listViewBtn").click()
    list_result = page.locator("#fileList.table-mode")
    expect(list_result).to_be_visible()
    list_metrics = list_result.locator("tbody tr").first.evaluate(
        """
        row => {
          const listRect = row.closest('.file-list').getBoundingClientRect();
          const rowRect = row.getBoundingClientRect();
          const name = row.querySelector('.file-name strong');
          const meta = row.querySelector('.file-name span');
          const actions = row.querySelector('.row-actions');
          return {
            rowLeft: rowRect.left,
            rowRight: rowRect.right,
            listLeft: listRect.left,
            listRight: listRect.right,
            rowDisplay: getComputedStyle(row).display,
            rowColumns: getComputedStyle(row).gridTemplateColumns,
            nameClientWidth: name.clientWidth,
            nameScrollWidth: name.scrollWidth,
            nameHeight: name.getBoundingClientRect().height,
            nameLineHeight: Number.parseFloat(getComputedStyle(name).lineHeight),
            nameWhiteSpace: getComputedStyle(name).whiteSpace,
            nameOverflowWrap: getComputedStyle(name).overflowWrap,
            metaTop: meta.getBoundingClientRect().top,
            nameBottom: name.getBoundingClientRect().bottom,
            actionsWidth: actions.getBoundingClientRect().width,
          };
        }
        """
    )
    assert list_metrics["rowLeft"] >= list_metrics["listLeft"] - 1
    assert list_metrics["rowRight"] <= list_metrics["listRight"] + 1
    assert list_metrics["nameScrollWidth"] <= list_metrics["nameClientWidth"] + 1

    table_selection = list_result.locator(".table-select-target").first
    table_selection_box = table_selection.bounding_box()
    assert table_selection_box is not None
    assert table_selection_box["width"] >= 43.99
    assert table_selection_box["height"] >= 43.99
    table_check = table_selection.locator(".check")
    table_check_box = table_check.bounding_box()
    assert table_check_box is not None
    assert table_check_box["width"] <= 20.01 and table_check_box["height"] <= 20.01
    table_selection.click()
    expect(table_check).to_be_checked()

    _assert_visible_table_descendants_stay_within_cells(list_result)

    preview_row = list_result.locator('tbody tr:has([data-file-action="preview"])')
    expect(preview_row).to_have_count(1)
    preview_actions = preview_row.locator(".row-action")
    expect(preview_actions).to_have_count(2)
    for action_index in range(2):
        action_box = preview_actions.nth(action_index).bounding_box()
        assert action_box is not None
        assert action_box["width"] >= 43.99 and action_box["height"] >= 43.99
    if viewport["width"] <= 720:
        table_head = list_result.locator("thead")
        expect(table_head).to_have_count(1)
        header_semantics = table_head.evaluate(
            """
            head => {
              const rect = head.getBoundingClientRect();
              const style = getComputedStyle(head);
              const headers = [...head.querySelectorAll('th')];
              return {
                display: style.display,
                position: style.position,
                width: rect.width,
                height: rect.height,
                texts: headers.map(header => header.textContent.trim()),
                scopes: headers.map(header => header.getAttribute('scope')),
              };
            }
            """
        )
        assert header_semantics["display"] != "none"
        assert header_semantics["position"] == "absolute"
        assert header_semantics["width"] <= 1.01
        assert header_semantics["height"] <= 1.01
        assert header_semantics["texts"] == [
            "选择",
            "文件",
            "大小",
            "更新时间",
            "状态",
            "操作",
        ]
        assert header_semantics["scopes"] == ["col"] * 6
        assert list_metrics["rowDisplay"] == "grid"
        assert list_metrics["rowColumns"].split()[0] == "44px"
        assert list_metrics["nameWhiteSpace"] == "normal"
        assert list_metrics["nameOverflowWrap"] == "anywhere"
        assert list_metrics["nameHeight"] > list_metrics["nameLineHeight"]
        assert list_metrics["metaTop"] >= list_metrics["nameBottom"]
        assert list_metrics["actionsWidth"] == pytest.approx(44, abs=1)
    if viewport == {"width": 390, "height": 844}:
        if not page.locator("html").evaluate("element => element.classList.contains('dark')"):
            page.locator("#themeToggle").click()
        expect(page.locator("html")).to_have_class(re.compile(r".*dark.*"))
        dark_colors = preview_row.evaluate(
            """
            row => {
              const probe = document.createElement('span');
              probe.style.color = 'var(--surface)';
              probe.style.backgroundColor = 'var(--fg)';
              document.body.append(probe);
              const result = {
                background: getComputedStyle(row).backgroundColor,
                color: getComputedStyle(row).color,
                expectedBackground: getComputedStyle(probe).color,
                expectedColor: getComputedStyle(probe).backgroundColor,
              };
              probe.remove();
              return result;
            }
            """
        )
        assert dark_colors["background"] == dark_colors["expectedBackground"]
        assert dark_colors["color"] == dark_colors["expectedColor"]
    _assert_no_horizontal_overflow(page)
    _assert_browser_clean(browser_session)


@pytest.mark.parametrize("width", [721, 900, 901])
def test_files_compact_table_keeps_visible_cells_separate(
    browser_session: BrowserSession,
    width: int,
) -> None:
    page = browser_session.page
    page.set_viewport_size({"width": width, "height": 768})
    _create_seed_session(browser_session, f"files-compact-{width}")
    upload = browser_session.context.request.post(
        f"{browser_session.base_url}/api/upload",
        multipart={
            "client_request_id": f"files-compact-{width}",
            "file": {
                "name": f"compact-{'unbrokenfilename' * 12}.txt",
                "mimeType": "text/plain",
                "buffer": b"compact table boundaries",
            },
        },
    )
    assert upload.ok
    browser_session.context.clear_cookies()

    _open_locked_application(browser_session)
    _unlock(page)
    page.locator('.sidebar [data-route="files"]').click()
    _assert_route_state(page, "files", ".sidebar")
    page.locator("#listViewBtn").click()
    table = page.locator("#fileList.table-mode")
    expect(table).to_be_visible()
    expect(table.locator("tbody td:nth-child(3)")).to_be_hidden()
    expect(table.locator("tbody td:nth-child(5)")).to_be_hidden()
    date_cell = table.locator("tbody td:nth-child(4)")
    expect(date_cell).to_be_visible()
    date_style = date_cell.evaluate(
        """
        cell => ({
          overflow: getComputedStyle(cell).overflow,
          textOverflow: getComputedStyle(cell).textOverflow,
          whiteSpace: getComputedStyle(cell).whiteSpace,
        })
        """
    )
    assert date_style == {
        "overflow": "hidden",
        "textOverflow": "ellipsis",
        "whiteSpace": "nowrap",
    }
    _assert_visible_table_descendants_stay_within_cells(table)
    _assert_no_horizontal_overflow(page)
    _assert_browser_clean(browser_session)


def test_files_locate_routes_then_highlights_message_and_back_returns_files(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    _create_seed_session(browser_session, "playwright-locate-seed")
    upload = browser_session.context.request.post(
        f"{browser_session.base_url}/api/upload",
        multipart={
            "client_request_id": "browser-locate-upload",
            "file": {
                "name": "browser-locate.txt",
                "mimeType": "text/plain",
                "buffer": b"locate target",
            },
        },
    )
    assert upload.ok
    message_id = upload.json()["id"]
    browser_session.context.clear_cookies()

    _open_locked_application(browser_session)
    _unlock(page)
    page.locator('.sidebar [data-route="files"]').click()
    _assert_route_state(page, "files", ".sidebar")
    page.locator(
        f'[data-file-action="locate"][data-message-id="{message_id}"]'
    ).click()

    _assert_route_state(
        page, "transfer", ".sidebar", expect_heading_focus=False
    )
    target = page.locator(f'.timeline-message[data-message-id="{message_id}"]')
    expect(target).to_have_class(re.compile(r".*timeline-message-highlight.*"))
    expect(target).to_have_attribute("tabindex", "-1")
    expect(target).to_be_focused()
    assert page.evaluate("location.hash") == "#transfer"

    page.go_back()
    _assert_route_state(page, "files", ".sidebar", expect_heading_focus=False)
    assert page.evaluate("location.hash") == "#files"
    _assert_browser_clean(browser_session)


def test_mobile_toast_stays_above_composer_without_intercepting_it(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    page.set_viewport_size({"width": 390, "height": 568})
    _open_locked_application(browser_session)
    _unlock(page)
    composer = page.locator("#composerPanel")
    attach = page.locator("#composerAttachBtn")
    attach.scroll_into_view_if_needed()
    page.evaluate(
        """
        window.dispatchEvent(new CustomEvent('timeline-error', {
          detail: { message: 'toast geometry' },
        }))
        """
    )

    toast = page.locator("#toast")
    expect(toast).to_be_visible()
    expect(composer).to_be_visible()
    page.wait_for_function(
        """
        () => {
          const toast = document.querySelector('#toast').getBoundingClientRect();
          const composer = document.querySelector('#composerPanel').getBoundingClientRect();
          return toast.bottom <= composer.top - 8;
        }
    """
    )
    toast_box = toast.bounding_box()
    composer_box = composer.bounding_box()
    attach_box = attach.bounding_box()
    assert toast_box is not None and composer_box is not None and attach_box is not None
    assert toast_box["y"] + toast_box["height"] <= composer_box["y"] - 8
    assert page.evaluate(
        """
        ({ x, y }) => Boolean(
          document.elementFromPoint(x, y)?.closest('#composerPanel')
        )
        """,
        {
            "x": attach_box["x"] + attach_box["width"] / 2,
            "y": attach_box["y"] + attach_box["height"] / 2,
        },
    )
    _assert_browser_clean(browser_session)


def test_health_storage_summary_is_loaded_only_in_manage(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    _create_seed_session(browser_session, "playwright-health-seed")
    upload = browser_session.context.request.post(
        f"{browser_session.base_url}/api/upload",
        multipart={
            "client_request_id": "browser-health-upload",
            "file": {
                "name": "browser-health.txt",
                "mimeType": "text/plain",
                "buffer": b"health summary",
            },
        },
    )
    assert upload.ok
    browser_session.context.clear_cookies()

    _open_locked_application(browser_session)
    _unlock(page)
    expect(page.locator("#transferPage")).to_be_visible()
    expect(page.locator(".health")).to_be_hidden()
    assert page.locator("#transferPage #metricCount").count() == 0
    assert page.locator("#transferPage #metricSize").count() == 0

    page.locator('.sidebar [data-route="manage"]').click()
    _assert_route_state(page, "manage", ".sidebar")
    expect(page.locator(".health")).to_be_visible()
    expect(page.locator("#metricCount")).to_have_text("1")
    expect(page.locator("#metricSize")).not_to_have_text("0 B")
    _assert_browser_clean(browser_session)


@pytest.mark.parametrize(
    ("viewport", "health_columns", "manage_columns", "use_dark_theme"),
    [
        pytest.param(
            {"width": 1440, "height": 900}, 3, 2, True, id="desktop-1440-dark"
        ),
        pytest.param(
            {"width": 1024, "height": 768}, 2, 2, False, id="tablet-1024-light"
        ),
        pytest.param(
            {"width": 720, "height": 812}, 1, 1, False, id="mobile-720-light"
        ),
        pytest.param(
            {"width": 390, "height": 844}, 1, 1, False, id="mobile-390-light"
        ),
        pytest.param(
            {"width": 375, "height": 667}, 1, 1, False, id="mobile-375-light"
        ),
    ],
)
def test_manage_cards_reflow_without_horizontal_overflow(
    browser_session: BrowserSession,
    viewport: dict[str, int],
    health_columns: int,
    manage_columns: int,
    use_dark_theme: bool,
) -> None:
    page = browser_session.page
    page.set_viewport_size(viewport)
    _open_locked_application(browser_session)
    _unlock(page)
    nav_selector = ".sidebar" if viewport["width"] > 720 else ".mobile-nav"
    page.locator(f'{nav_selector} [data-route="manage"]').click()
    page.locator("#connectionPanel").wait_for(state="visible")

    if use_dark_theme:
        page.locator("#manageThemeToggle").click()
        expect(page.locator("html")).to_have_class(re.compile(r"\bdark\b"))
    else:
        expect(page.locator("html")).not_to_have_class(re.compile(r"\bdark\b"))

    page.evaluate(
        """
        () => {
          const longToken = 'status-storage-value-'.repeat(24);
          document.querySelector('#connectionDetail').textContent = longToken;
          document.querySelector('#connectionDevice').textContent = longToken;
          document.querySelector('#connectionEvents').textContent = longToken;
          document.querySelector('#storageSummary').textContent = longToken;
        }
        """
    )

    geometry = page.evaluate(
        """
        () => {
          const columnCount = selector => {
            const value = getComputedStyle(document.querySelector(selector))
              .gridTemplateColumns;
            if (!value || value === 'none' || value === 'auto') return 1;
            return value.trim().split(/\\s+/).length;
          };
          const settingBodies = [...document.querySelectorAll(
            '.manage-setting-panel .manage-panel-body'
          )].map(element => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return {
              height: rect.height,
              minHeight: style.minHeight,
              alignItems: style.alignItems,
              justifyContent: style.justifyContent,
            };
          });
          const primaryHeights = [...document.querySelectorAll('.manage-primary-panel')]
            .map(element => element.getBoundingClientRect().height);
          const settingHeights = [...document.querySelectorAll('.manage-setting-panel')]
            .map(element => element.getBoundingClientRect().height);
          return {
            healthColumns: columnCount('.health'),
            manageColumns: columnCount('.manage-grid'),
            settingBodies,
            primaryHeights,
            settingHeights,
          };
        }
        """
    )
    assert geometry["healthColumns"] == health_columns, geometry
    assert geometry["manageColumns"] == manage_columns, geometry
    assert all(
        body["minHeight"] == "76px" for body in geometry["settingBodies"]
    ), geometry
    if viewport["width"] > 720:
        assert all(
            body["alignItems"] == "center"
            and body["justifyContent"] == "flex-end"
            for body in geometry["settingBodies"]
        ), geometry
        assert max(geometry["settingHeights"]) < min(geometry["primaryHeights"]), geometry
    else:
        assert all(
            body["alignItems"] == "stretch" for body in geometry["settingBodies"]
        ), geometry

    control_boxes = page.locator(
        "#railRefresh, #refreshOpsBtn, #manageThemeToggle, #logoutButton"
    ).evaluate_all(
        """
        elements => elements.map(element => {
          const rect = element.getBoundingClientRect();
          return { id: element.id, width: rect.width, height: rect.height };
        })
        """
    )
    assert all(
        box["width"] >= 44 and box["height"] >= 44 for box in control_boxes
    ), control_boxes
    if viewport["width"] <= 720:
        for selector in ("#manageThemeToggle", "#logoutButton"):
            button = page.locator(selector)
            body = button.locator("xpath=parent::*")
            button_box = button.bounding_box()
            body_box = body.bounding_box()
            assert button_box is not None and body_box is not None
            body_padding = body.evaluate(
                """
                element => {
                  const style = getComputedStyle(element);
                  return Number.parseFloat(style.paddingLeft)
                    + Number.parseFloat(style.paddingRight);
                }
                """
            )
            assert abs(button_box["width"] - (body_box["width"] - body_padding)) <= 1

    for selector in (
        "#connectionDetail",
        "#connectionDevice",
        "#connectionEvents",
        "#storageSummary",
    ):
        overflow = page.locator(selector).evaluate(
            "element => ({ clientWidth: element.clientWidth, scrollWidth: element.scrollWidth })"
        )
        assert overflow["scrollWidth"] <= overflow["clientWidth"] + 1, {
            "selector": selector,
            **overflow,
        }
    _assert_no_horizontal_overflow(page)
    _assert_browser_clean(browser_session)


def test_route_history_restores_distinct_scroll_positions_without_focus_move(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    _open_locked_application(browser_session)
    _unlock(page)
    page.evaluate(
        """
        () => {
          for (const route of ['filesPage', 'managePage']) {
            const page = document.getElementById(route);
            for (let index = 0; index < 18; index += 1) {
              const filler = document.createElement('div');
              filler.className = 'timeline-empty';
              filler.textContent = `scroll filler ${index}`;
              page.append(filler);
            }
          }
        }
        """
    )

    page.locator('.sidebar [data-route="files"]').click()
    _assert_route_state(page, "files", ".sidebar")
    page.evaluate("window.scrollTo(0, 420)")
    page.wait_for_function("window.scrollY === 420")

    page.locator('.sidebar [data-route="manage"]').click()
    _assert_route_state(page, "manage", ".sidebar")
    page.evaluate("window.scrollTo(0, 760)")
    page.wait_for_function("window.scrollY === 760")
    stable_control = page.locator("#themeToggle")
    stable_control.focus()
    expect(stable_control).to_be_focused()

    page.go_back()
    _assert_route_state(page, "files", ".sidebar", expect_heading_focus=False)
    page.wait_for_function("window.scrollY === 420")
    expect(stable_control).to_be_focused()

    page.go_forward()
    _assert_route_state(page, "manage", ".sidebar", expect_heading_focus=False)
    page.wait_for_function("window.scrollY === 760")
    expect(stable_control).to_be_focused()
    _assert_browser_clean(browser_session)


def test_mobile_batch_toolbar_selection_clear_and_route_exit(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    page.set_viewport_size({"width": 390, "height": 568})
    _create_seed_session(browser_session, "playwright-batch-seed")
    upload = browser_session.context.request.post(
        f"{browser_session.base_url}/api/upload",
        multipart={
            "client_request_id": "browser-batch-upload",
            "file": {
                "name": "browser-batch.txt",
                "mimeType": "text/plain",
                "buffer": b"browser batch toolbar",
            },
        },
    )
    assert upload.ok
    browser_session.context.clear_cookies()

    _open_locked_application(browser_session)
    _unlock(page)
    page.locator('.mobile-nav [data-route="files"]').click()
    checkbox = page.locator("[data-select-message]").first
    expect(checkbox).to_be_visible()
    checkbox.check()

    toolbar = page.locator("#batchToolbar")
    _assert_batch_toolbar_state(toolbar, visible=True)
    toolbar_buttons = toolbar.locator("button").evaluate_all(
        "elements => elements.map(element => getComputedStyle(element).minHeight)"
    )
    assert toolbar_buttons
    assert all(float(value.removesuffix("px")) >= 44 for value in toolbar_buttons)

    page.locator("#batchToolbarClear").click()
    expect(checkbox).not_to_be_checked()
    _assert_batch_toolbar_state(toolbar, visible=False)

    checkbox.check()
    _assert_batch_toolbar_state(toolbar, visible=True)
    page.locator('.mobile-nav [data-route="manage"]').click()
    _assert_route_state(page, "manage", ".mobile-nav")
    _assert_batch_toolbar_state(toolbar, visible=False)

    page.locator('.mobile-nav [data-route="files"]').click()
    _assert_route_state(page, "files", ".mobile-nav")
    expect(checkbox).not_to_be_checked()
    _assert_batch_toolbar_state(toolbar, visible=False)
    _assert_browser_clean(browser_session)


def test_old_page_anchor_restoration_has_no_smooth_drift_and_focuses_fallback(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    context = browser_session.context

    session_response = context.request.post(
        f"{browser_session.base_url}/api/session",
        data={
            "access_token": UPLOAD_TOKEN,
            "device_id": "playwright-seed",
            "device_name": "Playwright seed",
        },
    )
    assert session_response.ok
    for index in range(60):
        response = context.request.post(
            f"{browser_session.base_url}/api/messages",
            data={
                "body": f"browser-seed-{index:03d}",
                "client_request_id": f"browser-seed-{index:03d}",
            },
        )
        assert response.ok
    first_page = context.request.get(
        f"{browser_session.base_url}/api/messages?limit=50"
    )
    assert first_page.ok
    next_before = first_page.json()["next_before"]
    assert next_before
    older_page = context.request.get(
        f"{browser_session.base_url}/api/messages?limit=50&before={next_before}"
    )
    assert older_page.ok
    anchor_id = older_page.json()["items"][0]["id"]
    assert anchor_id not in {item["id"] for item in first_page.json()["items"]}
    context.clear_cookies()

    paged_requests: list[str] = []
    page.on(
        "request",
        lambda request: paged_requests.append(request.url)
        if "/api/messages?" in request.url and "before=" in request.url
        else None,
    )
    _open_locked_application(browser_session)
    page.evaluate(
        "sessionStorage.setItem('transfer-last-sequence', String(Number.MAX_SAFE_INTEGER))"
    )
    _unlock(page)
    for _ in range(400):
        if page.evaluate("sessionStorage.getItem('transfer-last-sequence')") == "60":
            break
        page.wait_for_timeout(25)
    else:
        raise AssertionError("ahead-of-database cursor was not reset to the replay target")
    paged_requests.clear()

    anchor = page.locator(f'[data-message-id="{anchor_id}"]')
    container = page.locator("#timelineContainer")
    for _ in range(200):
        if paged_requests:
            break
        container.evaluate("element => { element.scrollTop = element.scrollHeight; }")
        page.wait_for_timeout(25)
        container.evaluate("element => { element.scrollTop = 0; }")
        page.wait_for_timeout(25)
    assert paged_requests
    expect(anchor).to_be_attached(timeout=10_000)

    snapshot = page.evaluate(
        """
        anchorId => {
          const container = document.getElementById('timelineContainer');
          const anchor = container.querySelector(`[data-message-id="${anchorId}"]`);
          const temporaryAction = document.createElement('button');
          temporaryAction.type = 'button';
          temporaryAction.dataset.timelineAction = 'removed-after-reload';
          temporaryAction.textContent = 'temporary action';
          anchor.append(temporaryAction);
          temporaryAction.focus({ preventScroll: true });
          const containerRect = container.getBoundingClientRect();
          const anchorRect = anchor.getBoundingClientRect();
          container.scrollTop += anchorRect.top - containerRect.top + 1;
          const adjustedContainerRect = container.getBoundingClientRect();
          const adjustedAnchorRect = anchor.getBoundingClientRect();
          const firstVisible = Array.from(container.querySelectorAll('.timeline-message'))
            .find(message => {
              const rect = message.getBoundingClientRect();
              return rect.bottom > adjustedContainerRect.top
                && rect.top < adjustedContainerRect.bottom;
            });
          const visibleMessages = Array.from(container.querySelectorAll('.timeline-message'))
            .filter(message => {
              const rect = message.getBoundingClientRect();
              return rect.bottom > adjustedContainerRect.top
                && rect.top < adjustedContainerRect.bottom;
            })
            .map(message => ({
              text: message.querySelector('.timeline-message-body').textContent,
              top: message.getBoundingClientRect().top - adjustedContainerRect.top,
            }));
          return {
            offset: adjustedAnchorRect.top - adjustedContainerRect.top,
            firstVisibleId: firstVisible && firstVisible.dataset.messageId,
            firstVisibleText: firstVisible && firstVisible.textContent,
            scrollTop: container.scrollTop,
            scrollHeight: container.scrollHeight,
            clientHeight: container.clientHeight,
            visibleMessages,
            temporaryActionFocused: document.activeElement === temporaryAction,
          };
        }
        """,
        anchor_id,
    )
    assert snapshot["firstVisibleId"] == anchor_id, snapshot["visibleMessages"]
    assert snapshot["temporaryActionFocused"] is True

    page.evaluate("window.dispatchEvent(new CustomEvent('session-expired'))")
    expect(page.get_by_role("dialog", name="访问验证")).to_be_visible()
    _unlock(page)

    expect(container).to_be_focused()
    restored_offset = anchor.evaluate(
        """
        element => {
          const container = document.getElementById('timelineContainer');
          return element.getBoundingClientRect().top
            - container.getBoundingClientRect().top;
        }
        """
    )
    page.wait_for_timeout(700)
    stable_offset = anchor.evaluate(
        """
        element => {
          const container = document.getElementById('timelineContainer');
          return element.getBoundingClientRect().top
            - container.getBoundingClientRect().top;
        }
        """
    )
    smooth_calls = page.evaluate(
        """
        window.__scrollIntoViewCalls.filter(
          options => options && options.behavior === 'smooth'
        ).length
        """
    )
    assert abs(restored_offset - snapshot["offset"]) <= 2
    assert abs(stable_offset - restored_offset) <= 1
    assert smooth_calls == 0
    _assert_browser_clean(browser_session)


def test_upload_refresh_reselect_rejects_mismatch_and_sends_missing_parts_only(
    browser_session: BrowserSession, tmp_path: Path
) -> None:
    page = browser_session.page
    source_file = tmp_path / "resume.bin"
    mismatch_dir = tmp_path / "mismatch"
    mismatch_dir.mkdir()
    mismatch_file = mismatch_dir / source_file.name
    source_file.write_bytes(b"a" * (8 * 1024 * 1024) + b"z")
    mismatch_file.write_bytes(b"b" * (8 * 1024 * 1024) + b"y")
    stable_mtime_ns = 1_784_500_000_000_000_000
    os.utime(source_file, ns=(stable_mtime_ns, stable_mtime_ns))
    os.utime(mismatch_file, ns=(stable_mtime_ns, stable_mtime_ns))

    _open_locked_application(browser_session)
    _unlock(page)
    sent_parts: list[int] = []

    def record_part_request(request: object) -> None:
        match = re.search(r"/parts/(\d+)$", request.url)  # type: ignore[attr-defined]
        if match:
            sent_parts.append(int(match.group(1)))

    page.on("request", record_part_request)
    page.route("**/api/uploads/*/parts/1", lambda route: route.abort())
    page.locator("#composerFileInput").set_input_files(source_file)
    active = _wait_for_active_upload(
        browser_session.context,
        browser_session.base_url,
        lambda upload: upload.get("confirmed_parts") == [0],
    )
    upload_id = str(active["upload_id"])
    upload_card = page.locator(f'[data-upload-id="{upload_id}"]')
    expect(upload_card).to_be_visible()

    page.reload(wait_until="domcontentloaded")
    reselect = page.locator(
        f'[data-upload-id="{upload_id}"] [data-upload-action="reselect"]'
    )
    expect(reselect).to_be_visible(timeout=10_000)
    expect(page.locator(f'[data-upload-id="{upload_id}"] .upload-card-status')).to_have_text(
        "需要重新选择原文件"
    )

    reselect.click()
    page.locator("#uploadReselectInput").set_input_files(mismatch_file)
    expect(page.locator(f'[data-upload-id="{upload_id}"] .upload-card-error')).to_have_text(
        "所选文件与原文件不一致"
    )

    page.unroute("**/api/uploads/*/parts/1")
    sent_parts.clear()
    reselect.click()
    page.locator("#uploadReselectInput").set_input_files(source_file)
    expect(page.locator(f'[data-upload-id="{upload_id}"]')).to_have_count(0, timeout=20_000)
    expect(page.locator(f'[data-message-id] [href="/download/{upload_id}"]')).to_have_count(
        1, timeout=20_000
    )
    assert sent_parts == [1]
    browser_session.console_messages[:] = [
        message for message in browser_session.console_messages
        if "net::ERR_FAILED" not in message
    ]
    _assert_browser_clean(browser_session)


def test_attachment_picker_persists_real_handle_and_reload_auto_resumes(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    _open_locked_application(browser_session)
    _unlock(page)
    supported = page.evaluate(
        """
        async () => {
          if (!navigator.storage?.getDirectory) return false;
          const root = await navigator.storage.getDirectory();
          const handle = await root.getFileHandle('picker-resume.bin', { create: true });
          const writable = await handle.createWritable();
          await writable.write(new Uint8Array(8 * 1024 * 1024 + 1));
          await writable.close();
          window.__productionPickerHandle = handle;
          window.showOpenFilePicker = options => {
            window.__productionPickerOptions = options;
            return Promise.resolve([handle]);
          };
          return typeof handle.getFile === 'function';
        }
        """
    )
    if not supported:
        pytest.skip("Chromium does not expose a cloneable FileSystemFileHandle")

    page.route("**/api/uploads/*/parts/1", lambda route: route.abort())
    page.locator("#composerAttachBtn").click()
    active = _wait_for_active_upload(
        browser_session.context,
        browser_session.base_url,
        lambda upload: upload.get("confirmed_parts") == [0],
    )
    upload_id = str(active["upload_id"])
    assert page.evaluate("window.__productionPickerOptions.multiple") is True
    persisted_handle = page.evaluate(
        """
        uploadId => new Promise((resolve, reject) => {
          const open = indexedDB.open('personal-transfer-timeline', 1);
          open.onerror = () => reject(open.error);
          open.onsuccess = () => {
            const request = open.result.transaction('upload-tasks', 'readonly')
              .objectStore('upload-tasks').get(uploadId);
            request.onerror = () => reject(request.error);
            request.onsuccess = () => resolve({
              kind: request.result?.fileHandle?.kind,
              hasGetFile: typeof request.result?.fileHandle?.getFile === 'function',
            });
          };
        })
        """,
        upload_id,
    )
    assert persisted_handle == {"kind": "file", "hasGetFile": True}

    page.reload(wait_until="domcontentloaded")
    page.unroute("**/api/uploads/*/parts/1")
    expect(page.locator(f'[data-upload-id="{upload_id}"]')).to_have_count(0, timeout=20_000)
    expect(page.locator(f'[data-message-id] [href="/download/{upload_id}"]')).to_have_count(
        1, timeout=20_000
    )
    browser_session.console_messages[:] = [
        message for message in browser_session.console_messages
        if "net::ERR_FAILED" not in message
    ]
    _assert_browser_clean(browser_session)


def test_window_open_copied_cursor_reconciles_then_tabs_advance_independently(
    browser_session: BrowserSession,
) -> None:
    page_a = browser_session.page
    context = browser_session.context
    _open_locked_application(browser_session)
    _unlock(page_a)
    expect(page_a.locator("#connectionStatus")).to_have_text("已连接", timeout=10_000)

    active_response = context.request.post(
        f"{browser_session.base_url}/api/uploads",
        data={
            "client_request_id": "window-open-active-upload",
            "name": "window-open-active.bin",
            "size_bytes": 4,
            "mime_type": "application/octet-stream",
            "last_modified_ms": 1_784_412_345_000,
            "chunk_size_bytes": 8 * 1024 * 1024,
            "sample_sha256": "0" * 64,
        },
    )
    assert active_response.ok
    upload_id = active_response.json()["upload_id"]
    current_response = context.request.post(
        f"{browser_session.base_url}/api/messages",
        data={
            "body": "state before window open",
            "client_request_id": "state-before-window-open",
        },
    )
    assert current_response.ok
    current_message_id = current_response.json()["id"]
    expect(page_a.locator(f'[data-upload-id="{upload_id}"]')).to_be_visible(timeout=10_000)
    expect(page_a.locator(f'[data-message-id="{current_message_id}"]')).to_be_visible(
        timeout=10_000
    )
    page_a.wait_for_function(
        "Number(sessionStorage.getItem('transfer-last-sequence') || 0) > 0"
    )
    cursor_before_open = page_a.evaluate(
        "Number(sessionStorage.getItem('transfer-last-sequence') || 0)"
    )

    with context.expect_page() as page_b_info:
        page_a.evaluate("window.open('about:blank', 'copied-cursor-tab')")
    page_b = page_b_info.value
    page_b.wait_for_load_state("domcontentloaded")
    copied_cursor = page_b.evaluate(
        "Number(sessionStorage.getItem('transfer-last-sequence') || 0)"
    )
    assert copied_cursor == cursor_before_open

    page_b_requests: list[str] = []
    page_b_websockets: list[str] = []
    page_b.on("request", lambda request: page_b_requests.append(request.url))
    page_b.on("websocket", lambda websocket: page_b_websockets.append(websocket.url))
    try:
        page_b.goto("/", wait_until="domcontentloaded")
        expect(page_b.locator("#mainContent")).to_be_visible(timeout=10_000)
        expect(page_b.locator("#connectionStatus")).to_have_text("已连接", timeout=10_000)
        expect(page_b.locator(f'[data-upload-id="{upload_id}"]')).to_be_visible(timeout=10_000)
        expect(page_b.locator(f'[data-message-id="{current_message_id}"]')).to_be_visible(
            timeout=10_000
        )
        assert any(url.endswith("/api/uploads/active") for url in page_b_requests)
        assert any("/api/messages?limit=50" in url for url in page_b_requests)
        assert page_b_websockets[-1].endswith(f"after={copied_cursor}")

        page_b.evaluate(
            "window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }))"
        )

        response = context.request.post(
            f"{browser_session.base_url}/api/messages",
            data={
                "body": "independent cursor replay",
                "client_request_id": "independent-cursor-replay",
            },
        )
        assert response.ok
        message_id = response.json()["id"]
        expect(page_a.locator(f'[data-message-id="{message_id}"]')).to_be_visible(timeout=10_000)
        page_a.wait_for_function(
            "Number(sessionStorage.getItem('transfer-last-sequence') || 0) > 0"
        )
        cursor_a = page_a.evaluate(
            "Number(sessionStorage.getItem('transfer-last-sequence') || 0)"
        )
        cursor_b_before = page_b.evaluate(
            "Number(sessionStorage.getItem('transfer-last-sequence') || 0)"
        )
        assert cursor_b_before == copied_cursor
        assert cursor_b_before < cursor_a
        expect(page_b.locator(f'[data-message-id="{message_id}"]')).to_have_count(0)

        websocket_count = len(page_b_websockets)
        page_b.evaluate(
            "window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))"
        )
        expect(page_b.locator(f'[data-message-id="{message_id}"]')).to_be_visible(timeout=10_000)
        page_b.wait_for_function(
            "expected => Number(sessionStorage.getItem('transfer-last-sequence') || 0) >= expected",
            arg=cursor_a,
        )
        assert len(page_b_websockets) > websocket_count
        assert page_b_websockets[-1].endswith(f"after={cursor_b_before}")
        assert page_b.locator(f'[data-message-id="{message_id}"]').count() == 1
    finally:
        page_b.close()
    _assert_browser_clean(browser_session)


def test_observer_can_cancel_and_remote_cancellation_reaches_both_pages(
    browser_session: BrowserSession, chromium_browser: Browser, tmp_path: Path
) -> None:
    source_page = browser_session.page
    source_file = tmp_path / "observer-cancel.bin"
    source_file.write_bytes(b"c" * (8 * 1024 * 1024 + 1))
    _open_locked_application(browser_session)
    _unlock(source_page)
    expect(source_page.locator("#connectionStatus")).to_have_text("已连接", timeout=10_000)
    sent_parts: list[int] = []
    failed_requests: list[str] = []
    finished_requests: list[str] = []
    cdp: CDPSession | None = None
    observer_context: BrowserContext | None = None

    def part_index(request: object) -> int | None:
        match = re.search(r"/parts/(\d+)$", request.url)  # type: ignore[attr-defined]
        return int(match.group(1)) if match else None

    try:
        cdp = browser_session.context.new_cdp_session(source_page)
        browser_session.cdp_sessions.append(cdp)
        cdp.send("Network.enable")
        cdp.send(
            "Network.emulateNetworkConditions",
            {
                "offline": False,
                "latency": 0,
                "downloadThroughput": -1,
                "uploadThroughput": 32 * 1024,
                "connectionType": "cellular3g",
            },
        )
        source_page.on(
            "request",
            lambda request: sent_parts.append(index)
            if (index := part_index(request)) is not None
            else None,
        )
        source_page.on(
            "requestfailed",
            lambda request: failed_requests.append(request.url)  # type: ignore[attr-defined]
            if part_index(request) is not None
            else None,
        )
        source_page.on(
            "requestfinished",
            lambda request: finished_requests.append(request.url)  # type: ignore[attr-defined]
            if part_index(request) is not None
            else None,
        )

        observer_context = chromium_browser.new_context(
            base_url=browser_session.base_url,
            viewport={"width": 390, "height": 844},
        )
        observer_page = observer_context.new_page()
        with source_page.expect_request(
            lambda request: request.method == "PUT" and part_index(request) == 0,
            timeout=60_000,
        ) as pending_request_info:
            source_page.locator("#composerFileInput").set_input_files(source_file)
        pending_request = pending_request_info.value
        upload_id = pending_request.url.split("/api/uploads/", 1)[1].split("/", 1)[0]
        expect(source_page.locator(f'[data-upload-id="{upload_id}"]')).to_be_visible()
        assert pending_request.url not in finished_requests
        assert pending_request.url not in failed_requests

        observer_page.goto("/", wait_until="domcontentloaded")
        _unlock(observer_page)
        expect(observer_page.locator("#connectionStatus")).to_have_text("已连接", timeout=10_000)
        observer_card = observer_page.locator(f'[data-upload-id="{upload_id}"]')
        expect(observer_card).to_be_visible(timeout=10_000)
        expect(observer_card.locator('[data-upload-action="pause"]')).to_have_count(0)
        expect(observer_card.locator('[data-upload-action="resume"]')).to_have_count(0)
        expect(observer_card.locator('[data-upload-action="cancel"]')).to_be_visible()
        cancel_box = observer_card.locator('[data-upload-action="cancel"]').bounding_box()
        assert cancel_box is not None
        assert cancel_box["width"] >= 44 and cancel_box["height"] >= 44
        with observer_page.expect_response(
            lambda response: response.request.method == "DELETE"
            and response.url.endswith(f"/api/uploads/{upload_id}")
        ) as cancel_response:
            observer_card.locator('[data-upload-action="cancel"]').click()
        assert cancel_response.value.status == 200
        expect(observer_card.locator(".upload-card-status")).to_have_text("已取消")
        source_status = source_page.locator(
            f'[data-upload-id="{upload_id}"] .upload-card-status'
        )
        try:
            expect(source_status).to_have_text("已取消", timeout=10_000)
        except AssertionError as error:
            details = source_page.evaluate("""
              () => ({
                sequence: sessionStorage.getItem('transfer-last-sequence'),
                connection: document.getElementById('connectionStatus').textContent,
              })
            """)
            raise AssertionError(f"source did not apply cancellation: {details!r}") from error
        for _ in range(200):
            if pending_request.url in failed_requests:
                break
            source_page.wait_for_timeout(25)
        assert pending_request.url in failed_requests
        assert pending_request.url not in finished_requests
        source_page.wait_for_timeout(250)
        assert sent_parts == [0]
    finally:
        try:
            if observer_context is not None:
                observer_context.close()
        finally:
            if cdp is not None:
                _cleanup_cdp_network_session(cdp)
    _assert_browser_clean(browser_session)


def test_resumable_40mb_upload_uses_bounded_parts_and_completes(
    browser_session: BrowserSession, tmp_path: Path
) -> None:
    source = tmp_path / "forty-megabytes.bin"
    block = bytes(range(256)) * 4096
    with source.open("wb") as output:
        for _ in range(40):
            output.write(block)

    page = browser_session.page
    page.add_init_script(r"""
      (() => {
        window.__uploadPartBodySizes = [];
        const originalOpen = XMLHttpRequest.prototype.open;
        const originalSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(method, url, ...rest) {
          this.__uploadUrl = String(url);
          return originalOpen.call(this, method, url, ...rest);
        };
        XMLHttpRequest.prototype.send = function(body) {
          if (this.__uploadUrl?.includes('/parts/')) {
            window.__uploadPartBodySizes.push(Number(body?.size ?? body?.byteLength ?? 0));
          }
          return originalSend.call(this, body);
        };
      })();
    """)
    _open_locked_application(browser_session)
    _unlock(page)
    content_ranges: list[str] = []

    def record_part_range(request: Request) -> None:
        if "/parts/" not in request.url:
            return
        content_ranges.append(request.headers["content-range"])

    page.on("requestfinished", record_part_range)
    try:
        page.locator("#composerFileInput").set_input_files(str(source))
        expect(page.locator('[data-upload-status="complete"]')).to_be_visible(
            timeout=120_000
        )
    finally:
        page.remove_listener("requestfinished", record_part_range)

    expected_part_size = 8 * 1024 * 1024
    body_sizes = page.evaluate("window.__uploadPartBodySizes")
    assert body_sizes == [expected_part_size] * 5
    assert sum(body_sizes) == 40 * 1024 * 1024
    assert content_ranges == [
        f"bytes {index * expected_part_size}-{(index + 1) * expected_part_size - 1}/41943040"
        for index in range(5)
    ]
    _assert_browser_clean(browser_session)


def test_eleven_files_complete_after_showing_nine_uploading_and_two_queued(
    browser_session: BrowserSession, tmp_path: Path
) -> None:
    paths = create_test_files(tmp_path, count=11, size_bytes=16 * 1024 * 1024)
    page = browser_session.page
    release_upload_creations_together(page, len(paths))
    hold_part_requests_until_released(page)
    _open_locked_application(browser_session)
    _unlock(page)
    with track_part_request_concurrency(page) as concurrency:
        try:
            page.locator("#composerFileInput").set_input_files(
                [str(path) for path in paths]
            )
            expect(page.locator('[data-upload-status="uploading"]')).to_have_count(
                9, timeout=30_000
            )
            expect(page.locator('[data-upload-status="queued"]')).to_have_count(2)
            deadline = time.monotonic() + 30
            while (
                page.evaluate("window.__heldUploadPartCount()") < 9
                and time.monotonic() < deadline
            ):
                page.wait_for_timeout(25)
            assert page.evaluate("window.__heldUploadPartCount()") == 9
            assert page.evaluate("window.__releaseUploadParts()") == 9

            expect(page.locator('[data-upload-status="complete"]')).to_have_count(
                11, timeout=120_000
            )
            expect(page.locator('[data-upload-status="uploading"]')).to_have_count(0)
            expect(page.locator('[data-upload-status="queued"]')).to_have_count(0)
        finally:
            page.evaluate("window.__releaseUploadParts()")

        page.wait_for_timeout(100)
        assert page.evaluate("window.__heldUploadPartCount()") == 0
        assert 1 <= concurrency["peak"] <= 9
        assert concurrency["active"] == 0
    _assert_browser_clean(browser_session)


@pytest.mark.parametrize(
    "viewport",
    [
        pytest.param({"width": 1440, "height": 900}, id="1440x900"),
        pytest.param({"width": 1024, "height": 768}, id="1024x768"),
        pytest.param({"width": 390, "height": 844}, id="390x844"),
        pytest.param({"width": 375, "height": 667}, id="375x667"),
    ],
)
@pytest.mark.parametrize("route", ["transfer", "files", "manage"])
def test_workspace_density_matrix(
    browser_session: BrowserSession,
    viewport: dict[str, int],
    route: str,
) -> None:
    page = browser_session.page
    page.set_viewport_size(viewport)
    _open_locked_application(browser_session)
    _unlock(page)
    nav_selector = ".sidebar" if viewport["width"] > 720 else ".mobile-nav"
    page.locator(f'{nav_selector} [data-route="{route}"]').click()
    page.locator(f'[data-route-page="{route}"]').wait_for(state="visible")
    _assert_no_horizontal_overflow(page)
    assert page.locator(f'[data-route-heading="{route}"]').get_attribute("tabindex") == "-1"
    _assert_browser_clean(browser_session)


def test_density_layout_preserves_dark_theme_and_reduced_motion(
    browser_session: BrowserSession,
) -> None:
    page = browser_session.page
    _open_locked_application(browser_session)
    _unlock(page)
    page.emulate_media(reduced_motion="reduce")
    page.locator("#themeToggle").click()
    assert page.locator("html").evaluate("node => node.classList.contains('dark')")
    scroll_behavior = page.locator("#timelineContainer").evaluate(
        "node => getComputedStyle(node).scrollBehavior"
    )
    assert scroll_behavior == "auto"
    transition_duration = page.locator("#timelineContainer").evaluate(
        "node => getComputedStyle(node).transitionDuration"
    )
    assert float(transition_duration.rstrip("s")) <= 0.0001
    animation_duration = page.locator("#timelineContainer").evaluate(
        "node => getComputedStyle(node).animationDuration"
    )
    assert float(animation_duration.rstrip("s")) <= 0.0001
    _assert_browser_clean(browser_session)
