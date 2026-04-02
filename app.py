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
]


COOKIE_DISMISS_JS = """
() => {
    // Common cookie consent selectors
    const selectors = [
        '[class*="cookie"] button[class*="accept"]',
        '[class*="cookie"] button[class*="Allow"]',
        '[class*="cookie"] button[class*="agree"]',
        '[class*="consent"] button[class*="accept"]',
        '[class*="consent"] button[class*="Allow"]',
        '[class*="consent"] button[class*="agree"]',
        '#onetrust-accept-btn-handler',
        '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
        '.cc-accept', '.cc-allow', '.cc-dismiss',
        'button[data-cookiefirst-action="accept"]',
        '[aria-label="Accept cookies"]',
        '[aria-label="Accept all cookies"]',
        'button:has-text("Accept")',
    ];
    for (const sel of selectors) {
        try {
            const el = document.querySelector(sel);
            if (el) { el.click(); return true; }
        } catch {}
    }
    // Fallback: find buttons by text content
    const buttons = document.querySelectorAll('button, a.btn, a.button, [role="button"]');
    for (const btn of buttons) {
        const text = btn.textContent?.trim().toLowerCase() || '';
        if (text.match(/^(accept|accept all|allow all|agree|got it|ok|i agree)$/)) {
            btn.click();
            return true;
        }
    }
    return false;
}
"""

HIDE_OVERLAYS_CSS = """
    [class*="cookie"], [id*="cookie"],
    [class*="consent"], [id*="consent"],
    [class*="Cookie"], [id*="Cookie"],
    [class*="Consent"], [id*="Consent"],
    .cc-window, #onetrust-banner-sdk,
    #CybotCookiebotDialog,
    [class*="gdpr"], [id*="gdpr"] {
        display: none !important;
        visibility: hidden !important;
    }
"""

SCROLL_PAGE_JS = """
async () => {
    const delay = ms => new Promise(r => setTimeout(r, ms));
    const height = document.body.scrollHeight;
    const step = window.innerHeight;
    for (let y = 0; y < height; y += step) {
        window.scrollTo(0, y);
        await delay(300);
    }
    window.scrollTo(0, 0);
}
"""


async def capture_screenshot(page, url: str, viewport: dict, is_mobile: bool) -> str:
    await page.set_viewport_size(viewport)
    if is_mobile:
        await page.emulate_media()
    await page.goto(url, wait_until="load", timeout=60000)
    await asyncio.sleep(2)

    # Dismiss cookie banners
    await page.evaluate(COOKIE_DISMISS_JS)
    await asyncio.sleep(1)

    # Force-hide any remaining cookie/consent overlays
    await page.add_style_tag(content=HIDE_OVERLAYS_CSS)

    # Scroll through page to trigger lazy loading and carousels
    await page.evaluate(SCROLL_PAGE_JS)
    await asyncio.sleep(2)

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    await page.screenshot(path=tmp.name, full_page=True)
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
                # Desktop
                ctx_desktop = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                )
                page_desktop = await ctx_desktop.new_page()
                desktop_path = await capture_screenshot(
                    page_desktop, url, {"width": 1440, "height": 900}, False
                )
                temp_files.append(desktop_path)
                await ctx_desktop.close()

                # Mobile
                ctx_mobile = await browser.new_context(
                    viewport={"width": 390, "height": 844},
                    is_mobile=True,
                )
                page_mobile = await ctx_mobile.new_page()
                mobile_path = await capture_screenshot(
                    page_mobile, url, {"width": 390, "height": 844}, True
                )
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
