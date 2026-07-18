from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect, sync_playwright


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
    expect(page.locator("#timelineView")).to_be_visible()


def _assert_browser_clean(session: BrowserSession) -> None:
    session.page.wait_for_timeout(100)
    csp_violations = session.page.evaluate("window.__cspViolations")
    assert session.console_messages == []
    assert session.page_errors == []
    assert csp_violations == []
    assert session.allowed_console_messages == [EXPECTED_AUTH_CONSOLE_EXPLANATION]
    assert len(session.document_csp_headers) == 1


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
    expect(page.locator("#timelineView")).to_be_focused()
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

    skip_link = page.locator("#skipLink")
    page.evaluate(
        """
        () => {
          const timeline = document.getElementById('timelineView');
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
    expect(page.locator("#timelineView")).to_be_focused()
    assert page.evaluate("location.hash") == "#timelineView"
    assert page.evaluate("window.__skipLinkFocusCalls") == 1

    page.evaluate("history.replaceState(null, '', location.pathname + location.search)")
    skip_link.focus()
    skip_link.click()
    expect(page.locator("#timelineView")).to_be_focused()
    assert page.evaluate("location.hash") == "#timelineView"
    assert page.evaluate("window.__skipLinkFocusCalls") == 2

    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    major_button_boxes = page.locator(
        "button.btn-primary:visible, #gridViewBtn:visible, #listViewBtn:visible, "
        "#composerAttachBtn:visible"
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
    seeded_messages = []
    for index in range(60):
        response = context.request.post(
            f"{browser_session.base_url}/api/messages",
            data={
                "body": f"browser-seed-{index:03d}",
                "client_request_id": f"browser-seed-{index:03d}",
            },
        )
        assert response.ok
        seeded_messages.append(response.json())
    context.clear_cookies()

    paged_requests: list[str] = []
    page.on(
        "request",
        lambda request: (
            paged_requests.append(request.url)
            if "/api/messages?" in request.url and "before=" in request.url
            else None
        ),
    )
    _open_locked_application(browser_session)
    _unlock(page)

    anchor_id = seeded_messages[5]["id"]
    anchor = page.locator(f'[data-message-id="{anchor_id}"]')
    container = page.locator("#timelineContainer")
    container.evaluate("element => { element.scrollTop = 0; }")
    expect(anchor).to_be_attached(timeout=10_000)
    request_deadline = time.monotonic() + 5.0
    while not paged_requests and time.monotonic() < request_deadline:
        page.wait_for_timeout(25)
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
