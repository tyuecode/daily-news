/* ============================================================
   每日国际与中国热点新闻早报 — 页面交互
   原生 JS，无第三方依赖。所有功能在缺失元素时自动跳过。
   ============================================================ */
(function () {
  "use strict";

  /* ---------- 工具 ---------- */
  function $(sel, root) {
    return (root || document).querySelector(sel);
  }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function store(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch (e) {
      /* 隐私模式禁用 localStorage 时静默忽略 */
    }
    return null;
  }

  /* ---------- 1. 阅读进度条 ---------- */
  function initProgress() {
    var bar = $("#progress");
    if (!bar) return;
    var ticking = false;

    function update() {
      var doc = document.documentElement;
      var max = doc.scrollHeight - doc.clientHeight;
      var pct = max > 0 ? (doc.scrollTop / max) * 100 : 0;
      bar.style.width = Math.min(100, Math.max(0, pct)) + "%";
      ticking = false;
    }

    window.addEventListener(
      "scroll",
      function () {
        if (!ticking) {
          ticking = true;
          window.requestAnimationFrame(update);
        }
      },
      { passive: true }
    );
    update();
  }

  /* ---------- 2. 回到顶部 ---------- */
  function initToTop() {
    var btn = $("#toTop");
    if (!btn) return;
    var ticking = false;

    function toggle() {
      var show = window.scrollY > 600;
      if (show) {
        btn.hidden = false;
        btn.classList.add("is-visible");
      } else {
        btn.classList.remove("is-visible");
        // 等待淡出动画结束再隐藏
        window.setTimeout(function () {
          if (window.scrollY <= 600) btn.hidden = true;
        }, 200);
      }
      ticking = false;
    }

    btn.addEventListener("click", function () {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    window.addEventListener(
      "scroll",
      function () {
        if (!ticking) {
          ticking = true;
          window.requestAnimationFrame(toggle);
        }
      },
      { passive: true }
    );
    toggle();
  }

  /* ---------- 3. 语音播放器：音色 + 倍速 ---------- */
  function initAudio() {
    var box = $("[data-audio-box]");
    if (!box) return;
    var audio = $("[data-audio]", box);
    if (!audio) return;

    var rates = $$(".rate-btn", box);
    var voices = $$(".voice-btn", box);

    /* 恢复上次的倍速与音色偏好 */
    var savedRate = store("briefing:rate");
    var savedVoice = store("briefing:voice");

    function applyRate(rate, remember) {
      var val = parseFloat(rate);
      if (!isFinite(val) || val <= 0) val = 1;
      audio.playbackRate = val;
      rates.forEach(function (b) {
        b.classList.toggle("is-active", b.getAttribute("data-rate") === String(rate));
        b.setAttribute("aria-pressed", b.classList.contains("is-active") ? "true" : "false");
      });
      if (remember) store("briefing:rate", rate);
    }

    function applyVoice(btn, remember) {
      var src = btn.getAttribute("data-src");
      if (!src) return;
      // 保持播放进度与播放状态
      var wasPlaying = !audio.paused && !audio.ended;
      var at = audio.currentTime || 0;
      audio.src = src;
      voices.forEach(function (b) {
        b.classList.toggle("is-active", b === btn);
        b.setAttribute("aria-pressed", b === btn ? "true" : "false");
      });
      audio.load();
      if (at > 0) {
        audio.addEventListener(
          "loadedmetadata",
          function once() {
            try {
              audio.currentTime = Math.min(at, audio.duration || at);
            } catch (e) {}
            audio.removeEventListener("loadedmetadata", once);
          }
        );
      }
      if (wasPlaying) {
        var p = audio.play();
        if (p && p.catch) p.catch(function () {});
      }
      // 换音色后重新套用倍速，浏览器会重置 playbackRate
      applyRate(store("briefing:rate") || "1", false);
      if (remember) store("briefing:voice", btn.getAttribute("data-voice") || "");
    }

    rates.forEach(function (btn) {
      btn.addEventListener("click", function () {
        applyRate(btn.getAttribute("data-rate"), true);
      });
    });

    voices.forEach(function (btn) {
      btn.addEventListener("click", function () {
        applyVoice(btn, true);
      });
    });

    // 恢复倍速
    if (savedRate) applyRate(savedRate, false);

    // 恢复音色（仅当该音色按钮仍存在）
    if (savedVoice) {
      var match = voices.filter(function (b) {
        return b.getAttribute("data-voice") === savedVoice;
      })[0];
      if (match && !match.classList.contains("is-active")) applyVoice(match, false);
    }

    // 同步初始 aria 状态
    if (!rates.some(function (b) { return b.classList.contains("is-active"); })) {
      applyRate("1", false);
    }
  }

  /* ---------- 4. 目录高亮当前章节 ---------- */
  function initToc() {
    var toc = $(".toc");
    if (!toc || !("IntersectionObserver" in window)) return;
    var links = $$("a[href^='#']", toc);
    if (!links.length) return;

    var map = {};
    links.forEach(function (a) {
      var id = a.getAttribute("href").slice(1);
      var el = document.getElementById(id);
      if (el) map[id] = a;
    });

    var visible = new Set();
    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (en) {
          if (en.isIntersecting) visible.add(en.target.id);
          else visible.delete(en.target.id);
        });
        // 取文档顺序中最靠上的可见章节
        var current = null;
        Object.keys(map).forEach(function (id) {
          if (visible.has(id) && !current) current = id;
        });
        links.forEach(function (a) {
          a.classList.toggle(
            "is-current",
            current !== null && a.getAttribute("href") === "#" + current
          );
        });
      },
      { rootMargin: "-80px 0px -70% 0px", threshold: 0 }
    );

    Object.keys(map).forEach(function (id) {
      observer.observe(document.getElementById(id));
    });
  }

  /* ---------- 启动 ---------- */
  function boot() {
    initProgress();
    initToTop();
    initAudio();
    initToc();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
