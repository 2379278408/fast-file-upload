from __future__ import annotations

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
    Locator,
    Page,
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


class ServerStartError(RuntimeError):
    def __init__(self, message: str, *, address_in_use: bool = False) -> None:
        super().__init__(message)
        self.address_in_use = address_in_use


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
        )
    finally:
        context.close()


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
    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
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
        assert composer.evaluate(
            "element => getComputedStyle(element).position"
        ) == "fixed"
        assert timeline_metrics["height"] >= 120
        assert timeline_metrics["overflowY"] == "auto"
        assert timeline_metrics["top"] >= 0
        assert timeline_metrics["bottom"] <= composer_box["y"] + 1, {
            "timeline": timeline_metrics,
            "composer": composer_box,
        }
        assert composer_box["y"] >= 0
        assert composer_box["y"] + composer_box["height"] <= mobile_nav_box["y"]
        if viewport == {"width": 390, "height": 568}:
            assert timeline_metrics["scrollHeight"] > timeline_metrics["clientHeight"]

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
    page.evaluate(
        """
        window.dispatchEvent(new CustomEvent('timeline-error', {
          detail: { message: 'toast geometry' },
        }))
        """
    )

    toast = page.locator("#toast")
    composer = page.locator("#composerPanel")
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
    attach_box = page.locator("#composerAttachBtn").bounding_box()
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
    context.clear_cookies()

    _open_locked_application(browser_session)
    page.evaluate(
        "localStorage.setItem('transfer-last-sequence', String(Number.MAX_SAFE_INTEGER))"
    )
    _unlock(page)

    anchor = page.locator(f'[data-message-id="{anchor_id}"]')
    container = page.locator("#timelineContainer")
    container.evaluate("element => { element.scrollTop = 0; }")
    expect(anchor).to_be_attached(timeout=10_000)
    paged_requests = page.evaluate(
        "performance.getEntriesByType('resource').map(entry => entry.name)"
        ".filter(name => name.includes('/api/messages?') && name.includes('before='))"
    )
    assert paged_requests

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
    expect(page.locator(f'[data-message-id] [href="/download/{upload_id}"]')).to_have_count(1)
    assert sent_parts == [1]
    browser_session.console_messages[:] = [
        message for message in browser_session.console_messages
        if "net::ERR_FAILED" not in message
    ]
    _assert_browser_clean(browser_session)


def test_event_cursor_is_per_tab_and_reconnect_replays_that_tabs_gap(
    browser_session: BrowserSession,
) -> None:
    page_a = browser_session.page
    context = browser_session.context
    _open_locked_application(browser_session)
    _unlock(page_a)
    expect(page_a.locator("#connectionStatus")).to_have_text("已连接", timeout=10_000)

    page_b = context.new_page()
    page_b_websockets: list[str] = []
    page_b.on("websocket", lambda websocket: page_b_websockets.append(websocket.url))
    try:
        page_b.goto("/", wait_until="domcontentloaded")
        expect(page_b.locator("#mainContent")).to_be_visible(timeout=10_000)
        expect(page_b.locator("#connectionStatus")).to_have_text("已连接", timeout=10_000)
        page_b.evaluate(
            "window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true }))"
        )

        response = context.request.post(
            f"{browser_session.base_url}/api/messages",
            data={
                "body": "tab-specific replay",
                "client_request_id": "tab-specific-replay",
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
    cdp = browser_session.context.new_cdp_session(source_page)
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
    sent_parts: list[int] = []
    failed_requests: list[str] = []
    finished_requests: list[str] = []

    def part_index(request: object) -> int | None:
        match = re.search(r"/parts/(\d+)$", request.url)  # type: ignore[attr-defined]
        return int(match.group(1)) if match else None

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
    try:
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
                sequence: localStorage.getItem('transfer-last-sequence'),
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
        observer_context.close()
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
        cdp.send("Network.disable")
        cdp.detach()
    _assert_browser_clean(browser_session)
