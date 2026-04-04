import asyncio
import json
import os
import tempfile
import uuid

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
from google.cloud import storage

app = FastAPI(title="Screenshot API")

# GCS setup
BUCKET_NAME = os.environ.get("BUCKET_NAME", "automated-outreach")
GCS_PROJECT = os.environ.get("GCS_PROJECT", "n8n-internal-472316")

# If GOOGLE_CREDENTIALS is provided as raw JSON, write it to a temp file
# and set GOOGLE_APPLICATION_CREDENTIALS so the SDK picks it up.
_raw_creds = os.environ.get("GOOGLE_CREDENTIALS")
if _raw_creds and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
    _creds_path = os.path.join(tempfile.gettempdir(), "gcp_sa.json")
    with open(_creds_path, "w") as f:
        f.write(_raw_creds)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _creds_path


class ScreenshotRequest(BaseModel):
    urls: list[str]


class ScreenshotResult(BaseModel):
    url: str
    desktop: str
    mobile: str


class ScreenshotResponse(BaseModel):
    results: list[ScreenshotResult]


class ExtractRequest(BaseModel):
    url: str


class LinkItem(BaseModel):
    href: str
    anchor: str


class ExtractResponse(BaseModel):
    url: str
    title: str | None
    meta_description: str | None
    canonical: str | None
    meta_robots: str | None
    viewport: str | None
    og_tags: dict[str, str]
    json_ld: list[dict]
    headings: dict[str, list[str]]
    images: dict[str, list[dict]]
    internal_links: list[LinkItem]
    external_links: list[LinkItem]
    word_count: int


class AuditPageInput(BaseModel):
    url: str
    reason: str | None = None
    in_inventory: bool | None = None


class AuditRequest(BaseModel):
    pages: list[AuditPageInput]


class AuditPageResult(BaseModel):
    url: str
    status: int | None
    redirects: list[str]
    title: str | None
    meta_desc: str | None
    canonical: str | None
    robots: str | None
    og_title: str | None
    og_desc: str | None
    og_image: str | None
    schema_types: list[str]
    schema_raw: list[dict]
    h1: list[str]
    h2: list[str]
    h3: list[str]
    h4: list[str]
    h5: list[str]
    h6: list[str]
    img_alt_ok: int
    img_alt_missing: int
    img_alt_bad: list[str]
    links_internal: int
    links_external: int
    word_count: int
    error: str | None = None


class AuditResponse(BaseModel):
    results: list[AuditPageResult]


def upload_to_gcs(local_path: str, destination_blob: str) -> str:
    client = storage.Client(project=GCS_PROJECT)
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(destination_blob)
    blob.upload_from_filename(local_path)
    return f"https://storage.googleapis.com/{BUCKET_NAME}/{destination_blob}"


LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--font-render-hinting=none",
]

# Domains to block — analytics, tracking, ads, chat widgets that slow pages
# and cause networkidle timeouts without affecting visual rendering
BLOCKED_RESOURCE_PATTERNS = [
    "google-analytics.com", "googletagmanager.com", "gtag",
    "facebook.net", "fbevents", "connect.facebook",
    "hotjar.com", "clarity.ms", "mouseflow.com",
    "doubleclick.net", "googlesyndication.com", "adservice.google",
    "intercom.io", "crisp.chat", "tawk.to", "livechat",
    "hubspot.com", "hs-scripts.com", "hs-analytics",
    "sentry.io", "bugsnag.com", "logrocket.com",
    "optimizely.com", "segment.com", "mixpanel.com",
    "amplitude.com", "heap.io",
]

COOKIE_DISMISS_JS = """
() => {
    const selectors = [
        '#onetrust-accept-btn-handler',
        '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
        '#CybotCookiebotDialogBodyButtonAccept',
        '.cc-accept', '.cc-allow', '.cc-dismiss',
        'button[data-cookiefirst-action="accept"]',
        '[aria-label="Accept cookies"]',
        '[aria-label="Accept all cookies"]',
        '[aria-label="Accept all"]',
        '[class*="cookie"] button[class*="accept"]',
        '[class*="cookie"] button[class*="Accept"]',
        '[class*="cookie"] button[class*="allow"]',
        '[class*="cookie"] button[class*="Allow"]',
        '[class*="cookie"] button[class*="agree"]',
        '[class*="consent"] button[class*="accept"]',
        '[class*="consent"] button[class*="Accept"]',
        '[class*="consent"] button[class*="allow"]',
        '[class*="consent"] button[class*="Allow"]',
        '[class*="consent"] button[class*="agree"]',
        '[data-testid="cookie-accept"]',
        '[data-testid="accept-cookies"]',
    ];
    for (const sel of selectors) {
        try {
            const el = document.querySelector(sel);
            if (el && el.offsetParent !== null) { el.click(); return true; }
        } catch {}
    }
    const buttons = document.querySelectorAll('button, a.btn, a.button, [role="button"]');
    for (const btn of buttons) {
        const text = btn.textContent?.trim().toLowerCase() || '';
        if (text.match(/^(accept|accept all|accept cookies|allow all|allow cookies|agree|got it|ok|i agree|i understand|close)$/)) {
            if (btn.offsetParent !== null) { btn.click(); return true; }
        }
    }
    return false;
}
"""

HIDE_OVERLAYS_CSS = """
    div[class*="cookie" i], div[id*="cookie" i],
    div[class*="consent" i], div[id*="consent" i],
    div[class*="gdpr" i], div[id*="gdpr" i],
    aside[class*="cookie" i], aside[class*="consent" i],
    section[class*="cookie" i], section[class*="consent" i],
    .cc-window, #onetrust-banner-sdk,
    #CybotCookiebotDialog, #CybotCookiebotDialogBodyUnderlay {
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
        pointer-events: none !important;
    }
    body.cookie-open, body.modal-open {
        overflow: auto !important;
    }
"""

FREEZE_ANIMATIONS_JS = """
() => {
    // Force all running animations to their end state, then freeze
    document.getAnimations().forEach(anim => {
        try {
            anim.finish();
        } catch {
            anim.cancel();
        }
    });
}
"""

FREEZE_ANIMATIONS_CSS = """
    *, *::before, *::after {
        animation-play-state: paused !important;
        transition-duration: 0s !important;
        transition-delay: 0s !important;
        scroll-behavior: auto !important;
    }
    /* Ensure carousels show their current slide properly */
    .slick-track, .swiper-wrapper, [class*="carousel"] {
        transition: none !important;
    }
"""

SCROLL_AND_WAIT_JS = """
async () => {
    const delay = ms => new Promise(r => setTimeout(r, ms));

    // Scroll down in steps to trigger lazy loading
    const step = window.innerHeight;
    let lastHeight = 0;
    let currentY = 0;

    for (let i = 0; i < 50; i++) {
        const height = document.body.scrollHeight;
        if (currentY >= height) break;
        currentY += step;
        window.scrollTo(0, currentY);
        await delay(400);
    }

    // Scroll back to top
    window.scrollTo(0, 0);
    await delay(500);
}
"""

WAIT_FOR_IMAGES_JS = """
async () => {
    const images = Array.from(document.querySelectorAll('img'));
    await Promise.all(images.map(img => {
        if (img.complete) return Promise.resolve();
        return new Promise(resolve => {
            img.addEventListener('load', resolve, { once: true });
            img.addEventListener('error', resolve, { once: true });
            // Timeout per image
            setTimeout(resolve, 5000);
        });
    }));
    // Wait for web fonts
    if (document.fonts && document.fonts.ready) {
        await Promise.race([
            document.fonts.ready,
            new Promise(r => setTimeout(r, 3000))
        ]);
    }
}
"""


async def block_tracking(route):
    await route.abort()


async def capture_screenshot(context, url: str) -> str:
    page = await context.new_page()

    # Block tracking/analytics requests
    await page.route("**/*", lambda route: (
        asyncio.ensure_future(block_tracking(route))
        if any(p in route.request.url for p in BLOCKED_RESOURCE_PATTERNS)
        else asyncio.ensure_future(route.continue_())
    ))

    await page.goto(url, wait_until="domcontentloaded", timeout=60000)

    # Wait for network to mostly settle (up to 10s)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass  # Some sites never reach networkidle — that's fine

    await asyncio.sleep(1)

    # Dismiss cookie banners
    await page.evaluate(COOKIE_DISMISS_JS)
    await asyncio.sleep(1)

    # Hide remaining cookie/consent overlays
    await page.add_style_tag(content=HIDE_OVERLAYS_CSS)

    # Scroll page to trigger lazy loading
    await page.evaluate(SCROLL_AND_WAIT_JS)

    # Wait for all images and fonts to load
    await page.evaluate(WAIT_FOR_IMAGES_JS)

    # Force all animations to their end state, then freeze
    await page.evaluate(FREEZE_ANIMATIONS_JS)
    await page.add_style_tag(content=FREEZE_ANIMATIONS_CSS)
    await asyncio.sleep(0.5)

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    await page.screenshot(path=tmp.name, full_page=True)
    await page.close()
    return tmp.name


EXTRACT_SEO_JS = """
() => {
    const getText = sel => {
        const el = document.querySelector(sel);
        return el ? el.textContent.trim() : null;
    };
    const getAttr = (sel, attr) => {
        const el = document.querySelector(sel);
        return el ? el.getAttribute(attr) : null;
    };

    // Title
    const title = getText('title');

    // Meta description
    const metaDescription = getAttr('meta[name="description"]', 'content');

    // Canonical
    const canonical = getAttr('link[rel="canonical"]', 'href');

    // Meta robots
    const metaRobots = getAttr('meta[name="robots"]', 'content');

    // Viewport
    const viewport = getAttr('meta[name="viewport"]', 'content');

    // OG tags
    const ogTags = {};
    document.querySelectorAll('meta[property^="og:"]').forEach(el => {
        ogTags[el.getAttribute('property')] = el.getAttribute('content') || '';
    });

    // JSON-LD / Schema blocks
    const jsonLd = [];
    document.querySelectorAll('script[type="application/ld+json"]').forEach(el => {
        try { jsonLd.push(JSON.parse(el.textContent)); } catch {}
    });

    // Headings H1-H6
    const headings = {};
    for (let i = 1; i <= 6; i++) {
        const tag = 'h' + i;
        headings[tag] = Array.from(document.querySelectorAll(tag)).map(el => el.textContent.trim());
    }

    // Images — split by alt presence
    const withAlt = [];
    const missingAlt = [];
    document.querySelectorAll('img').forEach(img => {
        const src = img.getAttribute('src');
        const alt = img.getAttribute('alt');
        if (alt && alt.trim()) {
            withAlt.push({ src, alt: alt.trim() });
        } else {
            missingAlt.push({ src });
        }
    });

    // Links — split internal vs external, include anchor text
    const baseHost = location.hostname;
    const internalLinks = [];
    const externalLinks = [];
    document.querySelectorAll('a[href]').forEach(a => {
        try {
            const href = a.href;
            if (!href || href.startsWith('javascript:') || href.startsWith('mailto:') || href.startsWith('tel:')) return;
            const anchor = a.textContent.trim();
            const url = new URL(href, location.origin);
            if (url.hostname === baseHost || url.hostname.endsWith('.' + baseHost)) {
                internalLinks.push({ href, anchor });
            } else {
                externalLinks.push({ href, anchor });
            }
        } catch {}
    });

    // Word count — visible body text
    const bodyText = document.body.innerText || '';
    const wordCount = bodyText.split(/\\s+/).filter(w => w.length > 0).length;

    return {
        title, metaDescription, canonical, metaRobots, viewport,
        ogTags, jsonLd, headings,
        images: { with_alt: withAlt, missing_alt: missingAlt },
        internalLinks, externalLinks, wordCount
    };
}
"""


@app.post("/extract", response_model=ExtractResponse)
async def extract(req: ExtractRequest):
    url = req.url.strip()
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid URL")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=LAUNCH_ARGS)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        # Block tracking/analytics
        await page.route("**/*", lambda route: (
            asyncio.ensure_future(block_tracking(route))
            if any(p in route.request.url for p in BLOCKED_RESOURCE_PATTERNS)
            else asyncio.ensure_future(route.continue_())
        ))

        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        await asyncio.sleep(1)

        # Dismiss cookie banners so they don't pollute the HTML
        await page.evaluate(COOKIE_DISMISS_JS)
        await asyncio.sleep(1)

        # Extract SEO elements
        seo = await page.evaluate(EXTRACT_SEO_JS)

        await context.close()
        await browser.close()

    return ExtractResponse(
        url=url,
        title=seo["title"],
        meta_description=seo["metaDescription"],
        canonical=seo["canonical"],
        meta_robots=seo["metaRobots"],
        viewport=seo["viewport"],
        og_tags=seo["ogTags"],
        json_ld=seo["jsonLd"],
        headings=seo["headings"],
        images=seo["images"],
        internal_links=seo["internalLinks"],
        external_links=seo["externalLinks"],
        word_count=seo["wordCount"],
    )


AUDIT_SEO_JS = """
() => {
    const getText = sel => {
        const el = document.querySelector(sel);
        return el ? el.textContent.trim() : null;
    };
    const getAttr = (sel, attr) => {
        const el = document.querySelector(sel);
        return el ? el.getAttribute(attr) : null;
    };

    const title = getText('title');
    const metaDesc = getAttr('meta[name="description"]', 'content');
    const canonical = getAttr('link[rel="canonical"]', 'href');
    const robots = getAttr('meta[name="robots"]', 'content');
    const ogTitle = getAttr('meta[property="og:title"]', 'content');
    const ogDesc = getAttr('meta[property="og:description"]', 'content');
    const ogImage = getAttr('meta[property="og:image"]', 'content');

    // Schema — collect all types and build simplified summaries
    const schemaTypes = [];
    const schemaRaw = [];
    function walkSchema(obj) {
        if (!obj || typeof obj !== 'object') return;
        if (Array.isArray(obj)) { obj.forEach(walkSchema); return; }
        if (obj['@type']) {
            const types = Array.isArray(obj['@type']) ? obj['@type'] : [obj['@type']];
            types.forEach(t => { if (!schemaTypes.includes(t)) schemaTypes.push(t); });
            const summary = { '@type': obj['@type'] };
            if (obj['@id']) summary['@id'] = obj['@id'];
            if (obj.headline) summary.headline = obj.headline;
            if (obj.name) summary.name = obj.name;
            if (obj.author) {
                summary.author = typeof obj.author === 'string' ? obj.author
                    : obj.author.name || obj.author['@id'] || null;
            }
            if (obj.description) summary.description = obj.description;
            schemaRaw.push(summary);
        }
        if (obj['@graph']) walkSchema(obj['@graph']);
    }
    document.querySelectorAll('script[type="application/ld+json"]').forEach(el => {
        try { walkSchema(JSON.parse(el.textContent)); } catch {}
    });

    // Headings — deduplicated, preserve order
    const headings = {};
    for (let i = 1; i <= 6; i++) {
        const seen = new Set();
        headings['h' + i] = [];
        document.querySelectorAll('h' + i).forEach(el => {
            const t = el.textContent.trim().replace(/\\s+/g, ' ');
            if (t && !seen.has(t)) { seen.add(t); headings['h' + i].push(t); }
        });
    }

    // Images — count good/missing, collect bad alt text (filename-like)
    let altOk = 0, altMissing = 0;
    const altBadSet = new Set();
    const badPatterns = /^(img|image|photo|picture|banner|hero|screenshot|logo|icon|untitled|placeholder|dsc|dcl|img_|wp-image|no-?bg|scaled|\\d+x\\d+)/i;
    document.querySelectorAll('img').forEach(img => {
        const alt = (img.getAttribute('alt') || '').trim();
        if (!alt) {
            altMissing++;
        } else if (badPatterns.test(alt) || alt.length < 5 || /^[A-Z0-9_\\-. ]+$/.test(alt)) {
            if (!altBadSet.has(alt)) altBadSet.add(alt);
            altOk++;
        } else {
            altOk++;
        }
    });

    // Links — deduplicated counts
    const baseHost = location.hostname;
    const internalSet = new Set();
    const externalSet = new Set();
    document.querySelectorAll('a[href]').forEach(a => {
        try {
            const href = a.href;
            if (!href || href.startsWith('javascript:') || href.startsWith('mailto:') || href.startsWith('tel:')) return;
            const url = new URL(href, location.origin);
            const normalized = url.origin + url.pathname.replace(/\\/$/, '');
            if (url.hostname === baseHost || url.hostname.endsWith('.' + baseHost)) {
                internalSet.add(normalized);
            } else {
                externalSet.add(normalized);
            }
        } catch {}
    });

    // Word count
    const bodyText = document.body.innerText || '';
    const wordCount = bodyText.split(/\\s+/).filter(w => w.length > 0).length;

    return {
        title, metaDesc, canonical, robots,
        ogTitle, ogDesc, ogImage,
        schemaTypes, schemaRaw,
        headings,
        imgAltOk: altOk, imgAltMissing: altMissing, imgAltBad: Array.from(altBadSet),
        linksInternal: internalSet.size, linksExternal: externalSet.size,
        wordCount
    };
}
"""

BATCH_SIZE = 5


async def audit_single_page(context, url: str) -> AuditPageResult:
    """Audit a single page within an existing browser context."""
    redirects = []
    final_status = None

    page = await context.new_page()

    # Track redirects
    page.on("response", lambda resp: (
        redirects.append(resp.url) if resp.status in range(300, 400) else None
    ))

    # Block tracking/analytics
    await page.route("**/*", lambda route: (
        asyncio.ensure_future(block_tracking(route))
        if any(p in route.request.url for p in BLOCKED_RESOURCE_PATTERNS)
        else asyncio.ensure_future(route.continue_())
    ))

    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        final_status = response.status if response else None

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        await asyncio.sleep(1)
        await page.evaluate(COOKIE_DISMISS_JS)
        await asyncio.sleep(1)

        seo = await page.evaluate(AUDIT_SEO_JS)

        await page.close()

        return AuditPageResult(
            url=url,
            status=final_status,
            redirects=redirects,
            title=seo["title"],
            meta_desc=seo["metaDesc"],
            canonical=seo["canonical"],
            robots=seo["robots"],
            og_title=seo["ogTitle"],
            og_desc=seo["ogDesc"],
            og_image=seo["ogImage"],
            schema_types=seo["schemaTypes"],
            schema_raw=seo["schemaRaw"],
            h1=seo["headings"]["h1"],
            h2=seo["headings"]["h2"],
            h3=seo["headings"]["h3"],
            h4=seo["headings"]["h4"],
            h5=seo["headings"]["h5"],
            h6=seo["headings"]["h6"],
            img_alt_ok=seo["imgAltOk"],
            img_alt_missing=seo["imgAltMissing"],
            img_alt_bad=seo["imgAltBad"],
            links_internal=seo["linksInternal"],
            links_external=seo["linksExternal"],
            word_count=seo["wordCount"],
        )
    except Exception as e:
        await page.close()
        return AuditPageResult(
            url=url,
            status=final_status,
            redirects=redirects,
            title=None, meta_desc=None, canonical=None, robots=None,
            og_title=None, og_desc=None, og_image=None,
            schema_types=[], schema_raw=[],
            h1=[], h2=[], h3=[], h4=[], h5=[], h6=[],
            img_alt_ok=0, img_alt_missing=0, img_alt_bad=[],
            links_internal=0, links_external=0, word_count=0,
            error=str(e),
        )


@app.post("/audit", response_model=AuditResponse)
async def audit(req: AuditRequest):
    valid_pages = [p for p in req.pages if p.url and p.url.strip().startswith(("http://", "https://"))]
    if not valid_pages:
        raise HTTPException(status_code=400, detail="No valid URLs provided")

    results: list[AuditPageResult] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=LAUNCH_ARGS)

        # Process in batches of 5
        for i in range(0, len(valid_pages), BATCH_SIZE):
            batch = valid_pages[i : i + BATCH_SIZE]
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )
            batch_results = await asyncio.gather(
                *[audit_single_page(context, p.url.strip()) for p in batch]
            )
            results.extend(batch_results)
            await context.close()

        await browser.close()

    return AuditResponse(results=results)


@app.post("/screenshot", response_model=ScreenshotResponse)
async def screenshot(req: ScreenshotRequest):
    # Filter out null/empty/invalid URLs
    valid_urls = [u for u in req.urls if u and u.strip().lower() not in ("null", "none", "undefined", "")]
    valid_urls = [u for u in valid_urls if u.startswith(("http://", "https://"))]

    if not valid_urls:
        raise HTTPException(status_code=400, detail="No valid URLs provided")

    results: list[ScreenshotResult] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=LAUNCH_ARGS)

        for url in valid_urls:
            run_id = uuid.uuid4().hex[:12]
            temp_files: list[str] = []

            try:
                # Desktop — realistic Chrome on desktop
                ctx_desktop = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    device_scale_factor=2,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                )
                desktop_path = await capture_screenshot(ctx_desktop, url)
                temp_files.append(desktop_path)
                await ctx_desktop.close()

                # Mobile — iPhone 14 Pro equivalent
                ctx_mobile = await browser.new_context(
                    viewport={"width": 393, "height": 852},
                    device_scale_factor=3,
                    is_mobile=True,
                    has_touch=True,
                    user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
                )
                mobile_path = await capture_screenshot(ctx_mobile, url)
                temp_files.append(mobile_path)
                await ctx_mobile.close()

                # Upload
                desktop_url = upload_to_gcs(
                    desktop_path, f"screenshots/{run_id}-desktop.png"
                )
                mobile_url = upload_to_gcs(
                    mobile_path, f"screenshots/{run_id}-mobile.png"
                )

                results.append(
                    ScreenshotResult(url=url, desktop=desktop_url, mobile=mobile_url)
                )
            except Exception as e:
                raise HTTPException(
                    status_code=500, detail=f"Failed to capture {url}: {e}"
                )
            finally:
                for f in temp_files:
                    try:
                        os.unlink(f)
                    except OSError:
                        pass

        await browser.close()

    return ScreenshotResponse(results=results)
