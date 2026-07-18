export const ROUTES = Object.freeze({
  transfer: { hash: '#transfer', label: '传输', title: '传输工作台' },
  files: { hash: '#files', label: '文件', title: '全部文件' },
  manage: { hash: '#manage', label: '管理', title: '管理与设置' },
});

export function normalizeRoute(hash) {
  const route = String(hash || '').replace(/^#/, '');
  return Object.prototype.hasOwnProperty.call(ROUTES, route) ? route : 'transfer';
}

export function createNavigation({ windowObject, documentObject, onRouteChange = () => {} }) {
  let currentRoute = null;
  const scrollPositions = new Map();
  const buttonHandlers = new Map();
  let focusNextRoute = false;
  let started = false;

  function applyRoute(route) {
    const normalized = normalizeRoute(`#${route}`);
    if (currentRoute) scrollPositions.set(currentRoute, windowObject.scrollY || 0);
    documentObject.querySelectorAll('[data-route-page]').forEach(page => {
      page.hidden = page.dataset.routePage !== normalized;
    });
    documentObject.querySelectorAll('[data-route]').forEach(button => {
      const active = button.dataset.route === normalized;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    const config = ROUTES[normalized];
    const title = documentObject.querySelector('[data-route-title]');
    if (title) title.textContent = config.title;
    documentObject.title = `${config.title} · MonkeyCode`;
    currentRoute = normalized;
    onRouteChange(normalized);
    windowObject.scrollTo(0, scrollPositions.get(normalized) || 0);
    if (focusNextRoute) {
      documentObject.querySelector(`[data-route-heading="${normalized}"]`)?.focus({ preventScroll: true });
      focusNextRoute = false;
    }
  }

  function handleHashChange() {
    applyRoute(normalizeRoute(windowObject.location.hash));
  }

  function navigate(route) {
    const normalized = normalizeRoute(`#${route}`);
    focusNextRoute = true;
    if (windowObject.location.hash === ROUTES[normalized].hash) applyRoute(normalized);
    else windowObject.location.hash = ROUTES[normalized].hash;
  }

  function start() {
    if (started) return;
    started = true;
    documentObject.querySelectorAll('[data-route]').forEach(button => {
      const handleClick = () => navigate(button.dataset.route);
      buttonHandlers.set(button, handleClick);
      button.addEventListener('click', handleClick);
    });
    windowObject.addEventListener('hashchange', handleHashChange);
    const route = normalizeRoute(windowObject.location.hash);
    if (windowObject.location.hash !== ROUTES[route].hash) {
      windowObject.history.replaceState(null, '', ROUTES[route].hash);
    }
    applyRoute(route);
  }

  function destroy() {
    if (!started) return;
    buttonHandlers.forEach((handleClick, button) => {
      button.removeEventListener('click', handleClick);
    });
    buttonHandlers.clear();
    windowObject.removeEventListener('hashchange', handleHashChange);
    focusNextRoute = false;
    started = false;
  }

  return { start, navigate, getCurrentRoute: () => currentRoute, destroy };
}
