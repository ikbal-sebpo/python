/*
 * Renders a policy PDF page-by-page onto <canvas> elements instead of the
 * browser's native PDF plugin (<iframe src="...pdf">). The native plugin is a
 * separate rendering surface page JS can't reach, so its right-click "Save
 * As" can't be suppressed. Canvas is ordinary DOM, so contextmenu blocking
 * here actually works. This is still a deterrent, not real prevention: the
 * PDF bytes still transit the network to render, so devtools/inspection can
 * still get at them.
 *
 * installPrintGuard() blocks Ctrl/Cmd+P and any window.print() call, backed
 * up by the @media print rule in theme/static_src/src/styles.css in case
 * print is triggered some other way. drawWatermark() bakes the viewer's
 * identity + timestamp (data-watermark on the container) into every
 * rendered page. Neither stops Print Screen or a phone camera — nothing on
 * the web can. The watermark exists so a leaked screenshot is traceable
 * back to who viewed it, not to prevent the screenshot itself.
 *
 * installClipboardGuard() is a best-effort extra: on Windows, PrintScreen
 * copies a full-screen image to the OS clipboard before any page JS can
 * react, so this can only overwrite the clipboard a moment later — it does
 * not stop the capture, and only wins if it runs before the user pastes. No
 * effect on macOS/mobile (no PrintScreen key there), a phone camera, or any
 * capture path that doesn't go through the OS's PrintScreen-to-clipboard
 * hotkey. Cheap and harmless, not a control to rely on.
 *
 * pdf.js itself is loaded via a dynamic import() of the URL in
 * data-pdfjs-url (rather than a static top-level import) so the path goes
 * through {% static %} in the template and survives whitenoise's
 * content-hashed filenames in production.
 */
function blockCopy(el) {
    el.oncontextmenu = function () { return false; };
    el.ondragstart = function () { return false; };
    el.setAttribute("draggable", "false");
}

// Ctrl/Cmd+P and any other print trigger (browser menu, an extension,
// window.print() called from JS) on a page with the viewer. Reinforced by
// the @media print CSS rule (theme/static_src/src/styles.css) that blanks
// the actual print output if a print somehow still gets triggered — that
// CSS layer is the one that actually matters, since it governs what prints
// regardless of how printing got started. Neither layer can do anything
// about Print Screen / a phone camera — see drawWatermark below for that.
function installPrintGuard() {
    document.addEventListener("keydown", function (e) {
        var key = (e.key || "").toLowerCase();
        if ((e.ctrlKey || e.metaKey) && key === "p") {
            e.preventDefault();
            e.stopPropagation();
        }
    }, true);
    window.print = function () {};
    document.body.classList.add("policy-print-guard");
}

// See the module header for why this only wins a race, never actually stops
// the capture. `navigator.clipboard` may be missing (older browser) or
// `writeText` may reject (no permission, no focus) — both silently no-op.
function installClipboardGuard() {
    document.addEventListener("keyup", function (e) {
        if (e.key !== "PrintScreen") return;
        if (!navigator.clipboard || !navigator.clipboard.writeText) return;
        navigator.clipboard
            .writeText("Screenshots of this document are restricted by company policy.")
            .catch(function () {});
    });
}

// Tiles `text` diagonally across the canvas at low opacity, baked into the
// pixels themselves (not a CSS/DOM overlay, which devtools could strip) —
// so it's part of whatever ends up in a screenshot, photo, or printout.
// Deterrent only: identifies who viewed the document, doesn't stop the
// capture itself (nothing on the web can — see policy-pdf-viewer.js header).
function drawWatermark(canvas, text) {
    if (!text) return;
    var ctx = canvas.getContext("2d");
    var w = canvas.width, h = canvas.height;
    ctx.save();
    ctx.globalAlpha = 0.12;
    ctx.fillStyle = "#000000";
    ctx.font = Math.round(w / 26) + "px sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    var stepX = w / 2.2, stepY = h / 7;
    for (var y = 0; y < h + stepY; y += stepY) {
        for (var x = 0; x < w + stepX; x += stepX) {
            ctx.save();
            ctx.translate(x, y);
            ctx.rotate(-Math.PI / 6);
            ctx.fillText(text, 0, 0);
            ctx.restore();
        }
    }
    ctx.restore();
}

// Stretching pages to the full width of a wide viewer reads as "zoomed in"
// compared to a browser's native PDF viewer, which renders near actual
// print size with margins either side. Cap page width to a comfortable
// reading size instead and center it, like a sheet of paper on a desk.
var MAX_PAGE_WIDTH = 840;
var CONTAINER_PADDING = 24;

async function initViewer(container) {
    var pdfUrl = container.dataset.pdfUrl;
    var pdfjsUrl = container.dataset.pdfjsUrl;
    var workerUrl = container.dataset.pdfjsWorkerUrl;
    var watermark = container.dataset.watermark || "";
    if (!pdfUrl || !pdfjsUrl || !workerUrl) return;

    installPrintGuard();
    installClipboardGuard();
    container.classList.add("select-none", "flex", "flex-col", "items-center", "gap-3");
    container.style.padding = CONTAINER_PADDING + "px";
    blockCopy(container);

    var status = document.createElement("div");
    status.className = "flex items-center justify-center h-full text-sm text-text-soft dark:text-white/60";
    status.textContent = "Loading document…";
    container.appendChild(status);

    try {
        var pdfjsLib = await import(pdfjsUrl);
        pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl;

        var resp = await fetch(pdfUrl, { credentials: "same-origin" });
        if (!resp.ok) throw new Error("Failed to load PDF (" + resp.status + ")");
        var data = await resp.arrayBuffer();
        var pdf = await pdfjsLib.getDocument({ data: data }).promise;

        container.removeChild(status);

        var availableWidth = container.clientWidth - CONTAINER_PADDING * 2;
        var targetWidth = Math.max(240, Math.min(availableWidth, MAX_PAGE_WIDTH));
        var dpr = window.devicePixelRatio || 1;

        for (var pageNum = 1; pageNum <= pdf.numPages; pageNum++) {
            var page = await pdf.getPage(pageNum);
            var baseViewport = page.getViewport({ scale: 1 });
            var viewport = page.getViewport({ scale: (targetWidth / baseViewport.width) * dpr });

            var canvas = document.createElement("canvas");
            canvas.width = viewport.width;
            canvas.height = viewport.height;
            canvas.style.width = targetWidth + "px";
            canvas.style.height = "auto";
            canvas.style.display = "block";
            canvas.className = "bg-white shadow-card shrink-0";
            blockCopy(canvas);

            container.appendChild(canvas);

            await page.render({ canvasContext: canvas.getContext("2d"), viewport: viewport }).promise;
            drawWatermark(canvas, watermark);
        }
    } catch (err) {
        if (status.parentNode) status.textContent = "This document couldn't be displayed. Please contact HR.";
        console.error("Policy PDF viewer error:", err);
    }
}

document.querySelectorAll("[data-policy-pdf-viewer]").forEach(initViewer);
