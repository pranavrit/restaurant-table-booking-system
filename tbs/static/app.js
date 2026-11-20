/* Progressive enhancement: every page still works without this file. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  // ---- theme (remembered per browser)
  var root = document.documentElement;
  try { var saved = localStorage.getItem("tbs-theme"); if (saved) root.setAttribute("data-theme", saved); } catch (e) {}
  var toggle = $("#theme-toggle");
  if (toggle) toggle.addEventListener("click", function () {
    var dark = root.getAttribute("data-theme") === "dark" ||
      (!root.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
    var next = dark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("tbs-theme", next); } catch (e) {}
  });

  // ---- flash messages can be closed and fade after a while
  $$("[data-flash]").forEach(function (el) {
    var close = document.createElement("button");
    close.type = "button"; close.className = "flash-close"; close.setAttribute("aria-label", "Dismiss message");
    close.textContent = "×";
    close.addEventListener("click", function () { el.remove(); });
    el.appendChild(close);
    if (el.classList.contains("ok")) setTimeout(function () { el.classList.add("fade"); setTimeout(function () { el.remove(); }, 400); }, 6000);
  });

  // ---- confirm before risky actions
  document.addEventListener("submit", function (ev) {
    var msg = ev.target.getAttribute && ev.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) ev.preventDefault();
  });

  // ---- stop double submits
  $$("form[data-once]").forEach(function (f) {
    f.addEventListener("submit", function (ev) {
      if (ev.defaultPrevented) return;
      var b = $("button[type=submit]", f);
      if (b) { b.disabled = true; b.textContent = "Please wait…"; }
    });
  });

  // ---- character counters
  $$("[data-counter]").forEach(function (input) {
    var max = parseInt(input.getAttribute("data-counter"), 10);
    var out = document.createElement("p"); out.className = "hint counter"; input.parentNode.appendChild(out);
    var update = function () { out.textContent = input.value.length + " / " + max; };
    input.addEventListener("input", update); update();
  });

  // ---- inline e-mail check on the guest form
  var email = $("#email[type=email]");
  if (email && $("form[data-once]")) {
    var note = document.createElement("p"); note.className = "error"; note.hidden = true; email.parentNode.appendChild(note);
    email.addEventListener("blur", function () {
      var bad = email.value && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.value);
      note.hidden = !bad; note.textContent = bad ? "That does not look like an e-mail address." : "";
      email.setAttribute("aria-invalid", bad ? "true" : "false");
    });
  }

  // ---- booking page: live times, quick dates, party stepper
  var search = $("#search");
  if (search && window.fetch) {
    var date = $("#date", search), party = $("#party", search), results = $("#results"), timer, seq = 0;
    var iso = function (d) { return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2); };
    var longDate = function (s) {
      var p = s.split("-"); return new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
    };
    var render = function (data) {
      results.textContent = "";
      if (data.times && data.times.length) {
        var h = document.createElement("h2"); h.textContent = "Available times on " + longDate(date.value); results.appendChild(h);
        var ul = document.createElement("ul"); ul.className = "times";
        data.times.forEach(function (t) {
          var li = document.createElement("li"), a = document.createElement("a");
          a.textContent = t;
          a.href = search.getAttribute("data-details") + "?date=" + encodeURIComponent(date.value) + "&party=" + encodeURIComponent(party.value) + "&start=" + encodeURIComponent(t);
          li.appendChild(a); ul.appendChild(li);
        });
        results.appendChild(ul);
      } else {
        var p = document.createElement("p"); p.className = "notice"; p.setAttribute("role", "status");
        p.textContent = data.message || "No times found."; results.appendChild(p);
      }
    };
    var load = function () {
      if (!date.value || !party.value) return;
      var mine = ++seq;
      results.classList.add("loading");
      fetch(search.getAttribute("data-api") + "?date=" + encodeURIComponent(date.value) + "&party=" + encodeURIComponent(party.value), { headers: { Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (mine === seq) { render(d); results.classList.remove("loading"); history.replaceState(null, "", "?date=" + date.value + "&party=" + party.value); } })
        .catch(function () { results.classList.remove("loading"); });
    };
    var later = function () { clearTimeout(timer); timer = setTimeout(load, 250); };
    search.addEventListener("submit", function (ev) { ev.preventDefault(); load(); });
    date.addEventListener("change", load);
    party.addEventListener("input", later);
    $$("[data-step]", search).forEach(function (b) {
      b.addEventListener("click", function () {
        var v = Math.max(1, Math.min(50, (parseInt(party.value, 10) || 1) + parseInt(b.getAttribute("data-step"), 10)));
        party.value = v; later();
      });
    });
    $$("[data-days]", search).forEach(function (b) {
      b.addEventListener("click", function () {
        var d = new Date(); d.setDate(d.getDate() + parseInt(b.getAttribute("data-days"), 10));
        if (date.min && iso(d) < date.min) return;
        date.value = iso(d); load();
      });
    });
    if (!$$("#results .times a").length && date.value && party.value && !$("#results .notice")) load();
  }

  // ---- day list: search, status chips, optional auto refresh
  var table = $("#day-table");
  if (table) {
    var rows = $$("[data-row]", table), filter = $("#day-filter"), count = $("#day-count"), status = "";
    var apply = function () {
      var q = (filter.value || "").toLowerCase(), shown = 0;
      rows.forEach(function (tr) {
        var ok = (!status || tr.getAttribute("data-status") === status) && (!q || tr.textContent.toLowerCase().indexOf(q) !== -1);
        tr.hidden = !ok; if (ok) shown++;
      });
      count.textContent = rows.length ? shown + " of " + rows.length + " shown" : "";
    };
    filter.addEventListener("input", apply);
    $$("#day-tools [data-status]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        $$("#day-tools [data-status]").forEach(function (c) { c.classList.remove("on"); });
        chip.classList.add("on"); status = chip.getAttribute("data-status"); apply();
      });
    });
    var auto = $("#auto-refresh"), tick;
    try { auto.checked = sessionStorage.getItem("tbs-auto") === "1"; } catch (e) {}
    var arm = function () {
      clearInterval(tick);
      try { sessionStorage.setItem("tbs-auto", auto.checked ? "1" : "0"); } catch (e) {}
      if (auto.checked) tick = setInterval(function () { if (!document.hidden && document.activeElement.tagName !== "INPUT") location.reload(); }, 60000);
    };
    auto.addEventListener("change", arm); arm(); apply();
  }

  // ---- manager setup: jump bar highlights the section in view
  var jump = $$("#setup-jump a");
  if (jump.length && "IntersectionObserver" in window) {
    var map = {};
    jump.forEach(function (a) { map[a.getAttribute("href").slice(1)] = a; });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { jump.forEach(function (a) { a.classList.remove("on"); }); map[e.target.id].classList.add("on"); }
      });
    }, { rootMargin: "-20% 0px -70% 0px" });
    Object.keys(map).forEach(function (id) { var el = document.getElementById(id); if (el) io.observe(el); });
  }
})();
