(function () {
  function normalize(text) {
    return (text || '').toString().toLowerCase();
  }

  function cellValue(row, colIdx) {
    var cell = row.cells[colIdx];
    if (!cell) return '';
    return (cell.textContent || '').trim();
  }

  // Table cells can carry non-breaking spaces and wrapped whitespace from the
  // rendered markup; spreadsheets should get plain single-spaced text.
  function exportCellValue(row, colIdx) {
    return cellValue(row, colIdx).replace(/[\u00A0\s]+/g, ' ').trim();
  }

  function compareValues(a, b, type) {
    if (type === 'num') {
      var na = parseFloat(String(a).replace(/,/g, ''));
      var nb = parseFloat(String(b).replace(/,/g, ''));
      if (!Number.isNaN(na) && !Number.isNaN(nb)) return na - nb;
    }
    return normalize(a).localeCompare(normalize(b), undefined, { numeric: true, sensitivity: 'base' });
  }

  function xmlEscape(text) {
    return String(text || '')
      .replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function colLetter(index) {
    var n = index + 1;
    var letters = '';
    while (n > 0) {
      var rem = (n - 1) % 26;
      letters = String.fromCharCode(65 + rem) + letters;
      n = Math.floor((n - 1) / 26);
    }
    return letters;
  }

  var NUMBER_RE = /^-?(?:0|[1-9]\d{0,14})(?:\.\d+)?$/;
  var DATE_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?$/;

  function isExcelNumber(value) {
    if (!value) return false;
    // Leading zeros (e.g. device id "03068") must survive as text.
    if (/^0\d/.test(value)) return false;
    return NUMBER_RE.test(value);
  }

  // Identifier-like columns stay text so Excel keeps leading zeros and never
  // switches long device ids to scientific notation.
  function isIdentifierHeader(header) {
    var key = String(header || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    if (!key) return false;
    if (/(^|[a-z0-9])id$/.test(key)) return true;
    return /(plate|registration|imei|iccid|msisdn|serial|vin|phone|simno)/.test(key);
  }

  function excelSerial(value) {
    var m = DATE_RE.exec(value);
    if (!m) return null;
    var utc = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
    var days = Math.round((utc - Date.UTC(1899, 11, 30)) / 86400000);
    if (!Number.isFinite(days) || days < 1) return null;
    var seconds = (Number(m[4] || 0) * 3600) + (Number(m[5] || 0) * 60) + Number(m[6] || 0);
    return { serial: days + seconds / 86400, hasTime: m[4] !== undefined };
  }

  // One type per column keeps a column consistent in Excel instead of mixing
  // numbers and text depending on the individual cell.
  function columnTypes(headers, rows) {
    return headers.map(function (header, idx) {
      if (isIdentifierHeader(header)) return 'text';
      var seen = 0;
      var numbers = 0;
      var dates = 0;
      var datesWithTime = 0;
      for (var r = 0; r < rows.length; r++) {
        var value = rows[r][idx];
        if (value === undefined || value === null || value === '') continue;
        seen += 1;
        if (isExcelNumber(value)) numbers += 1;
        var parsed = excelSerial(value);
        if (parsed) {
          dates += 1;
          if (parsed.hasTime) datesWithTime += 1;
        }
      }
      if (!seen) return 'text';
      if (numbers === seen) return 'num';
      if (dates === seen) return datesWithTime ? 'datetime' : 'date';
      return 'text';
    });
  }

  function crc32(bytes) {
    var crc = -1;
    for (var i = 0; i < bytes.length; i++) {
      crc ^= bytes[i];
      for (var bit = 0; bit < 8; bit++) {
        crc = (crc >>> 1) ^ (crc & 1 ? 0xEDB88320 : 0);
      }
    }
    return (crc ^ -1) >>> 0;
  }

  function concatBytes(parts) {
    var total = 0;
    for (var i = 0; i < parts.length; i++) total += parts[i].length;
    var out = new Uint8Array(total);
    var offset = 0;
    for (var j = 0; j < parts.length; j++) {
      out.set(parts[j], offset);
      offset += parts[j].length;
    }
    return out;
  }

  function u16(value) {
    return new Uint8Array([value & 255, (value >>> 8) & 255]);
  }

  function u32(value) {
    return new Uint8Array([
      value & 255,
      (value >>> 8) & 255,
      (value >>> 16) & 255,
      (value >>> 24) & 255,
    ]);
  }

  function zipStore(files) {
    var encoder = new TextEncoder();
    var locals = [];
    var centrals = [];
    var offset = 0;
    for (var i = 0; i < files.length; i++) {
      var nameBytes = encoder.encode(files[i].name);
      var data = files[i].data;
      var crc = crc32(data);
      var local = concatBytes([
        u32(0x04034b50),
        u16(20),
        u16(0),
        u16(0),
        u16(0),
        u16(0),
        u32(crc),
        u32(data.length),
        u32(data.length),
        u16(nameBytes.length),
        u16(0),
        nameBytes,
        data,
      ]);
      var central = concatBytes([
        u32(0x02014b50),
        u16(20),
        u16(20),
        u16(0),
        u16(0),
        u16(0),
        u16(0),
        u32(crc),
        u32(data.length),
        u32(data.length),
        u16(nameBytes.length),
        u16(0),
        u16(0),
        u16(0),
        u16(0),
        u32(0),
        u32(offset),
        nameBytes,
      ]);
      locals.push(local);
      centrals.push(central);
      offset += local.length;
    }
    var localAll = concatBytes(locals);
    var centralAll = concatBytes(centrals);
    var eocd = concatBytes([
      u32(0x06054b50),
      u16(0),
      u16(0),
      u16(files.length),
      u16(files.length),
      u32(centralAll.length),
      u32(localAll.length),
      u16(0),
    ]);
    return concatBytes([localAll, centralAll, eocd]);
  }

  function sheetNameFromTitle(title) {
    var name = String(title || 'Sheet1')
      .replace(/[\\/?*[\]:]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    if (!name) name = 'Sheet1';
    return name.slice(0, 31);
  }

  function fileNameFromTitle(title) {
    var slug = String(title || 'table')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '');
    if (!slug) slug = 'table';
    var now = new Date();
    var y = now.getFullYear();
    var m = String(now.getMonth() + 1).padStart(2, '0');
    var d = String(now.getDate()).padStart(2, '0');
    return slug + '-' + y + '-' + m + '-' + d + '.xlsx';
  }

  // Style ids defined in stylesXml(): 1 = header, 2 = date, 3 = datetime.
  var DATE_STYLE = { date: 2, datetime: 3 };

  function buildSheetXml(headers, rows) {
    var types = columnTypes(headers, rows);
    var lastCol = colLetter(Math.max(headers.length - 1, 0));
    var lastRow = Math.max(rows.length + 1, 1);
    var widths = headers.map(function (header) {
      return Math.min(60, Math.max(10, String(header || '').length + 4));
    });
    rows.forEach(function (row) {
      row.forEach(function (value, idx) {
        widths[idx] = Math.min(60, Math.max(widths[idx] || 10, String(value || '').length + 2));
      });
    });
    var cols = widths.map(function (width, idx) {
      return '<col min="' + (idx + 1) + '" max="' + (idx + 1) + '" width="' + width + '" customWidth="1"/>';
    }).join('');
    var body = [];
    body.push('<row r="1">');
    headers.forEach(function (header, idx) {
      body.push(
        '<c r="' + colLetter(idx) + '1" t="inlineStr" s="1"><is><t xml:space="preserve">' +
          xmlEscape(header) +
        '</t></is></c>'
      );
    });
    body.push('</row>');
    rows.forEach(function (row, rowIdx) {
      var r = rowIdx + 2;
      body.push('<row r="' + r + '">');
      row.forEach(function (value, idx) {
        if (value === undefined || value === null || value === '') return;
        var ref = colLetter(idx) + r;
        var type = types[idx];
        if (type === 'num') {
          body.push('<c r="' + ref + '"><v>' + value + '</v></c>');
          return;
        }
        if (type === 'date' || type === 'datetime') {
          var parsed = excelSerial(value);
          if (parsed) {
            body.push('<c r="' + ref + '" s="' + DATE_STYLE[type] + '"><v>' + parsed.serial + '</v></c>');
            return;
          }
        }
        body.push(
          '<c r="' + ref + '" t="inlineStr"><is><t xml:space="preserve">' +
            xmlEscape(value) +
          '</t></is></c>'
        );
      });
      body.push('</row>');
    });
    return (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
      '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' +
      '<cols>' + cols + '</cols>' +
      '<sheetData>' + body.join('') + '</sheetData>' +
      '<autoFilter ref="A1:' + lastCol + lastRow + '"/>' +
      '</worksheet>'
    );
  }

  function workbookXml(name) {
    return (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ' +
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
      '<sheets><sheet name="' + xmlEscape(name) + '" sheetId="1" r:id="rId1"/></sheets>' +
      '</workbook>'
    );
  }

  function stylesXml() {
    return (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' +
      '<numFmts count="2">' +
      '<numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/>' +
      '<numFmt numFmtId="165" formatCode="yyyy\\-mm\\-dd\\ hh:mm:ss"/>' +
      '</numFmts>' +
      '<fonts count="2">' +
      '<font><sz val="11"/><name val="Calibri"/></font>' +
      '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>' +
      '</fonts>' +
      '<fills count="3">' +
      '<fill><patternFill patternType="none"/></fill>' +
      '<fill><patternFill patternType="gray125"/></fill>' +
      '<fill><patternFill patternType="solid"><fgColor rgb="FF183B63"/><bgColor indexed="64"/></patternFill></fill>' +
      '</fills>' +
      '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>' +
      '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>' +
      '<cellXfs count="4">' +
      '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' +
      '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>' +
      '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
      '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>' +
      '</cellXfs>' +
      '</styleSheet>'
    );
  }

  function contentTypesXml() {
    return (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
      '<Default Extension="xml" ContentType="application/xml"/>' +
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
      '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' +
      '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' +
      '</Types>'
    );
  }

  function rootRelsXml() {
    return (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
      '</Relationships>'
    );
  }

  function workbookRelsXml() {
    return (
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' +
      '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' +
      '</Relationships>'
    );
  }

  function buildXlsx(headers, rows, title) {
    var encoder = new TextEncoder();
    function xml(text) {
      return encoder.encode(text);
    }
    return zipStore([
      { name: '[Content_Types].xml', data: xml(contentTypesXml()) },
      { name: '_rels/.rels', data: xml(rootRelsXml()) },
      { name: 'xl/workbook.xml', data: xml(workbookXml(sheetNameFromTitle(title))) },
      { name: 'xl/_rels/workbook.xml.rels', data: xml(workbookRelsXml()) },
      { name: 'xl/styles.xml', data: xml(stylesXml()) },
      { name: 'xl/worksheets/sheet1.xml', data: xml(buildSheetXml(headers, rows)) },
    ]);
  }

  function downloadBytes(bytes, filename) {
    var blob = new Blob([bytes], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    });
    var url = URL.createObjectURL(blob);
    var link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1000);
  }

  function tableTitle(root) {
    var card = root.closest('.table-card');
    var heading = card && card.querySelector('.table-title');
    return heading ? heading.textContent.trim() : 'table';
  }

  function initTable(root) {
    var table = root.querySelector('.report-data-table');
    if (!table || table.dataset.enhanced === '1') return;
    table.dataset.enhanced = '1';

    var tbody = table.querySelector('tbody');
    var headers = Array.from(table.querySelectorAll('thead th'));
    var rows = Array.from(tbody.querySelectorAll('tr'));
    var search = root.querySelector('.table-search');
    var pageSizeSelect = root.querySelector('.table-page-size');
    var exportBtn = root.querySelector('.table-export-xlsx');
    var prev = root.querySelector('.table-prev');
    var next = root.querySelector('.table-next');
    var pageLabel = root.querySelector('.table-page-label');
    var count = root.querySelector('.table-count');
    var page = 1;
    var sortCol = null;
    var sortDir = 1;

    function pageSize() {
      var raw = pageSizeSelect ? parseInt(pageSizeSelect.value, 10) : parseInt(root.dataset.pageSize || '10', 10);
      return Number.isFinite(raw) && raw > 0 ? raw : 10;
    }

    function filteredRows() {
      var q = normalize(search ? search.value : '').trim();
      if (!q) return rows;
      return rows.filter(function (row) {
        return normalize(row.textContent).indexOf(q) !== -1;
      });
    }

    function applySort(list) {
      if (sortCol === null) return list;
      var type = headers[sortCol] && headers[sortCol].dataset.sortType === 'num' ? 'num' : 'text';
      return list.slice().sort(function (ra, rb) {
        var cmp = compareValues(cellValue(ra, sortCol), cellValue(rb, sortCol), type);
        return cmp * sortDir;
      });
    }

    function render() {
      var visible = applySort(filteredRows());
      var size = pageSize();
      var pages = Math.max(1, Math.ceil(visible.length / size));
      page = Math.min(Math.max(page, 1), pages);
      var start = (page - 1) * size;
      var end = start + size;

      rows.forEach(function (row) {
        tbody.appendChild(row);
        row.hidden = true;
      });
      visible.slice(start, end).forEach(function (row) {
        row.hidden = false;
      });

      if (pageLabel) pageLabel.textContent = 'Page ' + page + ' / ' + pages;
      if (prev) prev.disabled = page <= 1;
      if (next) next.disabled = page >= pages;
      if (count) {
        var shownEnd = visible.length ? Math.min(end, visible.length) : 0;
        var shownStart = visible.length ? start + 1 : 0;
        count.textContent = 'Showing ' + shownStart + '-' + shownEnd + ' of ' + visible.length + ' rows';
      }
    }

    headers.forEach(function (th, idx) {
      th.classList.add('sortable-th');
      th.setAttribute('role', 'columnheader');
      th.setAttribute('tabindex', '0');
      if (idx === 0) th.dataset.sortType = 'num';
      th.addEventListener('click', function () {
        if (sortCol === idx) {
          sortDir = sortDir * -1;
        } else {
          sortCol = idx;
          sortDir = 1;
        }
        headers.forEach(function (h) {
          h.classList.remove('sort-asc', 'sort-desc');
        });
        th.classList.add(sortDir > 0 ? 'sort-asc' : 'sort-desc');
        page = 1;
        render();
      });
      th.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          th.click();
        }
      });
    });

    if (search) {
      search.addEventListener('input', function () {
        page = 1;
        render();
      });
    }
    if (pageSizeSelect) {
      pageSizeSelect.addEventListener('change', function () {
        page = 1;
        render();
      });
    }
    if (prev) {
      prev.addEventListener('click', function () {
        page -= 1;
        render();
      });
    }
    if (next) {
      next.addEventListener('click', function () {
        page += 1;
        render();
      });
    }
    if (exportBtn) {
      exportBtn.addEventListener('click', function () {
        var visible = applySort(filteredRows());
        var headerLabels = headers.map(function (th) {
          return (th.textContent || '').replace(/[\u00A0\s]+/g, ' ').trim();
        });
        var dataRows = visible.map(function (row) {
          return headers.map(function (_th, idx) {
            return exportCellValue(row, idx);
          });
        });
        var title = tableTitle(root);
        downloadBytes(buildXlsx(headerLabels, dataRows, title), fileNameFromTitle(title));
      });
    }
    render();
  }

  function initAll() {
    document.querySelectorAll('.report-table').forEach(initTable);
  }

  function init(root) {
    (root || document).querySelectorAll('.report-table').forEach(initTable);
  }

  window.DashboardTables = { init: init };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }
})();
