(function () {
  var currentController = null;
  var pageCache = new Map();
  var prefetchStarted = false;

  function cacheKey(url) {
    var target = new URL(url, window.location.href);
    return target.pathname + target.search;
  }

  function isDashboardUrl(url) {
    return url.origin === window.location.origin && url.pathname.indexOf('/dashboard') === 0;
  }

  function navPath(url) {
    var target = new URL(url, window.location.href);
    var path = target.pathname.replace(/\/$/, '');
    return path || '/dashboard';
  }

  function stripEnhancementMarkers(root) {
    if (!root) return;
    root.querySelectorAll('[data-filter-enhanced]').forEach(function (el) {
      delete el.dataset.filterEnhanced;
    });
    root.querySelectorAll('[data-enhanced]').forEach(function (el) {
      delete el.dataset.enhanced;
    });
    root.querySelectorAll('[data-bound]').forEach(function (el) {
      delete el.dataset.bound;
    });
    root.querySelectorAll('[data-device-picker]').forEach(function (el) {
      delete el.dataset.devicePickerEnhanced;
    });
  }

  function stripEnhancementFromHtml(html) {
    return html
      .replace(/\sdata-filter-enhanced="1"/g, '')
      .replace(/\sdata-enhanced="1"/g, '')
      .replace(/\sdata-bound="1"/g, '')
      .replace(/\sdata-device-picker-enhanced="1"/g, '');
  }

  function setBusy(busy) {
    document.body.classList.toggle('dashboard-loading', busy);
  }

  var prefetchAbort = null;

  function replaceScripts(root) {
    root.querySelectorAll('script').forEach(function (oldScript) {
      var script = document.createElement('script');
      Array.from(oldScript.attributes).forEach(function (attr) {
        script.setAttribute(attr.name, attr.value);
      });
      script.text = oldScript.textContent || '';
      oldScript.replaceWith(script);
    });
  }

  function updateNavActiveState(url) {
    var path = navPath(url);
    document.querySelectorAll('a.nav-item, a.mobile-tab-item').forEach(function (link) {
      var linkPath = navPath(link.href);
      link.classList.toggle('active', linkPath === path);
    });
  }

  function syncShell(nextDoc) {
    var nextTitle = nextDoc.querySelector('title');
    if (nextTitle) document.title = nextTitle.textContent;

    var nextBody = nextDoc.body;
    if (nextBody) {
      document.body.dataset.awaitingRealtime = nextBody.dataset.awaitingRealtime || '';
      window.__dhlAwaitingRealtime = document.body.dataset.awaitingRealtime === '1';
    }

    var currentSidebar = document.querySelector('.sidebar-nav');
    var nextSidebar = nextDoc.querySelector('.sidebar-nav');
    if (currentSidebar && nextSidebar) currentSidebar.innerHTML = nextSidebar.innerHTML;

    var currentMobileTabs = document.querySelector('.mobile-tab-bar');
    var nextMobileTabs = nextDoc.querySelector('.mobile-tab-bar');
    if (currentMobileTabs && nextMobileTabs) currentMobileTabs.innerHTML = nextMobileTabs.innerHTML;
  }

  function applyShellFromCache(cached) {
    if (cached.sidebarHtml) {
      var nav = document.querySelector('.sidebar-nav');
      if (nav) nav.innerHTML = cached.sidebarHtml;
    }
    if (cached.mobileTabsHtml) {
      var tabs = document.querySelector('.mobile-tab-bar');
      if (tabs) tabs.innerHTML = cached.mobileTabsHtml;
    }
    if (cached.title) document.title = cached.title;
  }

  function initDynamicContent(root) {
    stripEnhancementMarkers(root);
    replaceScripts(root);
    window.DashboardFilters?.init(root);
    window.DashboardTables?.init(root);
    window.DashboardDevicePicker?.init(root);
  }

  function cacheEntryFromDoc(nextDoc, nextMain) {
    return {
      html: stripEnhancementFromHtml(nextMain.innerHTML),
      sidebarHtml: nextDoc.querySelector('.sidebar-nav')?.innerHTML || '',
      mobileTabsHtml: nextDoc.querySelector('.mobile-tab-bar')?.innerHTML || '',
      title: nextDoc.querySelector('title')?.textContent || document.title,
      awaitingRealtime: nextDoc.body?.dataset?.awaitingRealtime === '1'
    };
  }

  function storePageCache(key, entry) {
    pageCache.set(key, entry);
  }

  function restoreCachedPage(key, url) {
    var cached = pageCache.get(key);
    if (!cached) return false;

    var currentMain = document.querySelector('.main-content');
    if (!currentMain) return false;

    var wrapper = document.createElement('main');
    wrapper.className = 'main-content';
    wrapper.innerHTML = cached.html;
    if (cached.awaitingRealtime) {
      document.body.dataset.awaitingRealtime = '1';
      window.__dhlAwaitingRealtime = true;
    } else {
      document.body.dataset.awaitingRealtime = '';
      window.__dhlAwaitingRealtime = false;
    }
    currentMain.replaceWith(wrapper);
    applyShellFromCache(cached);
    updateNavActiveState(url || key);
    initDynamicContent(wrapper);
    return true;
  }

  function formSearchParams(form) {
    var params = new URLSearchParams();
    new FormData(form).forEach(function (value, key) {
      params.append(key, value);
    });
    return params;
  }

  async function fetchAndCachePage(url, signal) {
    var target = new URL(url, window.location.href);
    if (!isDashboardUrl(target)) return null;
    var key = cacheKey(target.href);
    if (pageCache.has(key)) return pageCache.get(key);

    var response = await fetch(target.href, {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'fetch' },
      signal: signal
    });
    if (!response.ok) return null;

    var html = await response.text();
    var nextDoc = new DOMParser().parseFromString(html, 'text/html');
    var nextMain = nextDoc.querySelector('.main-content');
    if (!nextMain) return null;

    var entry = cacheEntryFromDoc(nextDoc, nextMain);
    storePageCache(key, entry);
    return entry;
  }

  function prefetchDashboardPages() {
    if (prefetchStarted) return;
    prefetchStarted = true;
    prefetchAbort = new AbortController();
    var links = Array.from(document.querySelectorAll('a.nav-item, a.mobile-tab-item'));
    var seen = {};
    var urls = [];
    links.forEach(function (link) {
      var href = link.href;
      var target = new URL(href, window.location.href);
      if (!isDashboardUrl(target)) return;
      // MiX is slow and would occupy the only serverless slot, so skip it.
      if (target.pathname.indexOf('/dashboard/mix') === 0) return;
      var key = cacheKey(href);
      if (seen[key] || key === cacheKey(window.location.href)) return;
      seen[key] = true;
      urls.push(href);
    });

    var idx = 0;
    var inflight = 0;
    var max = 2;
    function pump() {
      while (inflight < max && idx < urls.length) {
        var href = urls[idx++];
        inflight += 1;
        fetchAndCachePage(href, prefetchAbort && prefetchAbort.signal).catch(function () {
          return null;
        }).finally(function () {
          inflight -= 1;
          pump();
        });
      }
    }
    pump();
  }

  function stopPrefetch() {
    if (prefetchAbort) prefetchAbort.abort();
    prefetchAbort = null;
    prefetchStarted = false;
  }

  async function navigateTo(url, opts) {
    var target = new URL(url, window.location.href);
    if (!isDashboardUrl(target)) {
      window.location.href = target.href;
      return false;
    }

    var key = cacheKey(target.href);
    var force = Boolean(opts && opts.force);
    // Background reloads must never interrupt or override a click the user just made.
    var background = Boolean(opts && opts.background);
    if (background && (currentController || cacheKey(window.location.href) !== key)) return false;
    updateNavActiveState(target.href);

    if (!force && pageCache.has(key)) {
      var cached = pageCache.get(key);
      if (cached.awaitingRealtime && !window.__dhlAwaitingRealtime) {
        pageCache.delete(key);
      } else {
        restoreCachedPage(key, target.href);
        if (!opts || opts.updateHistory !== false) {
          history.pushState({}, '', target.href);
        }
        window.scrollTo(0, 0);
        return true;
      }
    }

    if (!background) {
      stopPrefetch();
      setBusy(true);
    }

    if (currentController) currentController.abort();
    currentController = new AbortController();

    try {
      var response = await fetch(target.href, {
        credentials: 'same-origin',
        headers: { 'X-Requested-With': 'fetch' },
        signal: currentController.signal
      });
      if (!response.ok) throw new Error('Navigation failed: ' + response.status);

      var html = await response.text();
      var nextDoc = new DOMParser().parseFromString(html, 'text/html');
      var nextMain = nextDoc.querySelector('.main-content');
      var currentMain = document.querySelector('.main-content');
      if (!nextMain || !currentMain) throw new Error('Navigation target missing');

      if (background && cacheKey(window.location.href) !== key) {
        storePageCache(key, cacheEntryFromDoc(nextDoc, nextMain));
        return false;
      }

      syncShell(nextDoc);
      currentMain.replaceWith(nextMain);
      initDynamicContent(nextMain);
      updateNavActiveState(target.href);

      storePageCache(key, cacheEntryFromDoc(nextDoc, nextMain));

      if (!opts || opts.updateHistory !== false) {
        history.pushState({}, '', target.href);
      }
      window.scrollTo(0, 0);
      return true;
    } catch (error) {
      if (error.name !== 'AbortError' && !background) {
        window.location.href = target.href;
      }
      return false;
    } finally {
      currentController = null;
      if (!background) {
        setBusy(false);
        prefetchDashboardPages();
      }
    }
  }

  function navigateForm(form) {
    var method = (form.getAttribute('method') || 'get').toLowerCase();
    if (method !== 'get') return false;

    var action = form.getAttribute('action') || window.location.pathname;
    var target = new URL(action, window.location.href);
    target.search = formSearchParams(form).toString();
    var prefix = target.pathname;
    Array.from(pageCache.keys()).forEach(function (key) {
      if (key === prefix || key.indexOf(prefix + '?') === 0) {
        pageCache.delete(key);
      }
    });
    navigateTo(target.href);
    return true;
  }

  document.addEventListener('click', function (event) {
    var link = event.target.closest('a.nav-item, a.mobile-tab-item');
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }

    var target = new URL(link.href, window.location.href);
    if (!isDashboardUrl(target)) return;

    event.preventDefault();
    navigateTo(target.href);
  });

  document.addEventListener('submit', function (event) {
    var form = event.target.closest('.main-content form');
    if (!form || (form.getAttribute('method') || 'get').toLowerCase() !== 'get') return;
    event.preventDefault();
    navigateForm(form);
  });

  var filterDebounce = null;
  document.addEventListener('change', function (event) {
    var form = event.target.closest('form.filter-panel');
    if (!form || (form.getAttribute('method') || 'get').toLowerCase() !== 'get') return;
    var el = event.target;
    if (el.matches('.filter-select')) {
      if (filterDebounce) {
        clearTimeout(filterDebounce);
        filterDebounce = null;
      }
      navigateForm(form);
      return;
    }
    if (el.matches('input[type="checkbox"]')) {
      if (filterDebounce) clearTimeout(filterDebounce);
      filterDebounce = setTimeout(function () {
        filterDebounce = null;
        navigateForm(form);
      }, 250);
    }
  });

  window.addEventListener('popstate', function () {
    navigateTo(window.location.href, { updateHistory: false });
  });

  window.dashboardNavigateForm = navigateForm;
  window.dashboardNavigateTo = navigateTo;
  window.dashboardClearPageCache = function (url) {
    if (url) {
      pageCache.delete(cacheKey(url));
      return;
    }
    pageCache.clear();
  };
  window.dashboardInvalidateCurrentPage = function () {
    pageCache.delete(cacheKey(window.location.href));
  };

  var initialMain = document.querySelector('.main-content');
  if (initialMain) {
    storePageCache(cacheKey(window.location.href), {
      html: stripEnhancementFromHtml(initialMain.innerHTML),
      sidebarHtml: document.querySelector('.sidebar-nav')?.innerHTML || '',
      mobileTabsHtml: document.querySelector('.mobile-tab-bar')?.innerHTML || '',
      title: document.title,
      awaitingRealtime: document.body.dataset.awaitingRealtime === '1'
    });
    updateNavActiveState(window.location.href);
    prefetchDashboardPages();
  }
})();
