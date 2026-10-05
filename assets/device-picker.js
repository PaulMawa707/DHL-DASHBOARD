(function () {
  function normalize(value) {
    return String(value || '').toLowerCase().trim();
  }

  function initRoot(root) {
    if (!root || root.dataset.devicePickerEnhanced === '1') return;
    root.dataset.devicePickerEnhanced = '1';

    var input = root.querySelector('.device-picker-input');
    var hidden = root.querySelector('input[name="device_id"]');
    var list = root.querySelector('.device-picker-list');
    var form = root.closest('form');
    if (!input || !hidden || !list || !form) return;

    var options = Array.prototype.slice.call(
      list.querySelectorAll('[data-device-id]')
    );

    function setActive(option) {
      options.forEach(function (opt) {
        opt.classList.toggle('is-active', opt === option);
      });
    }

    function visibleOptions() {
      return options.filter(function (opt) {
        return !opt.hidden;
      });
    }

    function filter(query) {
      var q = normalize(query);
      var visible = 0;
      options.forEach(function (opt) {
        var label = opt.getAttribute('data-label') || opt.textContent || '';
        var match = !q || normalize(label).indexOf(q) !== -1;
        opt.hidden = !match;
        if (match) visible += 1;
      });
      list.hidden = visible === 0;
      setActive(visibleOptions()[0] || null);
      return visible;
    }

    function openList() {
      filter(input.value);
      if (visibleOptions().length) list.hidden = false;
    }

    function select(option) {
      if (!option) return;
      hidden.value = option.getAttribute('data-device-id') || '';
      input.value = option.getAttribute('data-label') || option.textContent || '';
      list.hidden = true;
      setActive(option);
      if (hidden.value) form.requestSubmit();
    }

    input.addEventListener('focus', openList);
    input.addEventListener('click', openList);

    input.addEventListener('input', function () {
      hidden.value = '';
      openList();
    });

    options.forEach(function (option) {
      option.addEventListener('mousedown', function (event) {
        event.preventDefault();
        select(option);
      });
    });

    input.addEventListener('keydown', function (event) {
      var visible = visibleOptions();
      var activeIndex = visible.findIndex(function (opt) {
        return opt.classList.contains('is-active');
      });

      if (event.key === 'ArrowDown') {
        event.preventDefault();
        if (!visible.length) return;
        list.hidden = false;
        var next = activeIndex < 0 ? 0 : Math.min(activeIndex + 1, visible.length - 1);
        setActive(visible[next]);
        visible[next].scrollIntoView({ block: 'nearest' });
        return;
      }

      if (event.key === 'ArrowUp') {
        event.preventDefault();
        if (!visible.length) return;
        list.hidden = false;
        var prev = activeIndex < 0 ? visible.length - 1 : Math.max(activeIndex - 1, 0);
        setActive(visible[prev]);
        visible[prev].scrollIntoView({ block: 'nearest' });
        return;
      }

      if (event.key === 'Escape') {
        list.hidden = true;
        return;
      }

      if (event.key === 'Enter') {
        event.preventDefault();
        var active = visible.find(function (opt) {
          return opt.classList.contains('is-active');
        });
        if (active) select(active);
      }
    });

    document.addEventListener('click', function (event) {
      if (!root.contains(event.target)) list.hidden = true;
    });
  }

  function init(root) {
    (root || document).querySelectorAll('[data-device-picker]').forEach(initRoot);
  }

  window.DashboardDevicePicker = { init: init };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      init(document);
    });
  } else {
    init(document);
  }
})();
