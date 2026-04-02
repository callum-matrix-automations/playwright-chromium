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
    [class*="cookie" i], [id*="cookie" i],
    [class*="consent" i], [id*="consent" i],
    [class*="gdpr" i], [id*="gdpr" i],
    .cc-window, #onetrust-banner-sdk,
    #CybotCookiebotDialog, #CybotCookiebotDialogBodyUnderlay,
    [class*="overlay" i][class*="cookie" i],
    [class*="banner" i][class*="cookie" i] {
        display: none !important;
        visibility: hidden !important;
        opacity: 0 !important;
        pointer-events: none !important;
    }
    /* Remove any backdrop/overlay that blocks content */
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


@app.post("/screenshot", response_model=ScreenshotResponse)
async def screenshot(req: ScreenshotRequest):
    if not req.urls:
        raise HTTPException(status_code=400, detail="urls list is empty")

    results: list[ScreenshotResult] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=LAUNCH_ARGS)

        for url in req.urls:
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
