(function () {
  var STORAGE_KEY = 'dhl-sidebar-collapsed';

  function isMobile() {
    return window.matchMedia('(max-width: 900px)').matches;
  }

  function isCollapsed() {
    return document.documentElement.classList.contains('sidebar-collapsed')
      || document.body.classList.contains('sidebar-collapsed');
  }

  function setCollapsed(collapsed) {
    var on = collapsed && !isMobile();
    document.documentElement.classList.toggle('sidebar-collapsed', on);
    if (document.body) {
      document.body.classList.toggle('sidebar-collapsed', on);
    }
    var btn = document.getElementById('sidebar-toggle');
    if (btn) {
      btn.setAttribute('aria-expanded', on ? 'false' : 'true');
      btn.setAttribute('aria-label', on ? 'Expand sidebar' : 'Collapse sidebar');
    }
  }

  function savedCollapsed() {
    var saved = localStorage.getItem(STORAGE_KEY);
    return saved === null ? true : saved === '1';
  }

  function init() {
    var btn = document.getElementById('sidebar-toggle');
    if (!btn) return;

    setCollapsed(savedCollapsed());

    btn.addEventListener('click', function () {
      if (isMobile()) return;
      var willCollapse = !isCollapsed();
      localStorage.setItem(STORAGE_KEY, willCollapse ? '1' : '0');
      setCollapsed(willCollapse);
    });

    window.addEventListener('resize', function () {
      setCollapsed(savedCollapsed());
    });

    var sidebar = document.getElementById('app-sidebar');
    if (sidebar) {
      sidebar.addEventListener('transitionend', function (event) {
        if (event.propertyName === 'width') {
          window.dispatchEvent(new Event('resize'));
        }
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
